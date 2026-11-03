#!/usr/bin/env python3
"""Strict review receipt schema and coordinator dispatch ledger module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from providers import is_hex_40, iso_timestamp, sha256_file

SCHEMA_VERSION = "review-receipt/v1"
REQUIRED_RECEIPT_KEYS = {
    "schema",
    "dispatch_id",
    "reviewer_session_id",
    "family",
    "model",
    "commit_sha",
    "tree_sha",
    "verdict",
    "findings",
}
REQUIRED_FINDING_KEYS = {"id", "severity", "file", "summary"}
VALID_FAMILIES = {"argon", "opus", "sol"}
VALID_VERDICTS = {"SHIP", "REVISE"}
FORBIDDEN_MODELS = {"claude-fable-5-1"}
CANONICAL_FAMILY_MODELS: dict[str, set[str]] = {
    "opus": {"claude-opus-5-5"},
    "argon": {"gemini-4.0-argon-high"},
    "sol": {"gpt-6.1-sol-high", "gpt-6.1-sol"},
}


def _validate_finding_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if set(item.keys()) != REQUIRED_FINDING_KEYS:
        return False
    return all(isinstance(item[k], str) for k in REQUIRED_FINDING_KEYS)


def _validate_receipt_structure(data: dict[str, Any]) -> tuple[bool, str]:
    if set(data.keys()) != REQUIRED_RECEIPT_KEYS:
        extra = set(data.keys()) - REQUIRED_RECEIPT_KEYS
        if extra:
            return False, "SCHEMA_EXTRA_KEYS_FORBIDDEN"
        return False, "SCHEMA_MISSING_REQUIRED_KEYS"
    if data["schema"] != SCHEMA_VERSION:
        return False, "SCHEMA_VERSION_UNSUPPORTED"
    if not (isinstance(data["dispatch_id"], str) and data["dispatch_id"].strip()):
        return False, "INVALID_DISPATCH_ID"
    if not (isinstance(data["reviewer_session_id"], str) and data["reviewer_session_id"].strip()):
        return False, "INVALID_REVIEWER_SESSION_ID"
    if data["family"] not in VALID_FAMILIES:
        return False, "INVALID_FAMILY"
    return True, ""


def _validate_receipt_values(data: dict[str, Any]) -> tuple[bool, str]:
    fam = data["family"]
    allowed_models = CANONICAL_FAMILY_MODELS.get(fam, set())
    if data["model"] not in allowed_models:
        return False, "UNAPPROVED_REVIEWER_MODEL"
    if not (is_hex_40(data["commit_sha"]) and is_hex_40(data["tree_sha"])):
        return False, "INVALID_REVISION_SHA"
    if data["verdict"] not in VALID_VERDICTS:
        return False, "INVALID_VERDICT"
    if not isinstance(data["findings"], list):
        return False, "INVALID_FINDINGS_CONTAINER"
    for item in data["findings"]:
        if not _validate_finding_item(item):
            return False, "FINDING_SCHEMA_VIOLATION"
    return True, ""


def validate_review_receipt(data: Any) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(data, dict):
        return False, "REPORT_INVALID", {}
    st_ok, st_err = _validate_receipt_structure(data)
    if not st_ok:
        return False, st_err, {}
    val_ok, val_err = _validate_receipt_values(data)
    if not val_ok:
        return False, val_err, {}
    return True, "", data


def verify_dispatch_binding(
    dispatch_ledger: dict[str, Any],
    dispatch_id: str,
    work_id: str,
    family: str,
    session_id: str,
) -> tuple[bool, str]:
    if not dispatch_ledger or dispatch_id not in dispatch_ledger:
        return False, "UNBOUND_DISPATCH_ID"
    entry = dispatch_ledger[dispatch_id]
    if not isinstance(entry, dict):
        return False, "UNBOUND_DISPATCH_ID"
    if entry.get("work_id") != work_id:
        return False, "DISPATCH_WORK_ID_MISMATCH"
    if entry.get("family") != family:
        return False, "DISPATCH_FAMILY_MISMATCH"
    entry_sess = entry.get("reviewer_session_id") or entry.get("session_id")
    if entry_sess != session_id:
        return False, "DISPATCH_SESSION_MISMATCH"
    return True, ""


def _filter_ship_reviews(reviews: list[dict[str, Any]], commit_sha: str, tree_sha: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in reviews:
        if not isinstance(r, dict):
            continue
        if r.get("verdict") == "SHIP" and r.get("commit_sha") == commit_sha and r.get("tree_sha") == tree_sha:
            out.append(r)
    return out


def _collect_family_reviews(
    ship_revs: list[dict[str, Any]],
) -> tuple[bool, dict[str, dict[str, Any]]]:
    by_family: dict[str, dict[str, Any]] = {}
    for r in ship_revs:
        if not isinstance(r, dict):
            return False, {}
        fam = r.get("family")
        if not isinstance(fam, str) or fam not in VALID_FAMILIES:
            return False, {}
        if fam in by_family:
            return False, {}
        by_family[fam] = r
    if set(by_family.keys()) != VALID_FAMILIES:
        return False, {}
    return True, by_family


def _verify_distinct_identities(
    reviews: list[dict[str, Any]],
) -> bool:
    sessions: set[str] = set()
    dispatches: set[str] = set()
    for r in reviews:
        if not isinstance(r, dict):
            return False
        sess = r.get("reviewer_session_id")
        disp = r.get("dispatch_id")
        if not isinstance(sess, str) or not sess.strip():
            return False
        if not isinstance(disp, str) or not disp.strip():
            return False
        sessions.add(sess.strip())
        dispatches.add(disp.strip())
    return len(sessions) == 3 and len(dispatches) == 3


def _check_ship_sets(ship_revs: list[dict[str, Any]]) -> bool:
    if len(ship_revs) < 3:
        return False
    ok, by_fam = _collect_family_reviews(ship_revs)
    if not ok:
        return False
    return _verify_distinct_identities(list(by_fam.values()))


def _is_single_ship_ledger_bound(
    r: dict[str, Any],
    dispatch_ledger: dict[str, Any],
    work_id: str,
) -> bool:
    d_id = str(r.get("dispatch_id") or "")
    entry = dispatch_ledger.get(d_id)
    if not isinstance(entry, dict):
        return False
    sess = r.get("reviewer_session_id")
    entry_sess = entry.get("reviewer_session_id") or entry.get("session_id")
    if entry.get("family") != r.get("family") or entry_sess != sess:
        return False
    if work_id and entry.get("work_id") != work_id:
        return False
    return True


def _check_ship_ledger_bindings(
    ship_revs: list[dict[str, Any]],
    dispatch_ledger: dict[str, Any],
    work_id: str = "",
) -> bool:
    return all(_is_single_ship_ledger_bound(r, dispatch_ledger, work_id) for r in ship_revs)


def check_qualification(
    reviews: list[dict[str, Any]],
    commit_sha: str,
    tree_sha: str,
    dispatch_ledger: dict[str, Any] | None = None,
    work_id: str = "",
) -> bool:
    if not isinstance(dispatch_ledger, dict) or not dispatch_ledger:
        return False
    ship_revs = _filter_ship_reviews(reviews, commit_sha, tree_sha)
    if not _check_ship_sets(ship_revs):
        return False
    return _check_ship_ledger_bindings(ship_revs, dispatch_ledger, work_id=work_id)


def _is_session_already_dispatched(ledger: dict[str, Any], work_id: str, session_id: str) -> bool:
    for d_item in ledger.values():
        if isinstance(d_item, dict) and d_item.get("work_id") == work_id:
            d_sess = d_item.get("reviewer_session_id") or d_item.get("session_id")
            if d_sess == session_id:
                return True
    return False


def _check_dispatch_role_and_uniqueness(
    doc: dict[str, Any],
    work_id: str,
    dispatch_id: str,
    reviewer_session_id: str,
) -> tuple[bool, str]:
    dc = doc.setdefault("delivery_control", {})
    work_items = dc.setdefault("work_items", {})
    wi = work_items.get(work_id, {})
    claim = wi.get("claim", {})
    if claim and claim.get("session_id") == reviewer_session_id:
        return False, "SESSION_REUSED_ACROSS_ROLES"
    ledger = dc.setdefault("dispatch_ledger", {})
    if dispatch_id in ledger:
        return False, "DUPLICATE_DISPATCH_ID"
    if _is_session_already_dispatched(ledger, work_id, reviewer_session_id):
        return False, "DUPLICATE_REVIEWER_SESSION"
    return True, ""


def record_dispatch(
    control_dir: Path | str,
    work_id: str,
    dispatch_id: str,
    family: str,
    reviewer_session_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    if family not in VALID_FAMILIES:
        return {"status": "refused", "reason_code": "INVALID_FAMILY"}
    if not (dispatch_id and reviewer_session_id and work_id):
        return {"status": "refused", "reason_code": "INVALID_DISPATCH_PARAMETERS"}

    cdir = Path(control_dir)
    status_file = cdir / "coordinator-status.json"
    doc: dict[str, Any] = {}
    if status_file.exists():
        try:
            doc = json.loads(status_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    ok, err = _check_dispatch_role_and_uniqueness(doc, work_id, dispatch_id, reviewer_session_id)
    if not ok:
        return {"status": "refused", "reason_code": err}

    dc = doc.setdefault("delivery_control", {})
    ledger = dc.setdefault("dispatch_ledger", {})
    entry = {
        "dispatch_id": dispatch_id,
        "work_id": work_id,
        "family": family,
        "reviewer_session_id": reviewer_session_id,
        "session_id": reviewer_session_id,
        "dispatched_at": iso_now,
    }
    ledger[dispatch_id] = entry
    status_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return {"status": "dispatched", "dispatch": entry}


def verify_reviewer_uniqueness(
    reviews: list[dict[str, Any]],
    family: str,
    session_id: str,
    dispatch_id: str,
) -> tuple[bool, str]:
    for r in reviews:
        if r.get("family") == family:
            return False, "DUPLICATE_REVIEWER_FAMILY"
        r_sess = r.get("reviewer_session_id") or r.get("session_id")
        if r_sess == session_id:
            return False, "DUPLICATE_REVIEWER_SESSION"
        if r.get("dispatch_id") and r.get("dispatch_id") == dispatch_id:
            return False, "DUPLICATE_DISPATCH_ID"
    return True, ""


def build_review_receipt(
    work_id: str,
    verdict: str,
    commit_sha: str,
    tree_sha: str,
    family: str,
    model: str,
    session_id: str,
    dispatch_id: str,
    findings: list[dict[str, Any]],
    report_path: Path | str,
    iso_now: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "status": "recorded",
        "verdict": verdict,
        "work_id": work_id,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "revision": commit_sha,
        "head": commit_sha,
        "reviewer_family": family,
        "family": family,
        "reviewer_model": model,
        "model": model,
        "reviewer_session_id": session_id,
        "session_id": session_id,
        "dispatch_id": dispatch_id,
        "findings": findings,
        "report_path": str(report_path),
        "report_sha256": str(extra.get("report_sha256", "")),
        "observed_at": iso_now,
    }
    receipt.update(extra)
    return receipt


def _load_report_json(report_path: Path | str, report_sha256: str) -> tuple[bool, str, dict[str, Any]]:
    p = Path(report_path)
    if not p.exists() or not p.is_file():
        return False, "ARTIFACT_NOT_FOUND", {}
    if sha256_file(p).lower() != report_sha256.lower():
        return False, "ARTIFACT_HASH_MISMATCH", {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False, "REPORT_INVALID", {}
        return True, "", data
    except (json.JSONDecodeError, OSError):
        return False, "REPORT_INVALID", {}


def _check_review_preconditions(
    wi: dict[str, Any],
    session_id: str,
    reviewer_id: str,
    model: str,
    rep_data: dict[str, Any],
    cand_commit: str,
    cand_tree: str,
) -> tuple[bool, str]:
    if not (is_hex_40(cand_commit) and is_hex_40(cand_tree)):
        return False, "MISSING_CANDIDATE_REVISION"
    if rep_data["commit_sha"] != cand_commit or rep_data["tree_sha"] != cand_tree:
        return False, "REVISION_MISMATCH"
    claim = wi.get("claim", {})
    if session_id == claim.get("session_id") or reviewer_id == claim.get("owner") or "flash" in model.lower():
        return False, "SELF_REVIEW_FORBIDDEN"
    return True, ""


def _apply_review_state(
    wi: dict[str, Any],
    p_row: dict[str, Any],
    verdict: str,
    findings: list[dict[str, Any]],
    cand_commit: str,
    cand_tree: str,
    dispatch_ledger: dict[str, Any],
    work_id: str,
) -> dict[str, Any]:
    if verdict == "REVISE":
        wi["stage"] = "implementing"
        wi["remediation_required"] = True
        wi["defects"] = findings
        wi["qualified"] = False
        p_row["stage"] = "implementing"
        p_row["next_action"] = f"Remediate defects for {work_id}"
        return {"stage": "implementing", "remediation_required": True, "defects": findings}

    qualified = check_qualification(wi.get("reviews", []), cand_commit, cand_tree, dispatch_ledger, work_id=work_id)
    wi["qualified"] = qualified
    if qualified:
        wi["remediation_required"] = False
        wi["defects"] = []
        wi["stage"] = "merge_ready"
        p_row["stage"] = "merge_ready"
        p_row["next_action"] = f"Merge candidate {cand_commit[:8]}"
    return {"stage": wi.get("stage", "review"), "qualified": qualified}


def _validate_report_match(
    rep_data: dict[str, Any],
    family: str,
    model: str,
    verdict: str,
    session_id: str,
    reviewer_id: str,
    iso_now: str,
) -> dict[str, Any] | None:
    if rep_data["family"] != family or rep_data["model"] != model:
        return {
            "status": "refused",
            "reason_code": "REPORT_IDENTITY_MISMATCH",
            "extra": {"reviewer_id": reviewer_id},
            "observed_at": iso_now,
        }
    if rep_data["verdict"] != verdict:
        return {
            "status": "refused",
            "reason_code": "REPORT_VERDICT_MISMATCH",
            "extra": {"reviewer_id": reviewer_id},
            "observed_at": iso_now,
        }
    if rep_data["reviewer_session_id"] != session_id:
        return {
            "status": "refused",
            "reason_code": "REVIEWER_SESSION_MISMATCH",
            "extra": {"reviewer_id": reviewer_id},
            "observed_at": iso_now,
        }
    return None


def _load_work_item_and_ledger(
    cdir: Path,
    work_id: str,
    reviewer_id: str,
    iso_now: str,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any], dict[str, Any], Path]:
    status_file = cdir / "coordinator-status.json"
    doc: dict[str, Any] = {}
    if status_file.exists():
        try:
            doc = json.loads(status_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    dc = doc.setdefault("delivery_control", {})
    work_items = dc.setdefault("work_items", {})
    if work_id not in work_items:
        return (
            False,
            {
                "status": "refused",
                "reason_code": "WORK_NOT_ADMITTED",
                "extra": {"reviewer_id": reviewer_id},
                "observed_at": iso_now,
            },
            doc,
            {},
            status_file,
        )
    return True, None, doc, work_items[work_id], status_file


def _write_review_receipt_file(
    cdir: Path,
    doc: dict[str, Any],
    wi: dict[str, Any],
    work_id: str,
    family: str,
    model: str,
    session_id: str,
    dispatch_id: str,
    verdict: str,
    cand_commit: str,
    cand_tree: str,
    findings: list[dict[str, Any]],
    report_path: Path | str,
    status_file: Path,
    iso_now: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    receipt = build_review_receipt(
        work_id,
        verdict,
        cand_commit,
        cand_tree,
        family,
        model,
        session_id,
        dispatch_id,
        findings,
        report_path,
        iso_now,
        extra,
    )
    rec_dir = cdir / "receipts"
    rec_dir.mkdir(parents=True, exist_ok=True)
    rec_path = rec_dir / f"review_{work_id}_{family}_{session_id}.json"
    rec_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    receipt["receipt_path"] = str(rec_path)
    status_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return receipt


def _load_and_validate_review_report(
    report_path: Path | str,
    report_sha256: str,
    family: str,
    model: str,
    verdict: str,
    session_id: str,
    reviewer_id: str,
    iso_now: str,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any]]:
    loaded_ok, loaded_err, raw_data = _load_report_json(report_path, report_sha256)
    if not loaded_ok:
        return (
            False,
            {
                "status": "refused",
                "reason_code": loaded_err,
                "extra": {"reviewer_id": reviewer_id},
                "observed_at": iso_now,
            },
            {},
        )

    val_ok, val_err, rep_data = validate_review_receipt(raw_data)
    if not val_ok:
        return (
            False,
            {
                "status": "refused",
                "reason_code": val_err,
                "extra": {"reviewer_id": reviewer_id},
                "observed_at": iso_now,
            },
            {},
        )

    match_err = _validate_report_match(rep_data, family, model, verdict, session_id, reviewer_id, iso_now)
    if match_err:
        return False, match_err, {}
    return True, None, rep_data


def record_review(
    control_dir: Path | str,
    work_id: str,
    reviewer_id: str,
    model: str,
    family: str,
    session_id: str,
    verdict: str,
    report_path: Path | str,
    report_sha256: str,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    rep_ok, rep_ref, rep_data = _load_and_validate_review_report(
        report_path, report_sha256, family, model, verdict, session_id, reviewer_id, iso_now
    )
    if not rep_ok and rep_ref is not None:
        return rep_ref

    cdir = Path(control_dir)
    loaded_wi, wi_ref, doc, wi, status_file = _load_work_item_and_ledger(cdir, work_id, reviewer_id, iso_now)
    if not loaded_wi and wi_ref is not None:
        return wi_ref

    cand = wi.get("candidate", {})
    cand_commit = str(wi.get("commit_sha") or cand.get("commit_sha") or "")
    cand_tree = str(wi.get("tree_sha") or cand.get("tree_sha") or "")
    pre_ok, pre_err = _check_review_preconditions(wi, session_id, reviewer_id, model, rep_data, cand_commit, cand_tree)
    if not pre_ok:
        return {
            "status": "refused",
            "reason_code": pre_err,
            "extra": {"reviewer_id": reviewer_id},
            "observed_at": iso_now,
        }

    dispatch_id = rep_data["dispatch_id"]
    ledger = doc.setdefault("delivery_control", {}).setdefault("dispatch_ledger", {})
    disp_ok, disp_err = verify_dispatch_binding(ledger, dispatch_id, work_id, family, session_id)
    if not disp_ok:
        return {
            "status": "refused",
            "reason_code": disp_err,
            "extra": {"reviewer_id": reviewer_id},
            "observed_at": iso_now,
        }

    reviews = wi.setdefault("reviews", [])
    uniq_ok, uniq_err = verify_reviewer_uniqueness(reviews, family, session_id, dispatch_id)
    if not uniq_ok:
        return {
            "status": "refused",
            "reason_code": uniq_err,
            "extra": {"reviewer_id": reviewer_id},
            "observed_at": iso_now,
        }

    findings = rep_data.get("findings", [])
    reviews.append(
        {
            "reviewer_id": reviewer_id,
            "family": family,
            "reviewer_family": family,
            "model": model,
            "reviewer_model": model,
            "reviewer_session_id": session_id,
            "session_id": session_id,
            "dispatch_id": dispatch_id,
            "verdict": verdict,
            "commit_sha": cand_commit,
            "tree_sha": cand_tree,
            "findings": findings,
            "report_path": str(report_path),
            "report_sha256": report_sha256,
            "observed_at": iso_now,
        }
    )

    project = wi.get("repo", "").split("/")[-1]
    p_row = doc.setdefault("delivery_control", {}).setdefault("projects", {}).setdefault(project, {})
    p_row["last_progress_at"] = iso_now
    extra = _apply_review_state(wi, p_row, verdict, findings, cand_commit, cand_tree, ledger, work_id)
    extra["report_sha256"] = report_sha256
    extra["reviewer_id"] = reviewer_id
    return _write_review_receipt_file(
        cdir,
        doc,
        wi,
        work_id,
        family,
        model,
        session_id,
        dispatch_id,
        verdict,
        cand_commit,
        cand_tree,
        findings,
        report_path,
        status_file,
        iso_now,
        extra,
    )
