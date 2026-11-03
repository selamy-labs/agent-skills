#!/usr/bin/env python3
"""Guarded stage transitions and live provider CI enforcement module."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from admission import verify_mandatory_gates
from providers import (
    ALLOWED_MERGE_IDENTITIES,
    fetch_ci_checks_from_provider,
    fetch_commit_check_runs_from_provider,
    fetch_pr_base_branch,
    has_app_pinned_checks,
    is_hex_40,
    is_hex_64,
    iso_timestamp,
    reconcile_and_complete_spec_task,
    reconcile_and_merge_pr,
    reconcile_and_publish_pr,
    reconcile_and_push,
    resolve_required_checks,
    sha256_file,
    verify_live_status_rollup,
)
from receipts import check_qualification, validate_review_receipt

VALID_ACTIONS = {"push", "publish_pr", "merge_pr", "complete_spec_task"}


def _verify_live_approval(approval_ref: Any, control_dir: Path | str | None = None) -> tuple[bool, str]:
    if not approval_ref:
        return False, "ABSENT_APPROVAL"
    path_str = approval_ref.get("path") if isinstance(approval_ref, dict) else str(approval_ref)
    expected_sha = approval_ref.get("sha256") if isinstance(approval_ref, dict) else None
    p = Path(path_str)
    if not p.is_absolute() and control_dir:
        p = Path(control_dir) / p
    if not p.exists():
        return False, "APPROVAL_FILE_NOT_FOUND"
    try:
        content = p.read_bytes()
    except OSError:
        return False, "APPROVAL_READ_FAILED"
    if b"[WITHDRAWN]" in content:
        return False, "APPROVAL_WITHDRAWN"
    if expected_sha and sha256_file(p).lower() != expected_sha.lower():
        return False, "APPROVAL_DIGEST_MISMATCH"
    return True, ""


def _guard_approval_and_defects(wi: dict[str, Any], control_dir: Path | str | None = None) -> tuple[bool, str]:
    if not wi.get("approval_ref"):
        return False, "ABSENT_APPROVAL"
    if wi.get("remediation_required") or wi.get("defects"):
        return False, "UNRESOLVED_REVISE_FINDINGS"
    return _verify_live_approval(wi.get("approval_ref"), control_dir)


def _guard_claim_freshness(wi: dict[str, Any], claim_token: str | None, now_dt: datetime.datetime) -> tuple[bool, str]:
    if not claim_token:
        return False, "MISSING_OR_EXPIRED_CLAIM"
    claim = wi.get("claim")
    if not claim or claim.get("claim_token") != claim_token:
        return False, "STALE_CLAIM"
    exp_str = claim.get("expires_at")
    if not exp_str:
        return False, "CLAIM_EXPIRED"
    try:
        exp_dt = datetime.datetime.fromisoformat(exp_str)
        if exp_dt.tzinfo is None and now_dt.tzinfo is not None:
            exp_dt = exp_dt.replace(tzinfo=datetime.UTC)
        elif exp_dt.tzinfo is not None and now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=datetime.UTC)
        if exp_dt <= now_dt:
            return False, "CLAIM_EXPIRED"
    except (ValueError, TypeError):
        return False, "CLAIM_EXPIRED"
    return True, ""


def _matches_report_fields(
    rep_data: dict[str, Any],
    family: str,
    session_id: str,
    commit_sha: str,
    tree_sha: str,
    dispatch_id: str,
) -> bool:
    if rep_data.get("verdict") != "SHIP":
        return False
    if rep_data.get("commit_sha") != commit_sha or rep_data.get("tree_sha") != tree_sha:
        return False
    if rep_data.get("family") != family:
        return False
    rep_sess = rep_data.get("reviewer_session_id")
    if rep_sess != session_id:
        return False
    if not dispatch_id or not dispatch_id.strip() or rep_data.get("dispatch_id") != dispatch_id:
        return False
    return True


def _check_on_disk_report(
    rep_path_str: str | None,
    expected_sha256: str | None,
    control_dir: Path | str | None,
    family: str,
    session_id: str,
    commit_sha: str,
    tree_sha: str,
    dispatch_id: str,
) -> bool:
    if not rep_path_str:
        return False
    if not expected_sha256 or not is_hex_64(expected_sha256):
        return False
    rep_p = Path(rep_path_str)
    if not rep_p.is_absolute() and control_dir:
        rep_p = Path(control_dir) / rep_p
    if not rep_p.is_file():
        return False
    if sha256_file(rep_p).lower() != expected_sha256.lower():
        return False
    try:
        rep_data = json.loads(rep_p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    val_ok, _, _ = validate_review_receipt(rep_data)
    if not val_ok:
        return False
    return _matches_report_fields(rep_data, family, session_id, commit_sha, tree_sha, dispatch_id)


def _matches_receipt_fields(
    data: dict[str, Any],
    family: str,
    session_id: str,
    commit_sha: str,
    tree_sha: str,
    dispatch_id: str,
) -> bool:
    if data.get("verdict") != "SHIP":
        return False
    rep_sha = data.get("report_sha256")
    if not rep_sha or not is_hex_64(str(rep_sha)):
        return False
    rec_sess = data.get("reviewer_session_id")
    rec_fam = data.get("family")
    if rec_fam != family or rec_sess != session_id:
        return False
    if not dispatch_id or not dispatch_id.strip() or data.get("dispatch_id") != dispatch_id:
        return False
    r_commit = data.get("commit_sha")
    r_tree = data.get("tree_sha")
    return r_commit == commit_sha and r_tree == tree_sha


def _check_receipt_file_on_disk(
    control_dir: Path | str,
    work_id: str,
    family: str,
    session_id: str,
    commit_sha: str,
    tree_sha: str,
    dispatch_id: str = "",
    expected_report_sha256: str | None = None,
    expected_report_path: str | None = None,
) -> bool:
    rec_path = Path(control_dir) / "receipts" / f"review_{work_id}_{family}_{session_id}.json"
    if not rec_path.exists():
        return False
    try:
        data = json.loads(rec_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    if not _matches_receipt_fields(data, family, session_id, commit_sha, tree_sha, dispatch_id):
        return False
    rep_sha = data.get("report_sha256")
    if not rep_sha or not is_hex_64(str(rep_sha)):
        return False
    if expected_report_sha256 and str(expected_report_sha256).lower() != str(rep_sha).lower():
        return False
    if expected_report_path and str(expected_report_path) != str(data.get("report_path")):
        return False
    return _check_on_disk_report(
        data.get("report_path"),
        str(rep_sha),
        control_dir,
        family,
        session_id,
        commit_sha,
        tree_sha,
        dispatch_id,
    )


def _verify_ship_disk_receipt(
    control_dir: Path | str,
    work_id: str,
    r: dict[str, Any],
    commit_sha: str,
    tree_sha: str,
) -> bool:
    fam = str(r.get("family", ""))
    sess = str(r.get("reviewer_session_id", ""))
    disp = str(r.get("dispatch_id", ""))
    exp_sha = str(r.get("report_sha256") or "").strip()
    exp_path = str(r.get("report_path") or "").strip()
    if not exp_sha or not is_hex_64(exp_sha):
        return False
    if not exp_path:
        return False
    return _check_receipt_file_on_disk(
        control_dir,
        work_id,
        fam,
        sess,
        commit_sha,
        tree_sha,
        dispatch_id=disp,
        expected_report_sha256=exp_sha,
        expected_report_path=exp_path,
    )


def _check_single_ship_match(
    r: dict[str, Any],
    commit_sha: str,
    tree_sha: str,
    control_dir: Path | str | None,
    work_id: str,
) -> bool:
    if r.get("commit_sha") != commit_sha:
        return False
    if r.get("tree_sha") != tree_sha:
        return False
    if not (control_dir and work_id):
        return True
    return _verify_ship_disk_receipt(control_dir, work_id, r, commit_sha, tree_sha)


def _check_ship_reviews_match(
    reviews: list[dict[str, Any]],
    commit_sha: str,
    tree_sha: str,
    control_dir: Path | str | None = None,
    work_id: str = "",
    dispatch_ledger: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if not check_qualification(reviews, commit_sha, tree_sha, dispatch_ledger, work_id=work_id):
        return False, "MISSING_QUALIFICATION"
    ship_revs = [r for r in reviews if r.get("verdict") == "SHIP"]
    if len(ship_revs) < 3:
        return False, "MISSING_QUALIFICATION"
    if not (is_hex_40(commit_sha) and is_hex_40(tree_sha)):
        return False, "STALE_REVIEW_REVISION"
    for r in ship_revs:
        if not _check_single_ship_match(r, commit_sha, tree_sha, control_dir, work_id):
            return False, "STALE_REVIEW_REVISION"
    return True, ""


def _check_spec_task_prerequisite(wi: dict[str, Any], cand_commit: str, cand_tree: str) -> bool:
    cur_transitions = [
        t
        for t in wi.get("transitions", [])
        if t.get("commit_sha") == cand_commit and (not t.get("tree_sha") or t.get("tree_sha") == cand_tree)
    ]
    req_action = wi.get("required_completion_action") or "merge_pr"
    allowed_map = {
        "merge_pr": {"merge_pr"},
        "publish_pr": {"publish_pr", "merge_pr"},
        "push": {"push", "publish_pr", "merge_pr"},
    }
    allowed = allowed_map.get(req_action, {"merge_pr"})
    return any(t.get("action") in allowed for t in cur_transitions)


def _guard_pre_transition(
    wi: dict[str, Any],
    action: str,
    claim_token: str | None,
    now_dt: datetime.datetime,
    control_dir: Path | str | None = None,
    dispatch_ledger: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    ok, err = _guard_approval_and_defects(wi, control_dir)
    if not ok:
        return False, err
    ok, err = _guard_claim_freshness(wi, claim_token, now_dt)
    if not ok:
        return False, err
    if wi.get("qualified") is not True:
        return False, "MISSING_QUALIFICATION"
    cand = wi.get("candidate", {})
    cand_commit = str(wi.get("commit_sha") or cand.get("commit_sha") or "")
    cand_tree = str(wi.get("tree_sha") or cand.get("tree_sha") or "")
    ok, err = _check_ship_reviews_match(
        wi.get("reviews", []),
        cand_commit,
        cand_tree,
        control_dir=control_dir,
        work_id=str(wi.get("work_id") or ""),
        dispatch_ledger=dispatch_ledger,
    )
    if not ok:
        return False, err
    if not verify_mandatory_gates(cand.get("gate_results")):
        return False, "MISSING_MANDATORY_GATE"
    if action == "complete_spec_task" and not _check_spec_task_prerequisite(wi, cand_commit, cand_tree):
        return False, "MISSING_TRANSITION_EVIDENCE"
    return True, ""


def _guard_live_provider_ci(
    pr_ref: str,
    commit_sha: str,
    command_runner: Callable[..., Any] | None,
    wt_path: Path,
    repo: str = "",
    base_branch: str = "main",
    control_dir: Path | str | None = None,
    required_checks: list[Any] | None = None,
) -> tuple[bool, str]:
    if not pr_ref:
        return False, "CI_CHECK_FAILED"
    req_checks = required_checks
    if req_checks is None:
        req_checks = resolve_required_checks(
            repo,
            base_branch=base_branch,
            runner=command_runner,
            cwd=wt_path,
            control_dir=control_dir,
        )
    if not req_checks:
        return False, "CI_CHECK_MISSING"
    live_checks = fetch_ci_checks_from_provider(repo, pr_ref, commit_sha, runner=command_runner, cwd=wt_path)
    if live_checks is None:
        return False, "MISSING_EXACT_HEAD_CI"
    if not live_checks:
        return False, "CI_CHECK_MISSING"
    check_runs = None
    if has_app_pinned_checks(req_checks):
        check_runs = fetch_commit_check_runs_from_provider(repo, commit_sha, runner=command_runner, cwd=wt_path)
        if check_runs is None:
            return False, "MISSING_EXACT_HEAD_CI"
    v_ok, v_err = verify_live_status_rollup(live_checks, commit_sha, required_checks=req_checks, check_runs=check_runs)
    if not v_ok:
        return False, v_err
    return True, ""


def _dispatch_action(
    action: str,
    wt_path: Path,
    target_branch: str,
    base_branch: str,
    commit_sha: str,
    pr_ref: str,
    title: str,
    body: str,
    spec_path: Path | str | None,
    task_id: str,
    command_runner: Callable[..., Any] | None,
    merge_identity: str = "selamy-labs-bot",
    remote: str = "origin",
    required_checks: list[Any] | None = None,
    repo: str = "",
) -> tuple[bool, dict[str, Any]]:
    if action == "push":
        return reconcile_and_push(wt_path, remote, target_branch, commit_sha, runner=command_runner)
    if action == "publish_pr":
        return reconcile_and_publish_pr(
            wt_path, target_branch, base_branch, commit_sha, title, body, runner=command_runner, remote=remote
        )
    if action == "merge_pr":
        return reconcile_and_merge_pr(
            wt_path,
            pr_ref,
            commit_sha,
            merge_identity=merge_identity,
            runner=command_runner,
            required_checks=required_checks,
            repo=repo,
            expected_base_branch=base_branch,
        )
    if action == "complete_spec_task":
        return reconcile_and_complete_spec_task(spec_path, task_id)
    return False, {"error": f"Unknown action {action}", "reason_code": "INVALID_ACTION"}


def _record_transition_outcome(
    doc: dict[str, Any],
    wi: dict[str, Any],
    action: str,
    commit_sha: str,
    receipt_path: str,
    iso_now: str,
) -> None:
    trans = wi.setdefault("transitions", [])
    trans.append(
        {
            "action": action,
            "commit_sha": commit_sha,
            "tree_sha": wi.get("tree_sha", ""),
            "receipt_path": receipt_path,
            "transitioned_at": iso_now,
        }
    )
    project = wi.get("repo", "").split("/")[-1]
    p_row = doc.get("delivery_control", {}).get("projects", {}).setdefault(project, {})
    p_row["last_progress_at"] = iso_now
    if action == "complete_spec_task":
        wi["stage"] = "done"
        p_row["stage"] = "done"
        p_row["next_action"] = "Delivery complete"
    elif action == "merge_pr":
        wi["stage"] = "ci"
        p_row["stage"] = "ci"
        p_row["next_action"] = f"Complete spec task for {wi.get('work_id')}"
    elif action == "publish_pr":
        wi["stage"] = "merge_ready"
        p_row["stage"] = "merge_ready"
        p_row["next_action"] = f"Merge PR for {wi.get('work_id')}"
    elif action == "push":
        wi["stage"] = "review"
        p_row["stage"] = "review"
        p_row["next_action"] = f"Publish PR for {wi.get('work_id')}"


def _validate_transition_action(
    action: str,
    merge_identity: str,
    iso_now: str,
) -> dict[str, Any] | None:
    if action not in VALID_ACTIONS:
        return {"status": "refused", "reason_code": "INVALID_ACTION", "observed_at": iso_now}
    if action == "merge_pr" and merge_identity not in ALLOWED_MERGE_IDENTITIES:
        return {"status": "refused", "reason_code": "DISALLOWED_MERGE_IDENTITY", "observed_at": iso_now}
    return None


def _validate_transition_args(
    action: str,
    target_branch: str,
    base_branch: str,
    merge_identity: str,
    iso_now: str,
) -> dict[str, Any] | None:
    act_err = _validate_transition_action(action, merge_identity, iso_now)
    if act_err:
        return act_err
    if action == "push" and target_branch in {"main", "master", base_branch}:
        return {"status": "refused", "reason_code": "PROTECTED_BRANCH_PUSH", "observed_at": iso_now}
    return None


def _guard_action_ci(
    action: str,
    commit_sha: str,
    command_runner: Callable[..., Any] | None,
    wt_path: Path,
    pr_ref: str,
    iso_now: str,
    repo: str = "",
    expected_base_branch: str = "main",
    control_dir: Path | str | None = None,
) -> tuple[bool, dict[str, Any] | None, list[Any] | None]:
    if action in {"complete_spec_task", "merge_pr"}:
        if not pr_ref:
            return False, {"status": "refused", "reason_code": "CI_CHECK_FAILED", "observed_at": iso_now}, None
        pr_base = fetch_pr_base_branch(repo, pr_ref, runner=command_runner, cwd=wt_path)
        if not pr_base:
            return False, {"status": "refused", "reason_code": "PR_VIEW_FAILED", "observed_at": iso_now}, None
        if pr_base != expected_base_branch:
            return False, {"status": "refused", "reason_code": "BASE_BRANCH_MISMATCH", "observed_at": iso_now}, None
        target_base = pr_base
        req_checks = resolve_required_checks(
            repo,
            base_branch=target_base,
            runner=command_runner,
            cwd=wt_path,
            control_dir=control_dir,
        )
        ci_ok, ci_code = _guard_live_provider_ci(
            pr_ref,
            commit_sha,
            command_runner,
            wt_path,
            repo=repo,
            required_checks=req_checks,
        )
        if not ci_ok:
            return (
                False,
                {
                    "status": "refused",
                    "reason_code": ci_code,
                    "observed_at": iso_now,
                },
                None,
            )
        return True, None, req_checks
    return True, None, None


def _write_transition_receipt(
    cdir: Path,
    doc: dict[str, Any],
    wi: dict[str, Any],
    work_id: str,
    action: str,
    commit_sha: str,
    act_res: dict[str, Any],
    status_file: Path,
    iso_now: str,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "status": "transitioned",
        "action": action,
        "work_id": work_id,
        "commit_sha": commit_sha,
        "reconciled_existing_effect": act_res.get("reconciled_existing_effect", False),
        "provider_result": act_res,
        "observed_at": iso_now,
    }
    if "merge_commit_sha" in act_res:
        receipt["merge_commit_sha"] = act_res["merge_commit_sha"]

    rec_dir = cdir / "receipts"
    rec_dir.mkdir(parents=True, exist_ok=True)
    rec_path = rec_dir / f"transition_{work_id}_{action}.json"
    rec_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    receipt["receipt_path"] = str(rec_path)
    _record_transition_outcome(doc, wi, action, commit_sha, str(rec_path), iso_now)
    status_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return receipt


def _load_and_guard_transition_item(
    control_dir: Path | str,
    work_id: str,
    action: str,
    claim_token: str | None,
    now_dt: datetime.datetime,
    iso_now: str,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any], dict[str, Any], Path]:
    cdir = Path(control_dir)
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
            {"status": "refused", "reason_code": "WORK_NOT_ADMITTED", "observed_at": iso_now},
            doc,
            {},
            status_file,
        )
    wi = work_items[work_id]

    ledger = dc.get("dispatch_ledger")
    if not isinstance(ledger, dict) or not ledger:
        return (
            False,
            {"status": "refused", "reason_code": "MISSING_QUALIFICATION", "observed_at": iso_now},
            doc,
            wi,
            status_file,
        )
    g_ok, g_err = _guard_pre_transition(
        wi, action, claim_token, now_dt, control_dir=control_dir, dispatch_ledger=ledger
    )
    if not g_ok:
        return False, {"status": "refused", "reason_code": g_err, "observed_at": iso_now}, doc, wi, status_file
    return True, None, doc, wi, status_file


def _dispatch_and_record_transition(
    cdir: Path,
    doc: dict[str, Any],
    wi: dict[str, Any],
    work_id: str,
    action: str,
    commit_sha: str,
    pr_ref: str,
    params: tuple[str, str, str, str],
    command_runner: Callable[..., Any] | None,
    merge_identity: str,
    remote: str,
    status_file: Path,
    iso_now: str,
    required_checks: list[str] | None = None,
    repo: str = "",
) -> dict[str, Any]:
    target_branch, base_branch, title, body = params
    wt_path = Path(wi.get("claim", {}).get("worktree", "."))
    act_ok, act_res = _dispatch_action(
        action,
        wt_path,
        target_branch,
        base_branch,
        commit_sha,
        pr_ref,
        title,
        body,
        wi.get("spec_path"),
        wi.get("task_id", ""),
        command_runner,
        merge_identity=merge_identity,
        remote=remote,
        required_checks=required_checks,
        repo=repo,
    )
    if not act_ok:
        reason = act_res.get("reason_code", "PROVIDER_EXECUTION_FAILED")
        return {
            "status": "refused",
            "reason_code": reason,
            "error": act_res.get("error", "Execution failed"),
            "observed_at": iso_now,
        }
    return _write_transition_receipt(cdir, doc, wi, work_id, action, commit_sha, act_res, status_file, iso_now)


def _format_pr_text(wi: dict[str, Any], work_id: str, pr_title: str | None, pr_body: str | None) -> tuple[str, str]:
    title = pr_title or f"feat: {wi.get('task_id', work_id)}"
    body = pr_body or f"Resolves {wi.get('repo')}#{wi.get('issue_num')} task {wi.get('task_id')}"
    return title, body


def execute_transition(
    control_dir: Path | str,
    work_id: str,
    action: str,
    claim_token: str | None = None,
    remote: str = "origin",
    branch: str | None = None,
    pr_number: int | str | None = None,
    pr_title: str | None = None,
    pr_body: str | None = None,
    merge_identity: str = "selamy-labs-bot",
    command_runner: Callable[..., Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    act_err = _validate_transition_action(action, merge_identity, iso_now)
    if act_err:
        return act_err

    now_dt = datetime.datetime.fromisoformat(iso_now)
    ok, ref, doc, wi, status_file = _load_and_guard_transition_item(
        control_dir, work_id, action, claim_token, now_dt, iso_now
    )
    if ref:
        return ref

    expected_base_branch = str(wi.get("base_branch") or "main")
    target_branch = branch or f"feat/{work_id}"
    arg_err = _validate_transition_args(action, target_branch, expected_base_branch, merge_identity, iso_now)
    if arg_err:
        return arg_err

    cand = wi.get("candidate", {})
    commit_sha = str(wi.get("commit_sha") or cand.get("commit_sha") or "")
    wt_path = Path(wi.get("claim", {}).get("worktree", "."))
    pr_val = pr_number if pr_number is not None else wi.get("pr_number")
    pr_ref = str(pr_val if pr_val is not None else (target_branch or "")).strip()
    repo = str(wi.get("repo") or "").strip()

    _, ci_ref, req_checks = _guard_action_ci(
        action,
        commit_sha,
        command_runner,
        wt_path,
        pr_ref,
        iso_now,
        repo=repo,
        expected_base_branch=expected_base_branch,
        control_dir=control_dir,
    )
    if ci_ref:
        return ci_ref

    title, body = _format_pr_text(wi, work_id, pr_title, pr_body)
    return _dispatch_and_record_transition(
        Path(control_dir),
        doc,
        wi,
        work_id,
        action,
        commit_sha,
        pr_ref,
        (target_branch, expected_base_branch, title, body),
        command_runner,
        merge_identity,
        remote,
        status_file,
        iso_now,
        required_checks=req_checks,
        repo=repo,
    )
