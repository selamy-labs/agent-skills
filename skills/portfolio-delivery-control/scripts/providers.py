#!/usr/bin/env python3
"""Provider adapters for git and GitHub CLI operations."""

from __future__ import annotations

import datetime
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

UTC = datetime.UTC
APPROVED_BOT_IDENTITIES = {
    "selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>",
    "claude-cir <claude-cir@selamy.dev>",
}
APPROVED_BOT_EMAILS = {
    "346154065+selamy-labs-bot@users.noreply.github.com",
    "claude-cir@selamy.dev",
}
APPROVED_BOT_PAIRS = {
    ("selamy-labs-bot", "346154065+selamy-labs-bot@users.noreply.github.com"),
    ("claude-cir", "claude-cir@selamy.dev"),
}
ALLOWED_MERGE_IDENTITIES = {"selamy-labs-bot", "claude-cir"}
GH_PR_VIEW_JSON_FIELDS = "number,url,state,headRefOid,mergedAt,mergeCommit"


def iso_timestamp(dt: datetime.datetime | None = None) -> str:
    current = dt or datetime.datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path | str) -> str:
    import hashlib

    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_hex_40(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    s = val.strip()
    return len(s) == 40 and all(c in "0123456789abcdefABCDEF" for c in s)


def is_hex_64(val: str) -> bool:
    if not isinstance(val, str):
        return False
    s = val.strip()
    return len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s)


def is_approved_identity(ident: str) -> bool:
    clean = ident.strip()
    if clean in APPROVED_BOT_IDENTITIES or clean in ALLOWED_MERGE_IDENTITIES:
        return True
    if "<" in clean and clean.endswith(">"):
        name = clean[: clean.index("<")].strip()
        email = clean[clean.index("<") + 1 : -1].strip()
        if (name, email) in APPROVED_BOT_PAIRS:
            return True
        if not name and email in APPROVED_BOT_EMAILS:
            return True
    return False


def _handle_runner_result(res: Any) -> tuple[int, str]:
    if isinstance(res, tuple) and len(res) >= 2:
        return int(res[0]), str(res[1])
    code = getattr(res, "returncode", 1)
    out = str(getattr(res, "stdout", "") or "")
    err = str(getattr(res, "stderr", "") or "")
    return int(code), (out + err).strip()


def _run_cmd(
    cmd: list[str],
    cwd: Path | str | None,
    runner: Callable[..., Any] | None = None,
) -> tuple[int, str]:
    if runner is not None:
        return _handle_runner_result(runner(cmd, cwd=cwd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        return proc.returncode, out
    except (subprocess.SubprocessError, OSError) as exc:
        return 1, str(exc)


def get_remote_branch_sha(
    worktree: Path,
    remote: str,
    branch: str,
    runner: Callable[..., Any] | None = None,
) -> str | None:
    ref_name = f"refs/heads/{branch}"
    cmd = ["git", "ls-remote", "--heads", remote, ref_name]
    code, out = _run_cmd(cmd, worktree, runner)
    if code != 0 or not out.strip():
        return None
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == ref_name:
            return parts[0]
    return None


def inspect_git_commit(
    worktree: Path,
    commit_sha: str,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str, str, str, list[str]]:
    cmd = ["git", "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%T%n%P", commit_sha]
    code, out = _run_cmd(cmd, worktree, runner)
    if code != 0:
        return False, "", "", "", []
    lines = out.strip().splitlines()
    if len(lines) < 3:
        return False, "", "", "", []
    author = lines[0].strip()
    committer = lines[1].strip()
    tree = lines[2].strip()
    parents = lines[3].strip().split() if len(lines) >= 4 and lines[3].strip() else []
    return True, author, committer, tree, parents


def is_pr_already_merged(info: dict[str, Any]) -> tuple[bool, str]:
    if info.get("state") != "MERGED":
        return False, ""
    merged_at = info.get("mergedAt")
    if not isinstance(merged_at, str) or not merged_at.strip():
        return False, ""
    mc = info.get("mergeCommit")
    mc_oid = mc.get("oid") if isinstance(mc, dict) else (mc if isinstance(mc, str) else "")
    if not is_hex_40(mc_oid):
        return False, ""
    return True, str(mc_oid)


def evaluate_existing_pr_data(info: dict[str, Any], commit_sha: str) -> tuple[bool, Any]:
    state = info.get("state")
    if state == "CLOSED":
        return False, "PR_NOT_OPEN"
    if state == "OPEN":
        if info.get("headRefOid") != commit_sha:
            return False, None
        return True, {
            "reconciled_existing_effect": True,
            "pr_number": info.get("number"),
            "pr_url": info.get("url"),
            "state": "OPEN",
        }
    if state == "MERGED":
        merged, oid = is_pr_already_merged(info)
        if merged and info.get("headRefOid") == commit_sha:
            return True, {
                "reconciled_existing_effect": True,
                "pr_number": info.get("number"),
                "pr_url": info.get("url"),
                "state": "MERGED",
                "merge_commit_sha": oid,
            }
        return False, "PR_NOT_OPEN"
    return False, "PR_NOT_OPEN"


def check_existing_pr(
    target: Path | str,
    branch_or_sha: str,
    commit_sha: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, Any]:
    if commit_sha is not None:
        cmd = ["gh", "pr", "view", branch_or_sha, "--json", GH_PR_VIEW_JSON_FIELDS]
        code, out = _run_cmd(cmd, Path(target), runner)
        if code != 0 or not out.strip():
            return False, None
        raw_out = out
        expected_sha = commit_sha
    else:
        raw_out = str(target)
        expected_sha = branch_or_sha

    try:
        info = json.loads(raw_out)
        if isinstance(info, dict):
            return evaluate_existing_pr_data(info, expected_sha)
    except (json.JSONDecodeError, TypeError):
        pass
    return False, None


def _is_provider_bot_login_allowed(login: str, acc_type: str, merge_identity: str) -> bool:
    if not (login in ALLOWED_MERGE_IDENTITIES or login.endswith("[bot]")):
        return False
    if login == merge_identity:
        return True
    if acc_type.lower() == "bot" and login.startswith(merge_identity):
        return True
    return False


def verify_provider_bot_actor(
    worktree: Path,
    merge_identity: str = "selamy-labs-bot",
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    cmd = ["gh", "api", "user", "--jq", "{login: .login, type: .type}"]
    code, out = _run_cmd(cmd, worktree, runner)
    if code != 0 or not out.strip():
        cmd_auth = ["gh", "auth", "status"]
        c_code, c_out = _run_cmd(cmd_auth, worktree, runner)
        if c_code != 0:
            return False, "PROVIDER_AUTH_FAILED"
        if merge_identity in c_out:
            return True, ""
        return False, "DISALLOWED_MERGE_IDENTITY"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False, "PROVIDER_AUTH_FAILED"
    if not isinstance(data, dict):
        return False, "DISALLOWED_MERGE_IDENTITY"
    login = str(data.get("login") or "").strip()
    acc_type = str(data.get("type") or "").strip()
    if not _is_provider_bot_login_allowed(login, acc_type, merge_identity):
        return False, "DISALLOWED_MERGE_IDENTITY"
    return True, ""


def get_commit_modified_paths(
    worktree: Path,
    commit_sha: str,
    runner: Callable[..., Any] | None = None,
) -> list[str]:
    cmd = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha]
    code, out = _run_cmd(cmd, worktree, runner)
    if code != 0 or not out.strip():
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def reconcile_and_push(
    worktree: Path,
    remote: str,
    branch: str,
    commit_sha: str,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    remote_head = get_remote_branch_sha(worktree, remote, branch, runner)
    if remote_head and remote_head == commit_sha:
        return True, {
            "reconciled_existing_effect": True,
            "remote_head": remote_head,
        }
    push_cmd = ["git", "push", remote, f"{commit_sha}:refs/heads/{branch}"]
    p_code, push_out = _run_cmd(push_cmd, worktree, runner)
    if p_code != 0:
        return False, {"error": push_out}
    return True, {"reconciled_existing_effect": False, "push_output": push_out}


def _verify_created_pr(v_code: int, v_out: str, commit_sha: str) -> tuple[bool, dict[str, Any]]:
    if v_code != 0:
        return False, {"error": f"Failed to query created PR: {v_out}", "reason_code": "CREATED_PR_VIEW_FAILED"}
    try:
        pr_data = json.loads(v_out)
    except json.JSONDecodeError:
        return False, {"error": "Invalid JSON from gh pr view", "reason_code": "PR_HEAD_MISMATCH"}
    if not isinstance(pr_data, dict):
        return False, {"error": "Invalid PR data", "reason_code": "PR_HEAD_MISMATCH"}
    if pr_data.get("state") != "OPEN" or pr_data.get("headRefOid") != commit_sha:
        return False, {
            "error": f"Created PR state '{pr_data.get('state')}' or headRefOid '{pr_data.get('headRefOid')}' mismatch",
            "reason_code": "PR_HEAD_MISMATCH",
        }
    return True, pr_data


def reconcile_and_publish_pr(
    worktree: Path,
    branch: str,
    base_branch: str,
    commit_sha: str,
    title: str,
    body: str,
    runner: Callable[..., Any] | None = None,
    remote: str = "origin",
) -> tuple[bool, dict[str, Any]]:
    matched, pr_info = check_existing_pr(worktree, branch, commit_sha, runner=runner)
    if matched and isinstance(pr_info, dict):
        return True, pr_info
    if pr_info == "PR_NOT_OPEN":
        return False, {
            "error": f"Existing PR for branch '{branch}' is not open",
            "reason_code": "PR_NOT_OPEN",
        }

    remote_sha = get_remote_branch_sha(worktree, remote, branch, runner)
    if not remote_sha or remote_sha != commit_sha:
        return False, {
            "error": f"Remote branch '{branch}' head '{remote_sha}' does not match commit '{commit_sha}'",
            "reason_code": "REMOTE_HEAD_MISMATCH",
        }

    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--head",
        branch,
        "--base",
        base_branch,
    ]
    code, pr_out = _run_cmd(cmd, worktree, runner)
    if code != 0:
        return False, {"error": pr_out}

    v_code, v_out = _run_cmd(
        ["gh", "pr", "view", branch, "--json", GH_PR_VIEW_JSON_FIELDS],
        worktree,
        runner,
    )
    v_ok, v_res = _verify_created_pr(v_code, v_out, commit_sha)
    if not v_ok:
        return False, v_res
    return True, {
        "reconciled_existing_effect": False,
        "pr_number": v_res.get("number"),
        "pr_url": v_res.get("url"),
        "output": pr_out,
    }


def fetch_pr_base_branch(
    repo: str,
    pr_ref: str,
    runner: Callable[..., Any] | None = None,
    cwd: Path | str | None = None,
) -> str | None:
    cmd = ["gh", "pr", "view", str(pr_ref)]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(["--json", "baseRefName"])
    code, out = _run_cmd(cmd, cwd, runner)
    if code != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            base = str(data.get("baseRefName") or "").strip()
            return base if base else None
    except json.JSONDecodeError:
        pass
    return None


def _clean_string_list(items: list[Any]) -> list[str]:
    res = []
    for x in items:
        s = str(x).strip()
        if s:
            res.append(s)
    return res


def _split_owner_repo(repo: str) -> tuple[str, str]:
    clean = str(repo).strip()
    if "/" in clean:
        parts = clean.split("/", 1)
        return parts[0].strip(), parts[1].strip()
    return "", clean


def _find_exact_repo_match(req_checks: dict[str, Any], target_repo: str) -> list[str] | None:
    if target_repo in req_checks and isinstance(req_checks[target_repo], list):
        return _clean_string_list(req_checks[target_repo])
    owner, repo_name = _split_owner_repo(target_repo)
    if repo_name and repo_name in req_checks and isinstance(req_checks[repo_name], list):
        return _clean_string_list(req_checks[repo_name])
    return None


def _extract_repo_required_checks(req_checks: Any, target_repo: str) -> list[str] | None:
    if isinstance(req_checks, list):
        return _clean_string_list(req_checks)
    if isinstance(req_checks, dict):
        match = _find_exact_repo_match(req_checks, target_repo)
        if match is not None:
            return match
        _, repo_name = _split_owner_repo(target_repo)
        dash_name = repo_name.replace("-", "_")
        if dash_name in req_checks and isinstance(req_checks[dash_name], list):
            return _clean_string_list(req_checks[dash_name])
    return None


def _read_policy_dict(policy_path: Path) -> dict[str, Any] | None:
    if not policy_path.exists():
        return None
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _normalize_req_check(x: Any) -> dict[str, Any]:
    if isinstance(x, dict):
        ctx = str(x.get("context") or x.get("name") or "").strip()
        return {"context": ctx, "app_id": None}
    return {"context": str(x).strip(), "app_id": None}


def load_policy_required_checks(
    repo: str,
    control_dir: Path | str | None = None,
) -> list[dict[str, Any]] | None:
    if not control_dir:
        return None
    p = Path(control_dir) / "delivery-policy.json"
    doc = _read_policy_dict(p)
    if doc and "required_checks" in doc:
        raw = _extract_repo_required_checks(doc["required_checks"], repo)
        if raw is not None:
            return [_normalize_req_check(x) for x in raw]
    return None


def _match_repo_in_authoritative(repo: str, auth: Any) -> bool:
    if not repo:
        return False
    owner, repo_name = _split_owner_repo(repo)
    if isinstance(auth, list):
        auth_repos = [str(x).strip() for x in auth if str(x).strip()]
        if repo in auth_repos or repo_name in auth_repos:
            return True
    return False


def is_policy_authoritative(
    repo: str,
    control_dir: Path | str | None = None,
) -> bool:
    if not control_dir:
        return False
    p = Path(control_dir) / "delivery-policy.json"
    doc = _read_policy_dict(p)
    if not doc:
        return False
    auth = doc.get("authoritative") or doc.get("authoritative_policy") or doc.get("authoritative_repositories")
    if auth is True:
        return True
    return _match_repo_in_authoritative(repo, auth)


# ==============================================================================
# Provider PR CI Evaluation (Adopt-and-Delete: replaces bespoke derivation)
# ==============================================================================


def _parse_pr_view_payload(out: str) -> dict[str, Any] | None:
    try:
        data = json.loads(out)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_pr_checks_payload(out: str) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(out)
        if not isinstance(data, list):
            return None
        res: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                return None
            res.append(item)
        return res
    except json.JSONDecodeError:
        return None


def _verify_pr_view_head_and_state(
    info: dict[str, Any],
    commit_sha: str,
    expected_base_branch: str = "",
    allow_merged: bool = True,
) -> tuple[bool, str]:
    state = str(info.get("state") or "").strip()
    valid_states = {"OPEN", "MERGED"} if allow_merged else {"OPEN"}
    if state not in valid_states:
        return False, "PR_NOT_OPEN"
    head = str(info.get("headRefOid") or "").strip()
    if not head or head != commit_sha:
        return False, "PR_HEAD_MISMATCH"
    if expected_base_branch:
        base_ref = str(info.get("baseRefName") or "").strip()
        if not base_ref or base_ref != expected_base_branch:
            return False, "BASE_BRANCH_MISMATCH"
    return True, ""


def _evaluate_provider_required_checks(
    worktree: Path,
    pr_ref: str,
    runner: Callable[..., Any] | None,
    repo: str = "",
) -> tuple[bool, str, list[dict[str, Any]]]:
    cmd = ["gh", "pr", "checks", str(pr_ref)]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(["--required", "--json", "name,state,bucket,workflow"])
    code, out = _run_cmd(cmd, worktree, runner)
    if code != 0:
        return False, "CI_CHECK_MISSING", []
    checks = _parse_pr_checks_payload(out)
    if checks is None or len(checks) == 0:
        return False, "CI_CHECK_MISSING", []
    for chk in checks:
        bucket = str(chk.get("bucket") or "").strip().lower()
        if bucket != "pass":
            return False, "CI_CHECK_FAILED", []
    return True, "", checks


def _verify_policy_checks_passed(
    policy_checks: list[dict[str, Any]],
    checks_by_name: dict[str, str],
) -> tuple[bool, str]:
    for p in policy_checks:
        req_name = str(p.get("context") or "").strip()
        if not req_name or req_name not in checks_by_name:
            return False, "CI_CHECK_MISSING"
        if checks_by_name[req_name] != "pass":
            return False, "CI_CHECK_FAILED"
    return True, ""


def _evaluate_additive_policy_checks(
    worktree: Path,
    pr_ref: str,
    repo: str,
    control_dir: Path | str | None,
    runner: Callable[..., Any] | None,
) -> tuple[bool, str]:
    if not control_dir:
        return True, ""
    policy_checks = load_policy_required_checks(repo, control_dir=control_dir)
    if not policy_checks:
        return True, ""
    cmd = ["gh", "pr", "checks", str(pr_ref)]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(["--json", "name,bucket"])
    code, out = _run_cmd(cmd, worktree, runner)
    if code != 0:
        return False, "CI_CHECK_MISSING"
    all_checks = _parse_pr_checks_payload(out)
    if all_checks is None:
        return False, "CI_CHECK_MISSING"
    checks_by_name = {str(c.get("name") or "").strip(): str(c.get("bucket") or "").strip().lower() for c in all_checks}
    return _verify_policy_checks_passed(policy_checks, checks_by_name)


def evaluate_pr_ci(
    worktree: Path,
    pr_ref: str,
    commit_sha: str,
    runner: Callable[..., Any] | None = None,
    expected_base_branch: str = "",
    control_dir: Path | str | None = None,
    repo: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    # 1. Pre-check: gh pr view
    cmd_view = ["gh", "pr", "view", str(pr_ref)]
    if repo:
        cmd_view.extend(["--repo", repo])
    cmd_view.extend(["--json", "headRefOid,baseRefName,state"])
    code1, out1 = _run_cmd(cmd_view, worktree, runner)
    if code1 != 0:
        return False, "PR_VIEW_FAILED", {}
    info1 = _parse_pr_view_payload(out1)
    if info1 is None:
        return False, "PR_VIEW_FAILED", {}

    pre_ok, pre_err = _verify_pr_view_head_and_state(info1, commit_sha, expected_base_branch)
    if not pre_ok:
        return False, pre_err, {}

    # 2. Required checks: gh pr checks --required
    req_ok, req_err, _ = _evaluate_provider_required_checks(worktree, pr_ref, runner, repo=repo)
    if not req_ok:
        return False, req_err, {}

    # 3. Additive policy checks
    pol_ok, pol_err = _evaluate_additive_policy_checks(worktree, pr_ref, repo, control_dir, runner)
    if not pol_ok:
        return False, pol_err, {}

    # 4. Post-check: gh pr view
    code2, out2 = _run_cmd(cmd_view, worktree, runner)
    if code2 != 0:
        return False, "PR_VIEW_FAILED", {}
    info2 = _parse_pr_view_payload(out2)
    if info2 is None:
        return False, "PR_VIEW_FAILED", {}

    post_head = str(info2.get("headRefOid") or "").strip()
    if not post_head or post_head != commit_sha:
        return False, "MISSING_EXACT_HEAD_CI", {}

    return True, "", info2


# ==============================================================================
# PR Merge Operations
# ==============================================================================


def _post_merge_verify(
    pr_ref: str,
    commit_sha: str,
    runner: Callable[..., Any] | None,
    cwd: Path | str | None = None,
    repo: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    cmd = ["gh", "pr", "view", str(pr_ref)]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(["--json", "state,mergedAt,mergeCommit,headRefOid"])
    code, out = _run_cmd(cmd, cwd, runner)
    if code != 0:
        return False, "PR_VIEW_FAILED", {"error": "Failed to re-query merged PR view"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False, "PR_VIEW_FAILED", {"error": "Failed to decode post-merge PR JSON"}
    if not isinstance(data, dict):
        return False, "PR_VIEW_FAILED", {"error": "Post-merge view was not a dictionary"}
    if data.get("state") != "MERGED":
        return False, "MERGE_NOT_COMPLETED", {"error": "PR is not in MERGED state after merge execution"}
    mc = data.get("mergeCommit")
    m_oid = mc.get("oid", "") if isinstance(mc, dict) else ""
    if data.get("headRefOid") != commit_sha:
        return False, "CANDIDATE_REVISION_MISMATCH", {"error": "Post-merge candidate head mismatch"}
    return True, m_oid, data


def _execute_pr_merge(
    worktree: Path,
    pr_ref: str,
    commit_sha: str,
    runner: Callable[..., Any] | None,
    repo: str,
) -> tuple[bool, dict[str, Any]]:
    merge_cmd = ["gh", "pr", "merge", str(pr_ref)]
    if repo:
        merge_cmd.extend(["--repo", repo])
    merge_cmd.extend(["--squash", "--match-head-commit", commit_sha])
    m_code, merge_out = _run_cmd(merge_cmd, worktree, runner)
    if m_code != 0:
        return False, {"error": merge_out, "reason_code": "MERGE_FAILED"}

    post_ok, post_err_code, post_data = _post_merge_verify(pr_ref, commit_sha, runner, cwd=worktree, repo=repo)
    if not post_ok:
        return False, {"error": post_data.get("error", "Merge was not completed"), "reason_code": post_err_code}

    return True, {
        "reconciled_existing_effect": False,
        "state": "MERGED",
        "merge_commit": post_err_code,
        "merge_commit_sha": post_err_code,
        "merged_at": post_data.get("mergedAt"),
        "output": merge_out,
    }


def _check_merge_already_merged(
    info: dict[str, Any],
    commit_sha: str,
    expected_base_branch: str = "",
) -> tuple[bool, dict[str, Any] | None]:
    merged, mc_oid = is_pr_already_merged(info)
    if not merged:
        return True, None
    if expected_base_branch:
        base_ref = str(info.get("baseRefName") or "").strip()
        if not base_ref or base_ref != expected_base_branch:
            return False, {"error": "Base branch mismatch", "reason_code": "BASE_BRANCH_MISMATCH"}
    head_sha = str(info.get("headRefOid") or "").strip()
    if head_sha == commit_sha:
        return True, {
            "reconciled_existing_effect": True,
            "state": "MERGED",
            "merge_commit": mc_oid,
            "merge_commit_sha": mc_oid,
            "merged_at": info.get("mergedAt"),
        }
    return False, {"error": "Head mismatch on merged PR", "reason_code": "PR_HEAD_MISMATCH"}


def reconcile_and_merge_pr(
    worktree: Path,
    pr_ref: str,
    commit_sha: str,
    merge_identity: str = "selamy-labs-bot",
    runner: Callable[..., Any] | None = None,
    repo: str = "",
    expected_base_branch: str = "",
    control_dir: Path | str | None = None,
) -> tuple[bool, dict[str, Any]]:
    id_ok, id_err = verify_provider_bot_actor(worktree, merge_identity, runner)
    if not id_ok:
        return False, {"error": "Provider authenticated user disallowed", "reason_code": id_err}

    cmd_view = ["gh", "pr", "view", str(pr_ref)]
    if repo:
        cmd_view.extend(["--repo", repo])
    cmd_view.extend(["--json", "state,mergedAt,mergeCommit,headRefOid,baseRefName"])
    code, out = _run_cmd(cmd_view, worktree, runner)
    if code != 0:
        return False, {"error": "PR view failed", "reason_code": "PR_VIEW_FAILED"}
    info = _parse_pr_view_payload(out)
    if info is None:
        return False, {"error": "PR view failed", "reason_code": "PR_VIEW_FAILED"}

    already_ok, already_res = _check_merge_already_merged(info, commit_sha, expected_base_branch)
    if not already_ok:
        return False, already_res or {}
    if already_res is not None:
        return True, already_res

    ci_ok, ci_err, _ = evaluate_pr_ci(
        worktree,
        pr_ref,
        commit_sha,
        runner=runner,
        expected_base_branch=expected_base_branch,
        control_dir=control_dir,
        repo=repo,
    )
    if not ci_ok:
        return False, {"error": "CI evaluation failed", "reason_code": ci_err}

    return _execute_pr_merge(worktree, pr_ref, commit_sha, runner, repo)


def reconcile_and_complete_spec_task(
    spec_path: Path | str | None,
    task_id: str,
) -> tuple[bool, dict[str, Any]]:
    if not spec_path:
        return False, {"error": "Missing spec_path for complete_spec_task"}
    p = Path(spec_path)
    if not p.exists():
        return False, {"error": f"Spec path {spec_path} does not exist"}
    content = p.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\s*-\s*\[)([ xX])(\]\s+{re.escape(task_id)})(?=\s|:|$)(.*)$", re.MULTILINE)
    matches = list(pattern.finditer(content))
    if not matches:
        return False, {"error": f"Task ID {task_id} not found in {spec_path}"}
    if any(m.group(2).lower() == "x" for m in matches):
        return True, {"reconciled_existing_effect": True, "task_id": task_id}

    def _repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}x{match.group(3)}{match.group(4)}"

    updated, count = pattern.subn(_repl, content, count=1)
    if count == 0:
        return False, {"error": f"Failed to mark task {task_id} complete"}
    p.write_text(updated, encoding="utf-8")
    return True, {"reconciled_existing_effect": False, "task_id": task_id}
