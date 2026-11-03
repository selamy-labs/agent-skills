#!/usr/bin/env python3
"""Admission, claim, and implementation recording module."""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from providers import (
    inspect_git_commit,
    is_approved_identity,
    is_hex_40,
    iso_timestamp,
)

DEFAULT_ALLOWED_ROOTS = ["/tmp", "/home/pselamy", "/workspace"]
PROTECTED_SKILL_REL = "skills/portfolio-delivery-control"
MANDATORY_GATE_NAMES = {
    "pytest",
    "complexipy",
    "vulture",
    "ruff_check",
    "ruff_format",
    "privacy_scan",
    "security_scan",
    "lint_skills",
}


def paths_overlap(left: str, right: str) -> bool:
    l_clean = os.path.normpath(left.strip())
    r_clean = os.path.normpath(right.strip())
    if l_clean == r_clean:
        return True
    l_parts = l_clean.split(os.sep)
    r_parts = r_clean.split(os.sep)
    min_len = min(len(l_parts), len(r_parts))
    return l_parts[:min_len] == r_parts[:min_len]


def validate_worktree(
    worktree: Path | str,
    allowed_roots: list[str] | None = None,
) -> tuple[bool, str, Path]:
    roots = allowed_roots or DEFAULT_ALLOWED_ROOTS
    try:
        wt_path = Path(worktree).resolve()
    except (OSError, RuntimeError):
        return False, "INVALID_WORKTREE_PATH", Path(worktree)
    if not wt_path.exists() or not wt_path.is_dir():
        return False, "WORKTREE_NOT_FOUND", wt_path
    wt_str = str(wt_path)
    if not any(wt_str == r or wt_str.startswith(r.rstrip("/") + "/") for r in roots):
        return False, "WORKTREE_SCOPE_ESCAPE", wt_path
    git_dir = wt_path / ".git"
    if not git_dir.exists():
        return False, "NOT_A_GIT_WORKTREE", wt_path
    return True, "", wt_path


def validate_spec_task(spec_path: Path | str, task_id: str) -> tuple[bool, str]:
    sp = Path(spec_path)
    if not sp.exists() or not sp.is_file():
        return False, "SPEC_FILE_NOT_FOUND"
    content = sp.read_text(encoding="utf-8")
    pattern = re.compile(rf"^\s*-\s*\[([ xX])\]\s+{re.escape(task_id)}(?:\s|:|$)", re.MULTILINE)
    m = pattern.search(content)
    if not m:
        return False, "TASK_NOT_FOUND_IN_SPEC"
    if m.group(1).lower() == "x":
        return False, "TASK_ALREADY_COMPLETED"
    return True, ""


def _is_path_allowed(file_path: str, allowed_prefixes: list[str]) -> bool:
    norm = os.path.normpath(file_path)
    if norm.startswith("..") or norm.startswith("/"):
        return False
    return any(norm == p or norm.startswith(p.rstrip("/") + "/") for p in allowed_prefixes)


def inspect_modified_paths(
    modified_paths: list[str] | None,
    allowed_prefixes: list[str],
) -> tuple[bool, str]:
    if not modified_paths:
        return True, ""
    for p in modified_paths:
        if not _is_path_allowed(p, allowed_prefixes):
            return False, "PATH_SCOPE_ESCAPE"
    return True, ""


def _is_gate_passing(val: Any) -> bool:
    if isinstance(val, dict):
        return val.get("exit_code") == 0 and val.get("passed") is not False
    return val in (0, True, "PASS", "PASSED")


def _is_coverage_passing(cov: Any) -> bool:
    if isinstance(cov, dict):
        cov = cov.get("percent", 0)
    try:
        return float(cov) >= 90.0
    except (ValueError, TypeError):
        return False


def verify_mandatory_gates(gate_results: dict[str, Any] | None) -> bool:
    if not isinstance(gate_results, dict):
        return False
    for g in MANDATORY_GATE_NAMES:
        if g not in gate_results or not _is_gate_passing(gate_results[g]):
            return False
    return _is_coverage_passing(gate_results.get("coverage", 0))


def _is_claim_active(claim: dict[str, Any], now_dt: datetime.datetime) -> bool:
    exp_str = claim.get("expires_at")
    if not exp_str:
        return False
    try:
        return datetime.datetime.fromisoformat(exp_str) > now_dt
    except (ValueError, TypeError):
        return False


def _check_claim_conflict(
    wi: dict[str, Any],
    work_id: str,
    owner: str,
    session_id: str,
    wt_str: str,
    now_dt: datetime.datetime,
) -> tuple[bool, str]:
    claim = wi.get("claim")
    if not claim or not _is_claim_active(claim, now_dt):
        return True, ""
    if claim.get("owner") == owner and claim.get("session_id") == session_id:
        return True, ""
    if claim.get("session_id") == session_id and claim.get("owner") != owner:
        return False, "SESSION_REUSED_ACROSS_WRITERS"
    if claim.get("worktree") and paths_overlap(claim["worktree"], wt_str):
        return False, "WORKTREE_PATH_CONFLICT"
    return False, "WORK_ITEM_ALREADY_CLAIMED"


def _check_all_claims(
    work_items: dict[str, Any],
    work_id: str,
    owner: str,
    session_id: str,
    wt_str: str,
    now_dt: datetime.datetime,
) -> tuple[bool, str]:
    for wid, wi in work_items.items():
        ok, err = _check_claim_conflict(wi, wid, owner, session_id, wt_str, now_dt)
        if not ok:
            return False, err
    return True, ""


def admit_work(
    control_dir: Path | str,
    repo: str,
    issue_num: int | str,
    spec_path: Path | str,
    task_id: str,
    base_branch: str = "main",
    target_branch: str | None = None,
    allowed_roots: list[str] | None = None,
    priority: int = 1,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    sp_ok, sp_err = validate_spec_task(spec_path, task_id)
    if not sp_ok:
        return {"status": "refused", "reason_code": sp_err, "observed_at": iso_now}
    work_id = f"{repo.replace('/', '-')}-{issue_num}-{task_id}"
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
    if work_id in work_items:
        return {"status": "refused", "reason_code": "DUPLICATE_WORK_ADMISSION", "observed_at": iso_now}

    wi = {
        "work_id": work_id,
        "repo": repo,
        "issue_num": str(issue_num),
        "spec_path": str(spec_path),
        "task_id": task_id,
        "base_branch": base_branch,
        "target_branch": target_branch or f"feat/{task_id}",
        "allowed_roots": allowed_roots or DEFAULT_ALLOWED_ROOTS,
        "priority": priority,
        "stage": "ready",
        "admitted_at": iso_now,
        "qualified": False,
        "remediation_required": False,
        "reviews": [],
        "transitions": [],
    }
    work_items[work_id] = wi
    project = repo.split("/")[-1]
    p_row = dc.setdefault("projects", {}).setdefault(project, {})
    p_row["stage"] = "ready"
    p_row["next_action"] = f"Claim work item {work_id}"
    p_row["last_progress_at"] = iso_now
    status_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return {"status": "admitted", "work_id": work_id, "stage": "ready", "observed_at": iso_now}


def claim_work(
    control_dir: Path | str,
    work_id: str,
    owner: str,
    session_id: str,
    worktree: Path | str,
    ttl_seconds: int = 1800,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    now_dt = datetime.datetime.fromisoformat(iso_now)
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
        return {"status": "refused", "reason_code": "WORK_NOT_ADMITTED", "observed_at": iso_now}
    wi = work_items[work_id]

    wt_ok, wt_err, wt_path = validate_worktree(worktree, wi.get("allowed_roots"))
    if not wt_ok:
        return {"status": "refused", "reason_code": wt_err, "observed_at": iso_now}

    cf_ok, cf_err = _check_all_claims(work_items, work_id, owner, session_id, str(wt_path), now_dt)
    if not cf_ok:
        return {"status": "refused", "reason_code": cf_err, "observed_at": iso_now}

    exp_dt = now_dt + datetime.timedelta(seconds=ttl_seconds)
    token = f"claim-{secrets.token_hex(16)}"
    claim = {
        "claim_token": token,
        "owner": owner,
        "session_id": session_id,
        "worktree": str(wt_path),
        "claimed_at": iso_now,
        "expires_at": iso_timestamp(exp_dt),
    }
    wi["claim"] = claim
    wi["stage"] = "implementing"
    project = wi.get("repo", "").split("/")[-1]
    p_row = dc.setdefault("projects", {}).setdefault(project, {})
    p_row["stage"] = "implementing"
    p_row["next_action"] = f"Implement candidate for {work_id}"
    p_row["last_progress_at"] = iso_now
    status_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return {
        "status": "claimed",
        "work_id": work_id,
        "claim_token": token,
        "expires_at": claim["expires_at"],
        "observed_at": iso_now,
    }


def invalidate_prior_reviews_if_revision_changed(
    wi: dict[str, Any],
    commit_sha: str,
    tree_sha: str,
) -> None:
    cand = wi.get("candidate", {})
    old_commit = wi.get("commit_sha") or cand.get("commit_sha")
    old_tree = wi.get("tree_sha") or cand.get("tree_sha")
    if old_commit and (old_commit != commit_sha or old_tree != tree_sha):
        wi["reviews"] = []
        wi["qualified"] = False


def _verify_git_commit_and_tree(
    wt_path: Path,
    commit_sha: str,
    tree_sha: str,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    git_ok, author, _, g_tree, _ = inspect_git_commit(wt_path, commit_sha, runner=runner)
    if not git_ok:
        return False, "GIT_COMMIT_NOT_FOUND"
    if g_tree != tree_sha:
        return False, "TREE_SHA_MISMATCH"
    if not is_approved_identity(author):
        return False, "DISALLOWED_AUTHOR_IDENTITY"
    return True, ""


def _build_candidate_dict(
    commit_sha: str,
    tree_sha: str,
    gate_results: dict[str, Any] | None,
    modified_paths: list[str] | None,
    patch_path: Path | str | None,
    patch_sha256: str | None,
    bundle_path: Path | str | None,
    bundle_sha256: str | None,
    author_receipt_path: Path | str | None,
    author_receipt_sha256: str | None,
    iso_now: str,
) -> dict[str, Any]:
    return {
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "gate_results": gate_results,
        "modified_paths": modified_paths or [],
        "patch_path": str(patch_path) if patch_path else "",
        "patch_sha256": patch_sha256 or "",
        "bundle_path": str(bundle_path) if bundle_path else "",
        "bundle_sha256": bundle_sha256 or "",
        "author_receipt_path": str(author_receipt_path) if author_receipt_path else "",
        "author_receipt_sha256": author_receipt_sha256 or "",
        "recorded_at": iso_now,
    }


def _validate_impl_preconditions(
    wi: dict[str, Any],
    claim_token: str,
    commit_sha: str,
    tree_sha: str,
    modified_paths: list[str] | None,
    gate_results: dict[str, Any] | None,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    claim = wi.get("claim")
    if not claim or claim.get("claim_token") != claim_token:
        return False, "INVALID_CLAIM_TOKEN"
    if not (is_hex_40(commit_sha) and is_hex_40(tree_sha)):
        return False, "INVALID_REVISION_SHA"
    git_ok, git_err = _verify_git_commit_and_tree(Path(claim["worktree"]), commit_sha, tree_sha, runner=runner)
    if not git_ok:
        return False, git_err
    p_ok, p_err = inspect_modified_paths(modified_paths, [PROTECTED_SKILL_REL, "tests", "scripts"])
    if not p_ok:
        return False, p_err
    if not verify_mandatory_gates(gate_results):
        return False, "MANDATORY_GATES_FAILED"
    return True, ""


def record_implementation(
    control_dir: Path | str,
    work_id: str,
    claim_token: str,
    commit_sha: str,
    tree_sha: str,
    gate_results: dict[str, Any] | None = None,
    modified_paths: list[str] | None = None,
    patch_path: Path | str | None = None,
    patch_sha256: str | None = None,
    bundle_path: Path | str | None = None,
    bundle_sha256: str | None = None,
    author_receipt_path: Path | str | None = None,
    author_receipt_sha256: str | None = None,
    now: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
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
        return {"status": "refused", "reason_code": "WORK_NOT_ADMITTED", "observed_at": iso_now}
    wi = work_items[work_id]

    val_ok, val_err = _validate_impl_preconditions(
        wi, claim_token, commit_sha, tree_sha, modified_paths, gate_results, runner=runner
    )
    if not val_ok:
        return {"status": "refused", "reason_code": val_err, "observed_at": iso_now}

    invalidate_prior_reviews_if_revision_changed(wi, commit_sha, tree_sha)
    cand = _build_candidate_dict(
        commit_sha,
        tree_sha,
        gate_results,
        modified_paths,
        patch_path,
        patch_sha256,
        bundle_path,
        bundle_sha256,
        author_receipt_path,
        author_receipt_sha256,
        iso_now,
    )
    wi["candidate"] = cand
    wi["commit_sha"] = commit_sha
    wi["tree_sha"] = tree_sha
    wi["stage"] = "review"
    project = wi.get("repo", "").split("/")[-1]
    p_row = dc.setdefault("projects", {}).setdefault(project, {})
    p_row["stage"] = "review"
    p_row["next_action"] = f"Review candidate {commit_sha[:8]} for {work_id}"
    p_row["last_progress_at"] = iso_now
    status_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return {
        "status": "recorded",
        "work_id": work_id,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "stage": "review",
        "observed_at": iso_now,
    }
