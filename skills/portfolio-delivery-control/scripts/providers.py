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


def stamp_rollup_head_sha(rollup: Any, commit_sha: str) -> None:
    if not isinstance(rollup, list) or not is_hex_40(commit_sha):
        return
    for item in rollup:
        if isinstance(item, dict):
            c_sha = (
                item.get("head_sha")
                or item.get("commit_sha")
                or item.get("sha")
                or item.get("headSha")
                or item.get("revision")
            )
            if not c_sha:
                item["head_sha"] = commit_sha


def _parse_pr_view_rollup(out: str, cand_sha: str) -> list[dict[str, Any]] | None:
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(info, dict) or info.get("headRefOid") != cand_sha:
        return None
    rollup = info.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return None
    checks = [dict(item) for item in rollup if isinstance(item, dict)]
    stamp_rollup_head_sha(checks, cand_sha)
    return checks


def fetch_ci_checks_from_provider(
    repo: str | Path | None,
    pr_ref: str,
    cand_sha: str,
    runner: Callable[..., Any] | None = None,
    cwd: Path | str | None = None,
) -> list[dict[str, Any]] | None:
    repo_name = str(repo).strip() if isinstance(repo, str) else ""
    worktree = Path(cwd or (repo if isinstance(repo, Path) else "."))
    cmd = ["gh", "pr", "view", pr_ref]
    if repo_name:
        cmd.extend(["--repo", repo_name])
    cmd.extend(["--json", "statusCheckRollup,headRefOid"])
    code, out = _run_cmd(cmd, worktree, runner)
    if code != 0 or not out.strip():
        return None
    return _parse_pr_view_rollup(out, cand_sha)


def fetch_pr_base_branch(
    repo: str | Path | None,
    pr_ref: str,
    runner: Callable[..., Any] | None = None,
    cwd: Path | str | None = None,
) -> str | None:
    repo_name = str(repo).strip() if isinstance(repo, str) else ""
    worktree = Path(cwd or (repo if isinstance(repo, Path) else "."))
    cmd = ["gh", "pr", "view", pr_ref]
    if repo_name:
        cmd.extend(["--repo", repo_name])
    cmd.extend(["--json", "baseRefName"])
    code, out = _run_cmd(cmd, worktree, runner)
    if code != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            val = str(data.get("baseRefName") or "").strip()
            return val or None
    except json.JSONDecodeError:
        pass
    return None


def _normalize_req_check(req: Any) -> dict[str, Any]:
    if isinstance(req, str):
        return {"context": req.strip(), "app_id": None}
    if isinstance(req, dict):
        ctx = str(req.get("context") or req.get("name") or "").strip()
        app_id = req.get("app_id") or req.get("integration_id")
        return {"context": ctx, "app_id": app_id}
    return {"context": str(req).strip(), "app_id": None}


def _merge_check_descriptors(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        ctx = str(item.get("context") or "").strip()
        if not ctx:
            continue
        if ctx not in merged or (merged[ctx].get("app_id") is None and item.get("app_id") is not None):
            merged[ctx] = item
    return list(merged.values())


def _parse_contexts(contexts: Any) -> list[dict[str, Any]]:
    if not isinstance(contexts, list):
        return []
    return [{"context": c.strip(), "app_id": None} for c in contexts if isinstance(c, str) and c.strip()]


def _parse_check_objects(checks_list: Any) -> list[dict[str, Any]]:
    if not isinstance(checks_list, list):
        return []
    res: list[dict[str, Any]] = []
    for c in checks_list:
        if isinstance(c, dict):
            ctx = str(c.get("context") or "").strip()
            if ctx:
                app_id = c.get("app_id") or c.get("integration_id")
                res.append({"context": ctx, "app_id": app_id})
    return res


def _parse_dict_required_checks(data: dict[str, Any]) -> list[dict[str, Any]]:
    combined = _parse_contexts(data.get("contexts")) + _parse_check_objects(data.get("checks"))
    return _merge_check_descriptors(combined)


def _extract_rule_check_item(rsc: Any) -> dict[str, Any] | None:
    if isinstance(rsc, dict):
        ctx = str(rsc.get("context") or "").strip()
        if ctx:
            app_id = rsc.get("integration_id") or rsc.get("app_id")
            return {"context": ctx, "app_id": app_id}
    return None


def _extract_rule_contexts(rule: Any) -> list[dict[str, Any]]:
    if not (isinstance(rule, dict) and rule.get("type") == "required_status_checks"):
        return []
    params = rule.get("parameters")
    if not isinstance(params, dict):
        return []
    rsc_list = params.get("required_status_checks")
    if not isinstance(rsc_list, list):
        return []
    items: list[dict[str, Any]] = []
    for rsc in rsc_list:
        cand = _extract_rule_check_item(rsc)
        if cand is not None:
            items.append(cand)
    return items


def _parse_ruleset_required_checks(data: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for rule in data:
        items.extend(_extract_rule_contexts(rule))
    return _merge_check_descriptors(items)


def parse_required_checks(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return _parse_dict_required_checks(data)
    if isinstance(data, list):
        return _parse_ruleset_required_checks(data)
    return []


def _query_branch_protection_checks(
    endpoint: str, worktree: Path, runner: Callable[..., Any] | None
) -> list[dict[str, Any]] | None:
    code, out = _run_cmd(["gh", "api", endpoint], worktree, runner)
    if code != 0 or not out.strip():
        return None
    try:
        return parse_required_checks(json.loads(out))
    except json.JSONDecodeError:
        return None


def fetch_required_checks_from_provider(
    repo: str | Path | None,
    base_branch: str = "main",
    runner: Callable[..., Any] | None = None,
    cwd: Path | str | None = None,
) -> list[dict[str, Any]] | None:
    worktree = Path(cwd if cwd else ".")
    repo_name = str(repo).strip() if repo else ""
    bp_endpoint = (
        f"repos/{repo_name}/branches/{base_branch}/protection/required_status_checks"
        if repo_name
        else f"repos/{{owner}}/{{repo}}/branches/{base_branch}/protection/required_status_checks"
    )
    rules_endpoint = (
        f"repos/{repo_name}/rules/branches/{base_branch}"
        if repo_name
        else f"repos/{{owner}}/{{repo}}/rules/branches/{base_branch}"
    )
    bp_res = _query_branch_protection_checks(bp_endpoint, worktree, runner)
    rules_res = _query_branch_protection_checks(rules_endpoint, worktree, runner)

    if bp_res is None and rules_res is None:
        return None

    combined: list[dict[str, Any]] = []
    if bp_res:
        combined.extend(bp_res)
    if rules_res:
        combined.extend(rules_res)
    return _merge_check_descriptors(combined)


def _clean_string_list(items: list[Any]) -> list[str]:
    res = []
    for x in items:
        s = str(x).strip()
        if s:
            res.append(s)
    return res


def _extract_repo_required_checks(req_checks: Any, repo: str) -> list[str] | None:
    if isinstance(req_checks, list):
        return _clean_string_list(req_checks)
    if not isinstance(req_checks, dict):
        return None
    clean = repo.strip()
    base = clean.split("/")[-1]
    for cand in (clean, base, base.replace("-", "_")):
        val = req_checks.get(cand)
        if isinstance(val, list):
            return _clean_string_list(val)
    return None


def _read_policy_dict(p: Path) -> dict[str, Any] | None:
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


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


def resolve_required_checks(
    repo: str | None,
    base_branch: str = "main",
    runner: Callable[..., Any] | None = None,
    cwd: Path | str | None = None,
    control_dir: Path | str | None = None,
) -> list[dict[str, Any]] | None:
    provider_checks = fetch_required_checks_from_provider(repo, base_branch, runner=runner, cwd=cwd)
    policy_checks = load_policy_required_checks(str(repo or ""), control_dir=control_dir)

    if provider_checks is None and policy_checks is None:
        return None

    combined: list[dict[str, Any]] = []
    if provider_checks:
        combined.extend(provider_checks)
    if policy_checks:
        combined.extend(policy_checks)
    return _merge_check_descriptors(combined)


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


def is_ci_check_passing(c: Any, cand_sha: str) -> bool:
    if not isinstance(c, dict):
        return False
    c_sha = str(c.get("head_sha") or c.get("commit_sha") or c.get("sha") or "").strip()
    if c_sha and c_sha != cand_sha:
        return False
    status = str(c.get("status") or "").upper()
    concl = str(c.get("conclusion") or "").upper()
    state = str(c.get("state") or "").upper()
    if concl in {"FAILURE", "FAILED", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "SKIPPED", "NEUTRAL"}:
        return False
    if state in {"FAILURE", "ERROR"}:
        return False
    return (status == "COMPLETED" and concl == "SUCCESS") or state == "SUCCESS"


_is_ci_check_passing = is_ci_check_passing


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


def _check_name_matches(item: dict[str, Any], req_name: str) -> bool:
    name = str(item.get("name") or item.get("context") or "").strip()
    return name == req_name


def _check_app_matches(item: dict[str, Any], req_app_id: int | str | None) -> bool:
    if req_app_id is None:
        return True
    app_val = item.get("app")
    actual_id = None
    if isinstance(app_val, dict):
        actual_id = app_val.get("id")
    elif app_val is not None:
        actual_id = app_val
    else:
        actual_id = item.get("app_id") or item.get("integration_id")

    if actual_id is None:
        return False
    return str(actual_id).strip() == str(req_app_id).strip()


def _item_matches_check(item: Any, req_name: str, req_app_id: int | str | None) -> bool:
    if not isinstance(item, dict):
        return False
    return _check_name_matches(item, req_name) and _check_app_matches(item, req_app_id)


def _is_named_check_passing(
    req: Any,
    rollup: list[dict[str, Any]],
    commit_sha: str,
) -> tuple[bool, str]:
    norm = _normalize_req_check(req)
    req_name = norm["context"]
    req_app_id = norm["app_id"]

    matched = False
    for item in rollup:
        if _item_matches_check(item, req_name, req_app_id):
            matched = True
            if not _is_ci_check_passing(item, commit_sha):
                return False, "CI_CHECK_FAILED"
    if not matched:
        return False, "CI_CHECK_MISSING"
    return True, ""


def _verify_all_required_present(
    rollup: list[dict[str, Any]], commit_sha: str, required_checks: list[Any]
) -> tuple[bool, str]:
    if not required_checks:
        return False, "CI_CHECK_MISSING"
    for req in required_checks:
        ok, err = _is_named_check_passing(req, rollup, commit_sha)
        if not ok:
            return False, err
    return True, ""


def _is_item_failed(item: Any, commit_sha: str) -> bool:
    if not isinstance(item, dict):
        return True
    c_sha = str(item.get("head_sha") or item.get("commit_sha") or item.get("sha") or "").strip()
    if c_sha and c_sha != commit_sha:
        return True
    concl = str(item.get("conclusion") or "").upper()
    state = str(item.get("state") or "").upper()
    if concl in {"FAILURE", "FAILED", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
        return True
    if state in {"FAILURE", "ERROR"}:
        return True
    return False


def _verify_required_and_clean_rollup(
    rollup: list[dict[str, Any]], commit_sha: str, required_checks: list[Any]
) -> tuple[bool, str]:
    ok, err = _verify_all_required_present(rollup, commit_sha, required_checks)
    if not ok:
        return False, err
    for item in rollup:
        if _is_item_failed(item, commit_sha):
            return False, "CI_CHECK_FAILED"
    return True, ""


def _verify_all_checks_passing(rollup: list[dict[str, Any]], commit_sha: str) -> tuple[bool, str]:
    for item in rollup:
        if not isinstance(item, dict) or not _is_ci_check_passing(item, commit_sha):
            return False, "CI_CHECK_FAILED"
    return True, ""


def verify_live_status_rollup(
    rollup: list[dict[str, Any]],
    commit_sha: str,
    required_checks: list[Any] | None = None,
) -> tuple[bool, str]:
    if not rollup:
        return False, "MISSING_EXACT_HEAD_CI"
    if required_checks is not None:
        return _verify_required_and_clean_rollup(rollup, commit_sha, required_checks)
    return _verify_all_checks_passing(rollup, commit_sha)


def _post_merge_verify(
    repo: str | Path | None,
    pr_number: str | int,
    cand_sha: str,
    runner: Callable[..., Any] | None = None,
    cwd: Path | str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    repo_name = str(repo).strip() if isinstance(repo, str) else ""
    worktree = Path(cwd or (repo if isinstance(repo, Path) else "."))
    cmd = ["gh", "pr", "view", str(pr_number)]
    if repo_name:
        cmd.extend(["--repo", repo_name])
    cmd.extend(["--json", "state,mergedAt,mergeCommit,headRefOid"])
    v_code, v_out = _run_cmd(cmd, worktree, runner)
    if v_code != 0:
        return False, "MERGE_NOT_COMPLETED", {"error": f"Failed to verify merge state: {v_out}"}
    try:
        post_info = json.loads(v_out)
    except json.JSONDecodeError:
        post_info = {}
    merged_post, post_oid = is_pr_already_merged(post_info)
    if not merged_post:
        return (
            False,
            "MERGE_NOT_COMPLETED",
            {
                "error": f"PR {pr_number} was queued or not merged",
                "post_info": post_info,
            },
        )
    post_head = str(post_info.get("headRefOid") or "").strip()
    if not post_head or post_head != cand_sha:
        return (
            False,
            "CANDIDATE_REVISION_MISMATCH",
            {
                "error": f"Post-merge PR headRefOid {post_head} != {cand_sha}",
                "reason_code": "CANDIDATE_REVISION_MISMATCH",
                "post_info": post_info,
            },
        )
    return True, post_oid, post_info


def _verify_merge_pr_view(
    worktree: Path,
    pr_ref: str,
    commit_sha: str,
    runner: Callable[..., Any] | None = None,
    repo: str = "",
    expected_base_branch: str = "",
) -> tuple[bool, str, str, dict[str, Any]]:
    cmd = ["gh", "pr", "view", pr_ref]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(["--json", "state,mergeCommit,headRefOid,statusCheckRollup,mergedAt,baseRefName"])
    code, out = _run_cmd(
        cmd,
        worktree,
        runner,
    )
    if code != 0:
        return False, "PR_VIEW_FAILED", out, {}
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        return False, "PR_VIEW_FAILED", "Failed to parse gh pr view output", {}
    if not isinstance(info, dict):
        return False, "PR_VIEW_FAILED", "Invalid PR view object", {}
    head_ref = str(info.get("headRefOid") or "").strip()
    if head_ref != commit_sha:
        return False, "PR_HEAD_MISMATCH", f"PR head {head_ref} != {commit_sha}", {}
    if expected_base_branch:
        base_ref = str(info.get("baseRefName") or "").strip()
        if base_ref and base_ref != expected_base_branch:
            return False, "BASE_BRANCH_MISMATCH", f"PR baseRefName {base_ref} != {expected_base_branch}", {}
    return True, "", "", info


def _verify_merge_ci(
    info: dict[str, Any],
    commit_sha: str,
    required_checks: list[Any] | None = None,
) -> tuple[bool, str]:
    stamp_rollup_head_sha(info.get("statusCheckRollup"), commit_sha)
    rollup = info.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return False, "MISSING_EXACT_HEAD_CI"
    return verify_live_status_rollup(rollup, commit_sha, required_checks=required_checks)


def _resolve_merge_required_checks(
    repo: str,
    pr_ref: str,
    expected_base_branch: str,
    worktree: Path,
    runner: Callable[..., Any] | None,
    required_checks: list[Any] | None,
) -> tuple[bool, list[Any] | None]:
    if required_checks is not None:
        return True, required_checks
    target_base = expected_base_branch or fetch_pr_base_branch(repo, pr_ref, runner=runner, cwd=worktree) or "main"
    resolved = resolve_required_checks(repo, base_branch=target_base, runner=runner, cwd=worktree)
    if not resolved:
        return False, None
    return True, resolved


def _execute_pr_merge(
    worktree: Path,
    pr_ref: str,
    commit_sha: str,
    runner: Callable[..., Any] | None,
    repo: str,
) -> tuple[bool, dict[str, Any]]:
    merge_cmd = ["gh", "pr", "merge", pr_ref]
    if repo:
        merge_cmd.extend(["--repo", repo])
    merge_cmd.extend(["--squash", "--match-head-commit", commit_sha])
    m_code, merge_out = _run_cmd(merge_cmd, worktree, runner)
    if m_code != 0:
        return False, {"error": merge_out, "reason_code": "MERGE_FAILED"}

    post_ok, post_err_code, post_data = _post_merge_verify(repo or worktree, pr_ref, commit_sha, runner, cwd=worktree)
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


def reconcile_and_merge_pr(
    worktree: Path,
    pr_ref: str,
    commit_sha: str,
    merge_identity: str = "selamy-labs-bot",
    runner: Callable[..., Any] | None = None,
    required_checks: list[Any] | None = None,
    repo: str = "",
    expected_base_branch: str = "",
) -> tuple[bool, dict[str, Any]]:
    id_ok, id_err = verify_provider_bot_actor(worktree, merge_identity, runner)
    if not id_ok:
        return False, {"error": "Provider authenticated user disallowed", "reason_code": id_err}

    pr_ok, pr_reason, pr_err, info = _verify_merge_pr_view(
        worktree, pr_ref, commit_sha, runner, repo=repo, expected_base_branch=expected_base_branch
    )
    if not pr_ok:
        return False, {"error": pr_err, "reason_code": pr_reason}

    rollup = info.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return False, {"error": "Missing exact head CI", "reason_code": "MISSING_EXACT_HEAD_CI"}

    res_ok, resolved_checks = _resolve_merge_required_checks(
        repo, pr_ref, expected_base_branch, worktree, runner, required_checks
    )
    if not res_ok:
        return False, {"error": "Required checks missing or could not be resolved", "reason_code": "CI_CHECK_MISSING"}

    ci_ok, ci_err = _verify_merge_ci(info, commit_sha, required_checks=resolved_checks)
    if not ci_ok:
        return False, {"error": "CI checks missing or failed", "reason_code": ci_err}

    merged, mc_oid = is_pr_already_merged(info)
    if merged:
        return True, {
            "reconciled_existing_effect": True,
            "state": "MERGED",
            "merge_commit": mc_oid,
            "merge_commit_sha": mc_oid,
            "merged_at": info.get("mergedAt"),
        }

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
