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
    is_hex_40,
    iso_timestamp,
    reconcile_and_complete_spec_task,
    reconcile_and_merge_pr,
    reconcile_and_publish_pr,
    reconcile_and_push,
    sha256_file,
)

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


def _check_receipt_file_on_disk(
    control_dir: Path | str,
    work_id: str,
    family: str,
    session_id: str,
    commit_sha: str,
    tree_sha: str,
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
    r_commit = data.get("commit_sha") or data.get("revision")
    r_tree = data.get("tree_sha")
    return r_commit == commit_sha and r_tree == tree_sha


def _check_single_ship_match(
    r: dict[str, Any],
    commit_sha: str,
    tree_sha: str,
    control_dir: Path | str | None,
    work_id: str,
) -> bool:
    r_commit = r.get("commit_sha") or r.get("revision")
    r_tree = r.get("tree_sha")
    if r_commit != commit_sha or r_tree != tree_sha:
        return False
    if control_dir and work_id:
        fam = str(r.get("family") or r.get("reviewer_family") or "")
        sess = str(r.get("reviewer_session_id") or r.get("session_id") or "")
        return _check_receipt_file_on_disk(control_dir, work_id, fam, sess, commit_sha, tree_sha)
    return True


def _check_ship_reviews_match(
    reviews: list[dict[str, Any]],
    commit_sha: str,
    tree_sha: str,
    control_dir: Path | str | None = None,
    work_id: str = "",
) -> tuple[bool, str]:
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
    )
    if not ok:
        return False, err
    if not verify_mandatory_gates(cand.get("gate_results")):
        return False, "MISSING_MANDATORY_GATE"
    if action == "complete_spec_task" and not _check_spec_task_prerequisite(wi, cand_commit, cand_tree):
        return False, "MISSING_TRANSITION_EVIDENCE"
    return True, ""


def _check_project_row_ci(p_row: dict[str, Any], commit_sha: str) -> tuple[bool | None, list[dict[str, Any]] | None]:
    p_ci = p_row.get("ci")
    if not isinstance(p_ci, dict):
        return None, None
    rev = str(p_ci.get("revision") or p_ci.get("head_sha") or "").strip()
    if rev and rev != commit_sha:
        return None, None
    status = str(p_ci.get("status") or "").lower()
    ev = p_ci.get("evidence")
    checks = [dict(c) for c in ev if isinstance(c, dict)] if isinstance(ev, list) else []
    if status in {"failing", "failed", "failure"}:
        return False, checks
    if status in {"passing", "passed", "success"} and checks:
        return True, checks
    return None, None


def _is_ci_check_passing(c: Any, cand_sha: str) -> bool:
    if not isinstance(c, dict):
        return False
    c_sha = str(c.get("head_sha") or c.get("commit_sha") or c.get("sha") or "").strip()
    if c_sha and c_sha != cand_sha:
        return False
    status = str(c.get("status") or "").upper()
    concl = str(c.get("conclusion") or "").upper()
    state = str(c.get("state") or "").upper()
    if concl in {"FAILURE", "FAILED", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
        return False
    if state in {"FAILURE", "ERROR"}:
        return False
    return (status == "COMPLETED" and concl in {"SUCCESS", "NEUTRAL", "SKIPPED"}) or state == "SUCCESS"


def _evaluate_live_ci_checks(
    live_checks: list[dict[str, Any]], commit_sha: str
) -> tuple[bool, str, list[dict[str, Any]]]:
    if not live_checks:
        return False, "required checks are missing", []
    if not all(_is_ci_check_passing(c, commit_sha) for c in live_checks):
        return False, "exact-head provider required checks failed", live_checks
    return True, "", live_checks


def _evaluate_caller_ci_checks(ci_checks: Any, commit_sha: str) -> tuple[bool, str, list[dict[str, Any]]]:
    if isinstance(ci_checks, list) and ci_checks:
        if all(_is_ci_check_passing(c, commit_sha) for c in ci_checks):
            return True, "", ci_checks
        return False, "exact-head provider required checks failed", ci_checks
    return False, "required checks are missing", []


def _guard_spec_task_ci(
    doc: dict[str, Any],
    wi: dict[str, Any],
    commit_sha: str,
    ci_checks: Any,
    command_runner: Callable[..., Any] | None,
    wt_path: Path,
    pr_ref: str,
) -> tuple[bool, str, list[dict[str, Any]]]:
    project = wi.get("project") or wi.get("repo", "").split("/")[-1] or "default"
    p_row = doc.get("delivery_control", {}).get("projects", {}).get(project, {})
    p_res, p_checks = _check_project_row_ci(p_row, commit_sha)
    if p_res is False:
        return False, "provider CI status is failing", p_checks or []

    if pr_ref and command_runner is not None:
        live_checks = fetch_ci_checks_from_provider("", pr_ref, commit_sha, runner=command_runner, cwd=wt_path)
        if live_checks is not None:
            return _evaluate_live_ci_checks(live_checks, commit_sha)

    if p_res is True and p_checks:
        return True, "", p_checks

    return _evaluate_caller_ci_checks(ci_checks, commit_sha)


def _check_merge_pr_ci_failure(
    p_row: dict[str, Any],
    commit_sha: str,
    ci_checks: list[dict[str, Any]] | None,
    command_runner: Callable[..., Any] | None,
    wt_path: Path,
    pr_ref: str,
) -> tuple[bool, list[dict[str, Any]]]:
    p_res, p_checks = _check_project_row_ci(p_row, commit_sha)
    if p_res is False:
        return True, p_checks or []
    if pr_ref and command_runner is not None:
        live_checks = fetch_ci_checks_from_provider("", pr_ref, commit_sha, runner=command_runner, cwd=wt_path)
        if live_checks is not None:
            if not live_checks or not all(_is_ci_check_passing(c, commit_sha) for c in live_checks):
                return True, live_checks
    return False, []


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
    ci_checks: list[dict[str, Any]] | None,
    command_runner: Callable[..., Any] | None,
    merge_identity: str = "selamy-labs-bot",
    remote: str = "origin",
) -> tuple[bool, dict[str, Any]]:
    if action == "push":
        return reconcile_and_push(wt_path, remote, target_branch, commit_sha, runner=command_runner)
    if action == "publish_pr":
        return reconcile_and_publish_pr(
            wt_path, target_branch, base_branch, commit_sha, title, body, runner=command_runner, remote=remote
        )
    if action == "merge_pr":
        return reconcile_and_merge_pr(
            wt_path, pr_ref, commit_sha, merge_identity=merge_identity, ci_checks=ci_checks, runner=command_runner
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


def _validate_transition_args(
    action: str,
    target_branch: str,
    base_branch: str,
    merge_identity: str,
    iso_now: str,
) -> dict[str, Any] | None:
    if action not in VALID_ACTIONS:
        return {"status": "refused", "reason_code": "INVALID_ACTION", "observed_at": iso_now}
    if action == "push" and target_branch in {"main", "master", base_branch}:
        return {"status": "refused", "reason_code": "PROTECTED_BRANCH_PUSH", "observed_at": iso_now}
    if action == "merge_pr" and merge_identity not in ALLOWED_MERGE_IDENTITIES:
        return {"status": "refused", "reason_code": "DISALLOWED_MERGE_IDENTITY", "observed_at": iso_now}
    return None


def _guard_action_ci(
    action: str,
    doc: dict[str, Any],
    wi: dict[str, Any],
    commit_sha: str,
    ci_checks: list[dict[str, Any]] | None,
    command_runner: Callable[..., Any] | None,
    wt_path: Path,
    pr_ref: str,
    iso_now: str,
) -> tuple[bool, dict[str, Any] | None]:
    if action == "complete_spec_task":
        ci_ok, ci_err, _ = _guard_spec_task_ci(doc, wi, commit_sha, ci_checks, command_runner, wt_path, pr_ref)
        if not ci_ok:
            return False, {
                "status": "refused",
                "reason_code": "CI_CHECK_FAILED",
                "error": ci_err,
                "observed_at": iso_now,
            }
    if action == "merge_pr":
        project = wi.get("repo", "").split("/")[-1]
        p_row = doc.setdefault("delivery_control", {}).setdefault("projects", {}).setdefault(project, {})
        fail_ci, _ = _check_merge_pr_ci_failure(p_row, commit_sha, ci_checks, command_runner, wt_path, pr_ref)
        if fail_ci:
            return False, {
                "status": "refused",
                "reason_code": "CI_CHECK_FAILED",
                "error": "CI checks missing or failed",
                "observed_at": iso_now,
            }
    return True, None


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

    g_ok, g_err = _guard_pre_transition(wi, action, claim_token, now_dt, control_dir=control_dir)
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
    ci_checks: list[dict[str, Any]] | None,
    command_runner: Callable[..., Any] | None,
    merge_identity: str,
    remote: str,
    status_file: Path,
    iso_now: str,
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
        ci_checks,
        command_runner,
        merge_identity=merge_identity,
        remote=remote,
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
    base_branch: str = "main",
    pr_number: int | str | None = None,
    pr_title: str | None = None,
    pr_body: str | None = None,
    merge_identity: str = "selamy-labs-bot",
    ci_checks: list[dict[str, Any]] | None = None,
    command_runner: Callable[..., Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    now_dt = datetime.datetime.fromisoformat(iso_now)
    target_branch = branch or f"feat/{work_id}"
    arg_err = _validate_transition_args(action, target_branch, base_branch, merge_identity, iso_now)
    if arg_err:
        return arg_err

    ok, ref, doc, wi, status_file = _load_and_guard_transition_item(
        control_dir, work_id, action, claim_token, now_dt, iso_now
    )
    if ref:
        return ref

    cand = wi.get("candidate", {})
    commit_sha = str(wi.get("commit_sha") or cand.get("commit_sha") or "")
    wt_path = Path(wi.get("claim", {}).get("worktree", "."))
    pr_ref = str(pr_number or wi.get("issue_num") or target_branch)

    _, ci_ref = _guard_action_ci(action, doc, wi, commit_sha, ci_checks, command_runner, wt_path, pr_ref, iso_now)
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
        (target_branch, base_branch, title, body),
        ci_checks,
        command_runner,
        merge_identity,
        remote,
        status_file,
        iso_now,
    )
