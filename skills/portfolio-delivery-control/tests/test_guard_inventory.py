"""Guard inventory and bidirectional sync verification.

Guarantees:
1. Every refusal reason code emitted by transitions.py, admission.py, and receipts.py
   is enumerated in the authoritative inventory.
2. Every code in the inventory is tested via a table-driven execution path that asserts
   status="refused", the exact reason_code, and verifiable zero side-effects.
3. Any added or removed refusal code without updating the inventory causes a test failure.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from admission import (  # noqa: E402
    admit_work,
    claim_work,
    record_implementation,
)
from receipts import (  # noqa: E402
    record_dispatch,
    record_review,
)
from transitions import execute_transition  # noqa: E402

BOT_AUTHOR = "claude-cir <claude-cir@selamy.dev>"
BOT_COMMITTER = "selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>"

TRANSITION_REFUSAL_CODES = {
    # Core stage guards in transitions.py
    "ABSENT_APPROVAL",
    "APPROVAL_DIGEST_MISMATCH",
    "APPROVAL_FILE_NOT_FOUND",
    "APPROVAL_READ_FAILED",
    "APPROVAL_WITHDRAWN",
    "BASE_BRANCH_MISMATCH",
    "CI_CHECK_FAILED",
    "CI_CHECK_MISSING",
    "CLAIM_EXPIRED",
    "DISALLOWED_MERGE_IDENTITY",
    "INVALID_ACTION",
    "MISSING_MANDATORY_GATE",
    "MISSING_OR_EXPIRED_CLAIM",
    "MISSING_QUALIFICATION",
    "MISSING_TRANSITION_EVIDENCE",
    "PROTECTED_BRANCH_PUSH",
    "STALE_CLAIM",
    "STALE_REVIEW_REVISION",
    "UNRESOLVED_REVISE_FINDINGS",
    "WORK_NOT_ADMITTED",
    # Provider-originated guards returned via execute_transition
    "CANDIDATE_REVISION_MISMATCH",
    "CREATED_PR_VIEW_FAILED",
    "MERGE_FAILED",
    "MERGE_NOT_COMPLETED",
    "MISSING_EXACT_HEAD_CI",
    "PROVIDER_AUTH_FAILED",
    "PROVIDER_EXECUTION_FAILED",
    "PR_HEAD_MISMATCH",
    "PR_NOT_OPEN",
    "PR_VIEW_FAILED",
    "REMOTE_HEAD_MISMATCH",
}

ADMISSION_REFUSAL_CODES = {
    "SPEC_FILE_NOT_FOUND",
    "TASK_NOT_FOUND_IN_SPEC",
    "TASK_ALREADY_COMPLETED",
    "DUPLICATE_WORK_ADMISSION",
    "WORK_NOT_ADMITTED",
    "INVALID_WORKTREE_PATH",
    "WORKTREE_NOT_FOUND",
    "WORKTREE_SCOPE_ESCAPE",
    "NOT_A_GIT_WORKTREE",
    "SESSION_REUSED_ACROSS_WRITERS",
    "WORKTREE_PATH_CONFLICT",
    "WORK_ITEM_ALREADY_CLAIMED",
    "INVALID_CLAIM_TOKEN",
    "CLAIM_EXPIRED",
    "INVALID_REVISION_SHA",
    "GIT_COMMIT_NOT_FOUND",
    "TREE_SHA_MISMATCH",
    "DISALLOWED_AUTHOR_IDENTITY",
    "DISALLOWED_COMMITTER_IDENTITY",
    "EMPTY_MODIFIED_PATHS",
    "PATH_SCOPE_ESCAPE",
    "MANDATORY_GATES_FAILED",
    "ARTIFACT_NOT_FOUND",
    "ARTIFACT_HASH_MISMATCH",
}

RECEIPT_REFUSAL_CODES = {
    "INVALID_FAMILY",
    "INVALID_DISPATCH_PARAMETERS",
    "DUPLICATE_DISPATCH_ID",
    "DUPLICATE_REVIEWER_SESSION",
    "SESSION_REUSED_ACROSS_ROLES",
    "WORK_NOT_ADMITTED",
    "ARTIFACT_NOT_FOUND",
    "ARTIFACT_HASH_MISMATCH",
    "REPORT_INVALID",
    "SCHEMA_EXTRA_KEYS_FORBIDDEN",
    "SCHEMA_MISSING_REQUIRED_KEYS",
    "SCHEMA_VERSION_UNSUPPORTED",
    "INVALID_DISPATCH_ID",
    "INVALID_REVIEWER_SESSION_ID",
    "UNAPPROVED_REVIEWER_MODEL",
    "INVALID_REVISION_SHA",
    "INVALID_VERDICT",
    "INVALID_FINDINGS_CONTAINER",
    "FINDING_SCHEMA_VIOLATION",
    "REPORT_IDENTITY_MISMATCH",
    "REPORT_VERDICT_MISMATCH",
    "REVIEWER_SESSION_MISMATCH",
    "MISSING_CANDIDATE_REVISION",
    "REVISION_MISMATCH",
    "SELF_REVIEW_FORBIDDEN",
    "UNBOUND_DISPATCH_ID",
    "DISPATCH_WORK_ID_MISMATCH",
    "DISPATCH_FAMILY_MISMATCH",
    "DISPATCH_SESSION_MISMATCH",
    "DUPLICATE_REVIEWER_FAMILY",
}


# ==============================================================================
# Transition Modifiers
# ==============================================================================


def _mod_invalid_action(ctx: dict[str, Any]) -> None:
    ctx["action"] = "nonexistent_action"


def _mod_protected_branch(ctx: dict[str, Any]) -> None:
    ctx["action"] = "push"
    ctx["target_b"] = "main"


def _mod_disallowed_merge_identity(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    ctx["merge_ident"] = "malicious-hacker"


def _mod_work_not_admitted(ctx: dict[str, Any]) -> None:
    ctx["work_id"] = "nonexistent-work-item"


def _mod_absent_approval(ctx: dict[str, Any]) -> None:
    ctx["wi"]["approval_ref"] = None
    ctx["save_doc"]()


def _mod_approval_file_not_found(ctx: dict[str, Any]) -> None:
    ctx["wi"]["approval_ref"] = str(Path(ctx["temp_dir"]) / "missing-approval.txt")
    ctx["save_doc"]()


def _mod_approval_read_failed(ctx: dict[str, Any]) -> None:
    dir_as_file = Path(ctx["temp_dir"]) / "dir_approval"
    dir_as_file.mkdir(parents=True, exist_ok=True)
    ctx["wi"]["approval_ref"] = str(dir_as_file)
    ctx["save_doc"]()


def _mod_approval_withdrawn(ctx: dict[str, Any]) -> None:
    withdrawn_file = Path(ctx["temp_dir"]) / "withdrawn_app.txt"
    withdrawn_file.write_text("Coordinator [WITHDRAWN] approval", encoding="utf-8")
    ctx["wi"]["approval_ref"] = str(withdrawn_file)
    ctx["save_doc"]()


def _mod_approval_digest_mismatch(ctx: dict[str, Any]) -> None:
    ctx["wi"]["approval_ref"] = {"path": str(ctx["approval_file"]), "sha256": "f" * 64}
    ctx["save_doc"]()


def _mod_unresolved_revise_findings(ctx: dict[str, Any]) -> None:
    ctx["wi"]["remediation_required"] = True
    ctx["wi"]["defects"] = [{"id": "DEF-01", "summary": "Must fix"}]
    ctx["save_doc"]()


def _mod_missing_or_expired_claim(ctx: dict[str, Any]) -> None:
    ctx["claim_token"] = None


def _mod_stale_claim(ctx: dict[str, Any]) -> None:
    ctx["claim_token"] = "wrong-stale-token"


def _mod_claim_expired(ctx: dict[str, Any]) -> None:
    ctx["wi"]["claim"]["expires_at"] = "2020-01-01T00:00:00Z"
    ctx["save_doc"]()


def _mod_missing_qualification(ctx: dict[str, Any]) -> None:
    ctx["wi"]["qualified"] = False
    ctx["save_doc"]()


def _mod_stale_review_revision(ctx: dict[str, Any]) -> None:
    stale_rec = ctx["state_dir"] / "receipts" / f"review_{ctx['work_id']}_opus_sess-r1.json"
    rec_content = json.loads(stale_rec.read_text(encoding="utf-8"))
    rec_content["commit_sha"] = "9" * 40
    stale_rec.write_text(json.dumps(rec_content), encoding="utf-8")


def _mod_missing_mandatory_gate(ctx: dict[str, Any]) -> None:
    ctx["wi"]["candidate"]["gate_results"]["pytest"] = {"exit_code": 1, "passed": False}
    ctx["save_doc"]()


def _mod_missing_transition_evidence(ctx: dict[str, Any]) -> None:
    ctx["wi"]["transitions"] = []
    ctx["save_doc"]()


def _mod_ci_check_failed(ctx: dict[str, Any]) -> None:
    ctx["action"] = "complete_spec_task"
    c_sha = ctx["c_sha"]

    def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if "baseRefName" in cmd_str:
            return 0, json.dumps({"baseRefName": "main"})
        if "required_status_checks" in cmd_str:
            return 0, json.dumps({"contexts": ["ci/test"]})
        if "rules/branches" in cmd_str:
            return 0, json.dumps([])
        if "statusCheckRollup" in cmd_str:
            return 0, json.dumps(
                {
                    "headRefOid": c_sha,
                    "statusCheckRollup": [
                        {"name": "ci/test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": c_sha}
                    ],
                }
            )
        return 0, ""

    ctx["cmd_runner"] = runner


def _mod_ci_check_missing(ctx: dict[str, Any]) -> None:
    ctx["action"] = "complete_spec_task"
    c_sha = ctx["c_sha"]

    def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if "baseRefName" in cmd_str:
            return 0, json.dumps({"baseRefName": "main"})
        if "required_status_checks" in cmd_str:
            return 0, json.dumps({"contexts": ["Quality"]})
        if "rules/branches" in cmd_str:
            return 0, json.dumps([])
        if "statusCheckRollup" in cmd_str:
            return 0, json.dumps(
                {
                    "headRefOid": c_sha,
                    "statusCheckRollup": [
                        {"name": "unrelated", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c_sha}
                    ],
                }
            )
        return 0, ""

    ctx["cmd_runner"] = runner


def _mod_pr_not_open(ctx: dict[str, Any]) -> None:
    ctx["action"] = "publish_pr"
    c_sha = ctx["c_sha"]

    def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if "pr view" in cmd_str:
            return 0, json.dumps(
                {"number": 123, "state": "CLOSED", "headRefOid": c_sha, "url": "https://github.com/org/repo/pull/123"}
            )
        return 0, ""

    ctx["cmd_runner"] = runner


def _mod_remote_head_mismatch(ctx: dict[str, Any]) -> None:
    ctx["action"] = "publish_pr"

    def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if "pr list" in cmd_str:
            return 0, json.dumps([])
        if "ls-remote" in cmd_str:
            return 0, f"{'9' * 40}\trefs/heads/feat/inventory\n"
        return 0, ""

    ctx["cmd_runner"] = runner


def _mod_created_pr_view_failed(ctx: dict[str, Any]) -> None:
    ctx["action"] = "publish_pr"
    c_sha = ctx["c_sha"]

    def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if "pr list" in cmd_str:
            return 0, json.dumps([])
        if "ls-remote" in cmd_str:
            return 0, f"{c_sha}\trefs/heads/feat/inventory\n"
        if "pr create" in cmd_str:
            return 0, "https://github.com/org/repo/pull/152"
        if "pr view" in cmd_str:
            return 1, "Failed to query created PR"
        return 0, ""

    ctx["cmd_runner"] = runner


def _merge_pr_view_cmd(
    cmd_str: str,
    c_sha: str,
    head_mismatch: bool,
    view_fail: bool,
    cand_mismatch: bool,
    queued: bool,
    merged_flag: list[bool] | None,
) -> tuple[int, str]:
    if view_fail:
        return 1, "pr view query failed"
    if head_mismatch:
        return 0, json.dumps({"headRefOid": "0" * 40, "state": "OPEN", "baseRefName": "main"})
    if cand_mismatch and merged_flag and merged_flag[0]:
        return 0, json.dumps(
            {
                "headRefOid": "8" * 40,
                "state": "MERGED",
                "baseRefName": "main",
                "mergedAt": "2026-11-03T12:00:00Z",
                "mergeCommit": {"oid": "3" * 40},
            }
        )
    if queued and merged_flag and merged_flag[0]:
        return 0, json.dumps(
            {
                "headRefOid": c_sha,
                "state": "OPEN",
                "baseRefName": "main",
                "statusCheckRollup": [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            }
        )
    return 0, json.dumps(
        {
            "headRefOid": c_sha,
            "state": "OPEN",
            "baseRefName": "main",
            "statusCheckRollup": [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        }
    )


def _handle_pr_merge_cmd(merge_fail: bool, merged_flag: list[bool] | None) -> tuple[int, str]:
    if merge_fail:
        return 1, "Merge conflict or branch protected"
    if merged_flag is not None:
        merged_flag[0] = True
    return 0, "Merged"


def _merge_pr_driver_runner(
    cmd: list[str],
    c_sha: str,
    *,
    head_mismatch: bool = False,
    view_fail: bool = False,
    merge_fail: bool = False,
    queued: bool = False,
    cand_mismatch: bool = False,
    merged_flag: list[bool] | None = None,
) -> tuple[int, str]:
    cmd_str = " ".join(cmd)
    if view_fail and "pr view" in cmd_str:
        return 1, "pr view query failed"
    if "api user" in cmd_str:
        return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
    if "auth status" in cmd_str:
        return 0, "Logged in to github.com as selamy-labs-bot"
    if "required_status_checks" in cmd_str:
        return 0, json.dumps({"contexts": ["test"]})
    if "rules/branches" in cmd_str:
        return 0, json.dumps([])
    if "mergeCommit" in cmd_str:
        return _merge_pr_view_cmd(cmd_str, c_sha, head_mismatch, view_fail, cand_mismatch, queued, merged_flag)
    if "statusCheckRollup" in cmd_str:
        return 0, json.dumps(
            {
                "headRefOid": c_sha,
                "statusCheckRollup": [
                    {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c_sha}
                ],
            }
        )
    if "baseRefName" in cmd_str:
        return 0, json.dumps({"baseRefName": "main"})
    if "pr merge" in cmd_str:
        return _handle_pr_merge_cmd(merge_fail, merged_flag)
    return 0, ""


def _auth_failed_cmd(cmd_str: str, c_sha: str) -> tuple[int, str]:
    if "baseRefName" in cmd_str:
        return 0, json.dumps({"baseRefName": "main"})
    if "api user" in cmd_str:
        return 1, "unauthorized"
    if "auth status" in cmd_str:
        return 1, "not logged in"
    if "required_status_checks" in cmd_str:
        return 0, json.dumps({"contexts": ["test"]})
    if "rules/branches" in cmd_str:
        return 0, json.dumps([])
    if "statusCheckRollup" in cmd_str:
        return 0, json.dumps(
            {
                "headRefOid": c_sha,
                "statusCheckRollup": [
                    {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c_sha}
                ],
            }
        )
    return 0, ""


def _mod_provider_auth_failed(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    c_sha = ctx["c_sha"]
    ctx["cmd_runner"] = lambda cmd, cwd=None: _auth_failed_cmd(" ".join(cmd), c_sha)


def _missing_exact_head_ci_cmd(cmd_str: str, c_sha: str) -> tuple[int, str]:
    if "api user" in cmd_str:
        return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
    if "auth status" in cmd_str:
        return 0, "Logged in to github.com as selamy-labs-bot"
    if "required_status_checks" in cmd_str:
        return 0, json.dumps({"contexts": ["test"]})
    if "rules/branches" in cmd_str:
        return 0, json.dumps([])
    if "statusCheckRollup" in cmd_str:
        return 0, json.dumps({"headRefOid": c_sha, "state": "OPEN", "baseRefName": "main", "statusCheckRollup": None})
    if "baseRefName" in cmd_str:
        return 0, json.dumps({"baseRefName": "main"})
    return 0, ""


def _mod_missing_exact_head_ci(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    c_sha = ctx["c_sha"]
    ctx["cmd_runner"] = lambda cmd, cwd=None: _missing_exact_head_ci_cmd(" ".join(cmd), c_sha)


def _mod_pr_head_mismatch(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    ctx["cmd_runner"] = lambda cmd, cwd=None: _merge_pr_driver_runner(cmd, ctx["c_sha"], head_mismatch=True)


def _mod_pr_view_failed(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    ctx["cmd_runner"] = lambda cmd, cwd=None: _merge_pr_driver_runner(cmd, ctx["c_sha"], view_fail=True)


def _mod_merge_failed(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    ctx["cmd_runner"] = lambda cmd, cwd=None: _merge_pr_driver_runner(cmd, ctx["c_sha"], merge_fail=True)


def _mod_merge_not_completed(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    merged = [False]
    ctx["cmd_runner"] = lambda cmd, cwd=None: _merge_pr_driver_runner(
        cmd, ctx["c_sha"], queued=True, merged_flag=merged
    )


def _mod_candidate_revision_mismatch(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    merged = [False]
    ctx["cmd_runner"] = lambda cmd, cwd=None: _merge_pr_driver_runner(
        cmd, ctx["c_sha"], cand_mismatch=True, merged_flag=merged
    )


def _mod_base_branch_mismatch(ctx: dict[str, Any]) -> None:
    ctx["action"] = "merge_pr"
    c_sha = ctx["c_sha"]

    def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if "api user" in cmd_str:
            return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
        if "auth status" in cmd_str:
            return 0, "Logged in to github.com as selamy-labs-bot"
        if "required_status_checks" in cmd_str:
            return 0, json.dumps({"contexts": ["test"]})
        if "rules/branches" in cmd_str:
            return 0, json.dumps([])
        if "pr view" in cmd_str:
            return 0, json.dumps(
                {
                    "headRefOid": c_sha,
                    "state": "OPEN",
                    "baseRefName": "mismatched-base-branch",
                    "statusCheckRollup": [
                        {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c_sha}
                    ],
                }
            )
        return 0, ""

    ctx["cmd_runner"] = runner


def _mod_provider_execution_failed(ctx: dict[str, Any]) -> None:
    ctx["action"] = "push"

    def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if "ls-remote" in cmd_str:
            return 0, ""
        if "push" in cmd_str:
            return 1, "fatal: remote error: push rejected"
        return 0, ""

    ctx["cmd_runner"] = runner


TRANS_MODIFIERS = {
    "INVALID_ACTION": _mod_invalid_action,
    "PROTECTED_BRANCH_PUSH": _mod_protected_branch,
    "DISALLOWED_MERGE_IDENTITY": _mod_disallowed_merge_identity,
    "WORK_NOT_ADMITTED": _mod_work_not_admitted,
    "ABSENT_APPROVAL": _mod_absent_approval,
    "APPROVAL_FILE_NOT_FOUND": _mod_approval_file_not_found,
    "APPROVAL_READ_FAILED": _mod_approval_read_failed,
    "APPROVAL_WITHDRAWN": _mod_approval_withdrawn,
    "APPROVAL_DIGEST_MISMATCH": _mod_approval_digest_mismatch,
    "UNRESOLVED_REVISE_FINDINGS": _mod_unresolved_revise_findings,
    "MISSING_OR_EXPIRED_CLAIM": _mod_missing_or_expired_claim,
    "STALE_CLAIM": _mod_stale_claim,
    "CLAIM_EXPIRED": _mod_claim_expired,
    "MISSING_QUALIFICATION": _mod_missing_qualification,
    "STALE_REVIEW_REVISION": _mod_stale_review_revision,
    "MISSING_MANDATORY_GATE": _mod_missing_mandatory_gate,
    "MISSING_TRANSITION_EVIDENCE": _mod_missing_transition_evidence,
    "CI_CHECK_FAILED": _mod_ci_check_failed,
    "CI_CHECK_MISSING": _mod_ci_check_missing,
    "BASE_BRANCH_MISMATCH": _mod_base_branch_mismatch,
    "CANDIDATE_REVISION_MISMATCH": _mod_candidate_revision_mismatch,
    "CREATED_PR_VIEW_FAILED": _mod_created_pr_view_failed,
    "MERGE_FAILED": _mod_merge_failed,
    "MERGE_NOT_COMPLETED": _mod_merge_not_completed,
    "MISSING_EXACT_HEAD_CI": _mod_missing_exact_head_ci,
    "PROVIDER_AUTH_FAILED": _mod_provider_auth_failed,
    "PROVIDER_EXECUTION_FAILED": _mod_provider_execution_failed,
    "PR_HEAD_MISMATCH": _mod_pr_head_mismatch,
    "PR_NOT_OPEN": _mod_pr_not_open,
    "PR_VIEW_FAILED": _mod_pr_view_failed,
    "REMOTE_HEAD_MISMATCH": _mod_remote_head_mismatch,
}


# ==============================================================================
# Admission Drivers
# ==============================================================================


def _adm_spec_file_not_found(tc: Any, code: str) -> dict[str, Any]:
    return admit_work(
        control_dir=tc.state_dir,
        repo="selamy-labs/agent-skills",
        issue_num="152",
        spec_path=Path(tc.temp_dir) / "nonexistent-spec.md",
        task_id=f"TASK_{code}",
    )


def _adm_task_not_found_in_spec(tc: Any, code: str) -> dict[str, Any]:
    return admit_work(
        control_dir=tc.state_dir,
        repo="selamy-labs/agent-skills",
        issue_num="152",
        spec_path=tc.spec_file,
        task_id=f"TASK_NOT_IN_SPEC_{code}",
    )


def _adm_task_already_completed(tc: Any, code: str) -> dict[str, Any]:
    done_spec = Path(tc.temp_dir) / "done_spec.md"
    done_spec.write_text(f"- [x] TASK_{code}\n", encoding="utf-8")
    return admit_work(
        control_dir=tc.state_dir,
        repo="selamy-labs/agent-skills",
        issue_num="152",
        spec_path=done_spec,
        task_id=f"TASK_{code}",
    )


def _adm_duplicate_work_admission(tc: Any, code: str) -> dict[str, Any]:
    task_id = f"TASK_{code}"
    tc.spec_file.write_text(f"- [ ] {task_id}\n", encoding="utf-8")
    admit_work(
        control_dir=tc.state_dir,
        repo="selamy-labs/agent-skills",
        issue_num="152",
        spec_path=tc.spec_file,
        task_id=task_id,
    )
    tc.snapshot_doc()
    return admit_work(
        control_dir=tc.state_dir,
        repo="selamy-labs/agent-skills",
        issue_num="152",
        spec_path=tc.spec_file,
        task_id=task_id,
    )


def _adm_work_not_admitted(tc: Any, code: str) -> dict[str, Any]:
    return claim_work(
        control_dir=tc.state_dir,
        work_id="selamy-labs-agent-skills-152-NONEXISTENT",
        owner="lane-01",
        session_id="sess-01",
        worktree=tc.worktree,
    )


def _adm_invalid_worktree_path(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_admitted_work(code)
    return claim_work(
        control_dir=tc.state_dir,
        work_id=work_id,
        owner="lane-01",
        session_id="sess-01",
        worktree="",
    )


def _adm_worktree_not_found(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_admitted_work(code)
    return claim_work(
        control_dir=tc.state_dir,
        work_id=work_id,
        owner="lane-01",
        session_id="sess-01",
        worktree=Path(tc.temp_dir) / "nonexistent-worktree-dir",
    )


def _adm_worktree_scope_escape(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_admitted_work(code)
    return claim_work(
        control_dir=tc.state_dir,
        work_id=work_id,
        owner="lane-01",
        session_id="sess-01",
        worktree="/var",
    )


def _adm_not_a_git_worktree(tc: Any, code: str) -> dict[str, Any]:
    non_git = Path(tc.temp_dir) / f"non_git_{code}"
    non_git.mkdir(parents=True, exist_ok=True)
    work_id = tc._setup_admitted_work(code)
    return claim_work(
        control_dir=tc.state_dir,
        work_id=work_id,
        owner="lane-01",
        session_id="sess-01",
        worktree=non_git,
    )


def _adm_session_reused_across_writers(tc: Any, code: str) -> dict[str, Any]:
    w1 = tc._setup_admitted_work(f"{code}_1")
    w2 = tc._setup_admitted_work(f"{code}_2")
    wt2 = Path(tc.temp_dir) / f"wt_{code}_2"
    wt2.mkdir(parents=True, exist_ok=True)
    (wt2 / ".git").mkdir(parents=True, exist_ok=True)
    claim_work(
        control_dir=tc.state_dir,
        work_id=w1,
        owner="lane-01",
        session_id="sess-shared",
        worktree=tc.worktree,
    )
    tc.snapshot_doc()
    return claim_work(
        control_dir=tc.state_dir,
        work_id=w2,
        owner="lane-02",
        session_id="sess-shared",
        worktree=wt2,
    )


def _adm_worktree_path_conflict(tc: Any, code: str) -> dict[str, Any]:
    w1 = tc._setup_admitted_work(f"{code}_1")
    w2 = tc._setup_admitted_work(f"{code}_2")
    claim_work(
        control_dir=tc.state_dir,
        work_id=w1,
        owner="lane-01",
        session_id="sess-01",
        worktree=tc.worktree,
    )
    tc.snapshot_doc()
    return claim_work(
        control_dir=tc.state_dir,
        work_id=w2,
        owner="lane-02",
        session_id="sess-02",
        worktree=tc.worktree,
    )


def _adm_work_item_already_claimed(tc: Any, code: str) -> dict[str, Any]:
    w1 = tc._setup_admitted_work(code)
    wt2 = Path(tc.temp_dir) / f"wt2_{code}"
    wt2.mkdir(parents=True, exist_ok=True)
    (wt2 / ".git").mkdir(parents=True, exist_ok=True)
    claim_work(
        control_dir=tc.state_dir,
        work_id=w1,
        owner="lane-01",
        session_id="sess-01",
        worktree=tc.worktree,
    )
    tc.snapshot_doc()
    return claim_work(
        control_dir=tc.state_dir,
        work_id=w1,
        owner="lane-02",
        session_id="sess-02",
        worktree=wt2,
    )


def _adm_invalid_claim_token(tc: Any, code: str) -> dict[str, Any]:
    work_id, _ = tc._setup_claimed_work(code)
    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token="wrong-claim-token",
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        runner=tc._default_impl_runner,
    )


def _adm_claim_expired(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)
    status_file = tc.state_dir / "coordinator-status.json"
    doc = json.loads(status_file.read_text(encoding="utf-8"))
    doc["delivery_control"]["work_items"][work_id]["claim"]["expires_at"] = "2020-01-01T00:00:00Z"
    status_file.write_text(json.dumps(doc), encoding="utf-8")
    tc.snapshot_doc()
    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        runner=tc._default_impl_runner,
    )


def _adm_invalid_revision_sha(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)
    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha="not-a-sha",
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        runner=tc._default_impl_runner,
    )


def _adm_git_commit_not_found(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)
    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        runner=lambda cmd, cwd=None: (1, "fatal: bad object"),
    )


def _adm_tree_sha_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)

    def runner(cmd: Any, cwd: Any = None) -> tuple[int, str]:
        if "log" in cmd:
            return 0, f"{BOT_AUTHOR}\n{BOT_COMMITTER}\n{'9' * 40}\n"
        return 0, ""

    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        runner=runner,
    )


def _adm_disallowed_author_identity(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)

    def runner(cmd: Any, cwd: Any = None) -> tuple[int, str]:
        if "log" in cmd:
            return 0, f"untrusted <untrusted@hacker.com>\n{BOT_COMMITTER}\n{tc.t_sha}\n"
        return 0, ""

    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        runner=runner,
    )


def _adm_disallowed_committer_identity(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)

    def runner(cmd: Any, cwd: Any = None) -> tuple[int, str]:
        if "log" in cmd:
            return 0, f"{BOT_AUTHOR}\nuntrusted <untrusted@hacker.com>\n{tc.t_sha}\n"
        return 0, ""

    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        runner=runner,
    )


def _adm_empty_modified_paths(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)

    def runner(cmd: Any, cwd: Any = None) -> tuple[int, str]:
        if "log" in cmd:
            return 0, f"{BOT_AUTHOR}\n{BOT_COMMITTER}\n{tc.t_sha}\n"
        if "diff-tree" in cmd:
            return 0, ""
        return 0, ""

    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        runner=runner,
    )


def _adm_path_scope_escape(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)
    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        modified_paths=["skills/forbidden.py"],
        runner=tc._default_impl_runner,
    )


def _adm_mandatory_gates_failed(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)
    bad_gates = dict(tc.valid_gates)
    bad_gates["pytest"] = {"exit_code": 1, "passed": False}
    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=bad_gates,
        runner=tc._default_impl_runner,
    )


def _adm_artifact_not_found(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)
    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        patch_path=Path(tc.temp_dir) / "missing.patch",
        patch_sha256="abc",
        runner=tc._default_impl_runner,
    )


def _adm_artifact_hash_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id, token = tc._setup_claimed_work(code)
    patch_file = Path(tc.temp_dir) / f"patch_{code}.patch"
    patch_file.write_text("patch content", encoding="utf-8")
    return record_implementation(
        control_dir=tc.state_dir,
        work_id=work_id,
        claim_token=token,
        commit_sha=tc.c_sha,
        tree_sha=tc.t_sha,
        gate_results=tc.valid_gates,
        patch_path=patch_file,
        patch_sha256="f" * 64,
        runner=tc._default_impl_runner,
    )


ADMISSION_DRIVERS = {
    "SPEC_FILE_NOT_FOUND": _adm_spec_file_not_found,
    "TASK_NOT_FOUND_IN_SPEC": _adm_task_not_found_in_spec,
    "TASK_ALREADY_COMPLETED": _adm_task_already_completed,
    "DUPLICATE_WORK_ADMISSION": _adm_duplicate_work_admission,
    "WORK_NOT_ADMITTED": _adm_work_not_admitted,
    "INVALID_WORKTREE_PATH": _adm_invalid_worktree_path,
    "WORKTREE_NOT_FOUND": _adm_worktree_not_found,
    "WORKTREE_SCOPE_ESCAPE": _adm_worktree_scope_escape,
    "NOT_A_GIT_WORKTREE": _adm_not_a_git_worktree,
    "SESSION_REUSED_ACROSS_WRITERS": _adm_session_reused_across_writers,
    "WORKTREE_PATH_CONFLICT": _adm_worktree_path_conflict,
    "WORK_ITEM_ALREADY_CLAIMED": _adm_work_item_already_claimed,
    "INVALID_CLAIM_TOKEN": _adm_invalid_claim_token,
    "CLAIM_EXPIRED": _adm_claim_expired,
    "INVALID_REVISION_SHA": _adm_invalid_revision_sha,
    "GIT_COMMIT_NOT_FOUND": _adm_git_commit_not_found,
    "TREE_SHA_MISMATCH": _adm_tree_sha_mismatch,
    "DISALLOWED_AUTHOR_IDENTITY": _adm_disallowed_author_identity,
    "DISALLOWED_COMMITTER_IDENTITY": _adm_disallowed_committer_identity,
    "EMPTY_MODIFIED_PATHS": _adm_empty_modified_paths,
    "PATH_SCOPE_ESCAPE": _adm_path_scope_escape,
    "MANDATORY_GATES_FAILED": _adm_mandatory_gates_failed,
    "ARTIFACT_NOT_FOUND": _adm_artifact_not_found,
    "ARTIFACT_HASH_MISMATCH": _adm_artifact_hash_mismatch,
}


# ==============================================================================
# Receipt Drivers
# ==============================================================================


def _rec_invalid_family(tc: Any, code: str) -> dict[str, Any]:
    return record_dispatch(tc.state_dir, "work_id", "disp_01", family="invalid_family", reviewer_session_id="sess-01")


def _rec_invalid_dispatch_parameters(tc: Any, code: str) -> dict[str, Any]:
    return record_dispatch(tc.state_dir, "work_id", "", family="opus", reviewer_session_id="sess-01")


def _rec_duplicate_dispatch_id(tc: Any, code: str) -> dict[str, Any]:
    record_dispatch(tc.state_dir, "work_id", "disp-dup", family="opus", reviewer_session_id="sess-01")
    tc.snapshot_doc()
    return record_dispatch(tc.state_dir, "work_id", "disp-dup", family="sol", reviewer_session_id="sess-02")


def _rec_duplicate_reviewer_session(tc: Any, code: str) -> dict[str, Any]:
    record_dispatch(tc.state_dir, "work_id", "disp-01", family="opus", reviewer_session_id="sess-dup")
    tc.snapshot_doc()
    return record_dispatch(tc.state_dir, "work_id", "disp-02", family="sol", reviewer_session_id="sess-dup")


def _rec_session_reused_across_roles(tc: Any, code: str) -> dict[str, Any]:
    work_id, _ = tc._setup_claimed_work(code)
    return record_dispatch(tc.state_dir, work_id, "disp-role", family="opus", reviewer_session_id="sess-01")


def _rec_work_not_admitted(tc: Any, code: str) -> dict[str, Any]:
    rep_file, rep_sha = tc._write_report({"verdict": "SHIP"})
    return tc._call_record_review("selamy-labs-agent-skills-152-NONEXISTENT", rep_file, rep_sha)


def _rec_artifact_not_found(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    return tc._call_record_review(work_id, Path(tc.temp_dir) / "missing_report.json", "abc")


def _rec_artifact_hash_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, _ = tc._write_report({"verdict": "SHIP"})
    return tc._call_record_review(work_id, rep_file, "f" * 64)


def _rec_report_invalid(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    bad_rep = Path(tc.temp_dir) / f"bad_rep_{code}.json"
    bad_rep.write_text("{bad json", encoding="utf-8")
    bad_sha = hashlib.sha256(bad_rep.read_bytes()).hexdigest()
    return tc._call_record_review(work_id, bad_rep, bad_sha)


def _rec_schema_extra_keys_forbidden(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"extra_key": "forbidden"})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_schema_missing_required_keys(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file = Path(tc.temp_dir) / f"missing_keys_{code}.json"
    rep_file.write_text(json.dumps({"schema": "review-receipt/v1"}), encoding="utf-8")
    rep_sha = hashlib.sha256(rep_file.read_bytes()).hexdigest()
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_schema_version_unsupported(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"schema": "v0"})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_invalid_dispatch_id(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"dispatch_id": ""})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_invalid_reviewer_session_id(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"reviewer_session_id": ""})
    return tc._call_record_review(work_id, rep_file, rep_sha, session_id="")


def _rec_unapproved_reviewer_model(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"model": "unapproved-model"})
    return tc._call_record_review(work_id, rep_file, rep_sha, model="unapproved-model")


def _rec_invalid_revision_sha(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"commit_sha": "not-a-sha"})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_invalid_verdict(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"verdict": "MAYBE"})
    return tc._call_record_review(work_id, rep_file, rep_sha, verdict="MAYBE")


def _rec_invalid_findings_container(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"findings": "not-a-list"})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_finding_schema_violation(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"findings": [{"bad": 1}]})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_report_identity_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"family": "sol", "model": "gpt-6.1-sol-high"})
    return tc._call_record_review(work_id, rep_file, rep_sha, family="opus", model="claude-opus-5-5")


def _rec_report_verdict_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"verdict": "REVISE"})
    return tc._call_record_review(work_id, rep_file, rep_sha, verdict="SHIP")


def _rec_reviewer_session_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"reviewer_session_id": "sess-different"})
    return tc._call_record_review(work_id, rep_file, rep_sha, session_id="sess-r1")


def _rec_missing_candidate_revision(tc: Any, code: str) -> dict[str, Any]:
    work_id, _ = tc._setup_claimed_work(code)
    tc._add_ledger_entry("disp-01", work_id, "opus", "sess-r1")
    rep_file, rep_sha = tc._write_report({"verdict": "SHIP"})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_revision_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    tc._add_ledger_entry("disp-01", work_id, "opus", "sess-r1")
    rep_file, rep_sha = tc._write_report({"commit_sha": "3" * 40})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_self_review_forbidden(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    tc._add_ledger_entry("disp-01", work_id, "opus", "sess-01")
    rep_file, rep_sha = tc._write_report({"reviewer_session_id": "sess-01"})
    return tc._call_record_review(work_id, rep_file, rep_sha, session_id="sess-01")


def _rec_unbound_dispatch_id(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    rep_file, rep_sha = tc._write_report({"dispatch_id": "disp-unbound"})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_dispatch_work_id_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    tc._add_ledger_entry("disp-01", "other-work-id", "opus", "sess-r1")
    rep_file, rep_sha = tc._write_report({"verdict": "SHIP"})
    return tc._call_record_review(work_id, rep_file, rep_sha)


def _rec_dispatch_family_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    tc._add_ledger_entry("disp-01", work_id, "sol", "sess-r1")
    rep_file, rep_sha = tc._write_report({"verdict": "SHIP"})
    return tc._call_record_review(work_id, rep_file, rep_sha, family="opus")


def _rec_dispatch_session_mismatch(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    tc._add_ledger_entry("disp-01", work_id, "opus", "sess-different")
    rep_file, rep_sha = tc._write_report({"verdict": "SHIP"})
    return tc._call_record_review(work_id, rep_file, rep_sha, session_id="sess-r1")


def _rec_duplicate_reviewer_family(tc: Any, code: str) -> dict[str, Any]:
    work_id = tc._setup_candidate_work(code)
    tc._add_ledger_entry("disp-01", work_id, "opus", "sess-r1")
    status_file = tc.state_dir / "coordinator-status.json"
    doc = json.loads(status_file.read_text(encoding="utf-8"))
    wi = doc["delivery_control"]["work_items"][work_id]
    wi["reviews"] = [{"family": "opus", "reviewer_session_id": "sess-r0", "dispatch_id": "disp-00"}]
    status_file.write_text(json.dumps(doc), encoding="utf-8")
    rep_file, rep_sha = tc._write_report({"verdict": "SHIP"})
    return tc._call_record_review(work_id, rep_file, rep_sha, family="opus")


RECEIPT_DRIVERS = {
    "INVALID_FAMILY": _rec_invalid_family,
    "INVALID_DISPATCH_PARAMETERS": _rec_invalid_dispatch_parameters,
    "DUPLICATE_DISPATCH_ID": _rec_duplicate_dispatch_id,
    "DUPLICATE_REVIEWER_SESSION": _rec_duplicate_reviewer_session,
    "SESSION_REUSED_ACROSS_ROLES": _rec_session_reused_across_roles,
    "WORK_NOT_ADMITTED": _rec_work_not_admitted,
    "ARTIFACT_NOT_FOUND": _rec_artifact_not_found,
    "ARTIFACT_HASH_MISMATCH": _rec_artifact_hash_mismatch,
    "REPORT_INVALID": _rec_report_invalid,
    "SCHEMA_EXTRA_KEYS_FORBIDDEN": _rec_schema_extra_keys_forbidden,
    "SCHEMA_MISSING_REQUIRED_KEYS": _rec_schema_missing_required_keys,
    "SCHEMA_VERSION_UNSUPPORTED": _rec_schema_version_unsupported,
    "INVALID_DISPATCH_ID": _rec_invalid_dispatch_id,
    "INVALID_REVIEWER_SESSION_ID": _rec_invalid_reviewer_session_id,
    "UNAPPROVED_REVIEWER_MODEL": _rec_unapproved_reviewer_model,
    "INVALID_REVISION_SHA": _rec_invalid_revision_sha,
    "INVALID_VERDICT": _rec_invalid_verdict,
    "INVALID_FINDINGS_CONTAINER": _rec_invalid_findings_container,
    "FINDING_SCHEMA_VIOLATION": _rec_finding_schema_violation,
    "REPORT_IDENTITY_MISMATCH": _rec_report_identity_mismatch,
    "REPORT_VERDICT_MISMATCH": _rec_report_verdict_mismatch,
    "REVIEWER_SESSION_MISMATCH": _rec_reviewer_session_mismatch,
    "MISSING_CANDIDATE_REVISION": _rec_missing_candidate_revision,
    "REVISION_MISMATCH": _rec_revision_mismatch,
    "SELF_REVIEW_FORBIDDEN": _rec_self_review_forbidden,
    "UNBOUND_DISPATCH_ID": _rec_unbound_dispatch_id,
    "DISPATCH_WORK_ID_MISMATCH": _rec_dispatch_work_id_mismatch,
    "DISPATCH_FAMILY_MISMATCH": _rec_dispatch_family_mismatch,
    "DISPATCH_SESSION_MISMATCH": _rec_dispatch_session_mismatch,
    "DUPLICATE_REVIEWER_FAMILY": _rec_duplicate_reviewer_family,
}


# ==============================================================================
# Test Class
# ==============================================================================


class _CodeVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.var_map: dict[str, str] = {}
        self.codes: set[str] = set()

    def _extract_string_value(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            resolved = self._resolve_joined_str(node)
            return resolved[0] if resolved else None
        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        val = self._extract_string_value(node.value)
        if val:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.var_map[target.id] = val
        self.generic_visit(node)

    def _resolve_formatted_part(self, part: ast.FormattedValue) -> str | None:
        if isinstance(part.value, ast.Constant) and isinstance(part.value.value, str):
            return part.value.value
        if isinstance(part.value, ast.Name):
            return self.var_map.get(part.value.id)
        return None

    def _resolve_joined_part(self, part: ast.AST) -> str | None:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            return part.value
        if isinstance(part, ast.FormattedValue):
            return self._resolve_formatted_part(part)
        return None

    def _resolve_joined_str(self, node: ast.JoinedStr) -> list[str]:
        parts: list[str] = []
        for part in node.values:
            resolved = self._resolve_joined_part(part)
            if resolved is None:
                return []
            parts.append(resolved)
        return ["".join(parts)]

    def _resolve_codes(self, node: ast.AST | None) -> list[str]:
        if node is None:
            return []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name) and node.id in self.var_map:
            return [self.var_map[node.id]]
        if isinstance(node, ast.IfExp):
            return self._resolve_codes(node.body) + self._resolve_codes(node.orelse)
        if isinstance(node, ast.JoinedStr):
            return self._resolve_joined_str(node)
        return []

    def _add_if_valid(self, node: ast.AST | None) -> None:
        for val in self._resolve_codes(node):
            if val and val.isupper() and "_" in val:
                self.codes.add(val)

    def visit_Dict(self, node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "reason_code":
                self._add_if_valid(v)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value and isinstance(node.value, ast.Tuple) and len(node.value.elts) >= 2:
            first = node.value.elts[0]
            if isinstance(first, ast.Constant) and first.value is False:
                self._add_if_valid(node.value.elts[1])
        elif isinstance(node.value, ast.IfExp):
            self._add_if_valid(node.value.body)
            self._add_if_valid(node.value.orelse)
        self.generic_visit(node)

    def _visit_make_refusal_call(self, node: ast.Call) -> None:
        if len(node.args) >= 3:
            self._add_if_valid(node.args[2])

    def _visit_dict_call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "reason_code":
                self._add_if_valid(kw.value)

    def _visit_get_call(self, node: ast.Call) -> None:
        if len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) and node.args[0].value == "reason_code":
            self._add_if_valid(node.args[1])

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id == "make_refusal":
                self._visit_make_refusal_call(node)
            elif node.func.id == "dict":
                self._visit_dict_call(node)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            self._visit_get_call(node)
        self.generic_visit(node)


def extract_refusal_codes_from_ast(source_text: str) -> set[str]:
    tree = ast.parse(source_text)
    visitor = _CodeVisitor()
    visitor.visit(tree)
    return visitor.codes


class TestGuardInventory(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="pdc-guard-inventory-")
        self.state_dir = Path(self.temp_dir) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.worktree = Path(self.temp_dir) / "worktree"
        self.worktree.mkdir(parents=True, exist_ok=True)
        (self.worktree / ".git").mkdir(parents=True, exist_ok=True)

        self.spec_file = self.worktree / "SPEC.md"
        self.spec_file.write_text("- [ ] TASK_01: Verify inventory\n", encoding="utf-8")

        self.approval_file = Path(self.temp_dir) / "approval.txt"
        self.approval_file.write_text("APPROVED by coordinator\n", encoding="utf-8")

        self.c_sha = "1" * 40
        self.t_sha = "2" * 40
        self.claim_token = "claim-inventory-token-12345"

        self.valid_gates = {
            "pytest": {"exit_code": 0, "passed": True},
            "complexipy": {"exit_code": 0, "passed": True},
            "vulture": {"exit_code": 0, "passed": True},
            "ruff_check": {"exit_code": 0, "passed": True},
            "ruff_format": {"exit_code": 0, "passed": True},
            "privacy_scan": {"exit_code": 0, "passed": True},
            "security_scan": {"exit_code": 0, "passed": True},
            "lint_skills": {"exit_code": 0, "passed": True},
            "coverage": 95.0,
        }
        self.doc_before = None

    def snapshot_doc(self) -> None:
        status_file = self.state_dir / "coordinator-status.json"
        if status_file.exists():
            self.doc_before = json.loads(status_file.read_text(encoding="utf-8"))
        else:
            self.doc_before = None

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _default_impl_runner(self, cmd: Any, cwd: Any = None) -> tuple[int, str]:
        if "log" in cmd:
            return 0, f"{BOT_AUTHOR}\n{BOT_COMMITTER}\n{self.t_sha}\n"
        if "diff-tree" in cmd:
            return 0, "skills/portfolio-delivery-control/scripts/workflow.py\n"
        return 0, ""

    def _call_record_review(
        self,
        work_id: str,
        report_path: Path | str,
        report_sha256: str,
        family: str = "opus",
        model: str = "claude-opus-5-5",
        verdict: str = "SHIP",
        session_id: str = "sess-r1",
        reviewer_id: str = "reviewer-01",
    ) -> dict[str, Any]:
        self.snapshot_doc()
        return record_review(
            control_dir=self.state_dir,
            work_id=work_id,
            reviewer_id=reviewer_id,
            model=model,
            family=family,
            session_id=session_id,
            verdict=verdict,
            report_path=report_path,
            report_sha256=report_sha256,
        )

    def _setup_admitted_work(self, code: str) -> str:
        task_id = f"TASK_{code}"
        self.spec_file.write_text(self.spec_file.read_text(encoding="utf-8") + f"- [ ] {task_id}\n", encoding="utf-8")
        res = admit_work(
            control_dir=self.state_dir,
            repo="selamy-labs/agent-skills",
            issue_num="152",
            spec_path=self.spec_file,
            task_id=task_id,
        )
        self.snapshot_doc()
        return res["work_id"]

    def _setup_claimed_work(self, code: str) -> tuple[str, str]:
        work_id = self._setup_admitted_work(code)
        res = claim_work(
            control_dir=self.state_dir,
            work_id=work_id,
            owner="lane-01",
            session_id="sess-01",
            worktree=self.worktree,
        )
        self.snapshot_doc()
        return work_id, res["claim_token"]

    def _setup_candidate_work(self, code: str) -> str:
        work_id, _ = self._setup_claimed_work(code)
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        wi = doc["delivery_control"]["work_items"][work_id]
        wi["candidate"] = {
            "commit_sha": self.c_sha,
            "tree_sha": self.t_sha,
        }
        status_file.write_text(json.dumps(doc), encoding="utf-8")
        self.snapshot_doc()
        return work_id

    def _add_ledger_entry(self, disp_id: str, work_id: str, family: str, session_id: str) -> None:
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        ledger = doc.setdefault("delivery_control", {}).setdefault("dispatch_ledger", {})
        ledger[disp_id] = {
            "dispatch_id": disp_id,
            "work_id": work_id,
            "family": family,
            "reviewer_session_id": session_id,
            "session_id": session_id,
        }
        status_file.write_text(json.dumps(doc), encoding="utf-8")
        self.snapshot_doc()

    def _write_report(self, overrides: dict[str, Any]) -> tuple[Path, str]:
        data = {
            "schema": "review-receipt/v1",
            "dispatch_id": "disp-01",
            "reviewer_session_id": "sess-r1",
            "family": "opus",
            "model": "claude-opus-5-5",
            "commit_sha": self.c_sha,
            "tree_sha": self.t_sha,
            "verdict": "SHIP",
            "findings": [],
        }
        data.update(overrides)
        rep_file = Path(self.temp_dir) / f"rep_{abs(hash(json.dumps(data)))}.json"
        rep_file.write_text(json.dumps(data), encoding="utf-8")
        rep_sha = hashlib.sha256(rep_file.read_bytes()).hexdigest()
        return rep_file, rep_sha

    def _setup_valid_baseline(self, task_id: str = "TASK_01") -> str:
        work_id = f"selamy-labs-agent-skills-152-{task_id}"
        reviews_data = [
            ("opus", "claude-opus-5-5", "disp-01", "sess-r1"),
            ("argon", "gemini-4.0-argon-high", "disp-02", "sess-r2"),
            ("sol", "gpt-6.1-sol-high", "disp-03", "sess-r3"),
        ]
        rec_dir = self.state_dir / "receipts"
        rec_dir.mkdir(parents=True, exist_ok=True)
        rep_dir = self.state_dir / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)

        reviews_list: list[dict[str, Any]] = []
        for fam, model, disp_id, sess in reviews_data:
            rep_path = rep_dir / f"report_{work_id}_{fam}_{sess}.json"
            rep_content = json.dumps(
                {
                    "schema": "review-receipt/v1",
                    "dispatch_id": disp_id,
                    "reviewer_session_id": sess,
                    "family": fam,
                    "model": model,
                    "commit_sha": self.c_sha,
                    "tree_sha": self.t_sha,
                    "verdict": "SHIP",
                    "findings": [],
                }
            )
            rep_path.write_text(rep_content, encoding="utf-8")
            rep_sha = hashlib.sha256(rep_content.encode("utf-8")).hexdigest()
            rec_path = rec_dir / f"review_{work_id}_{fam}_{sess}.json"
            rec_path.write_text(
                json.dumps(
                    {
                        "schema": "review-receipt/v1",
                        "work_id": work_id,
                        "dispatch_id": disp_id,
                        "reviewer_session_id": sess,
                        "family": fam,
                        "model": model,
                        "commit_sha": self.c_sha,
                        "tree_sha": self.t_sha,
                        "verdict": "SHIP",
                        "findings": [],
                        "report_path": str(rep_path),
                        "report_sha256": rep_sha,
                    }
                ),
                encoding="utf-8",
            )
            reviews_list.append(
                {
                    "dispatch_id": disp_id,
                    "reviewer_session_id": sess,
                    "session_id": sess,
                    "family": fam,
                    "reviewer_family": fam,
                    "model": model,
                    "verdict": "SHIP",
                    "commit_sha": self.c_sha,
                    "tree_sha": self.t_sha,
                    "report_path": str(rep_path),
                    "report_sha256": rep_sha,
                }
            )

        doc: dict[str, Any] = {
            "delivery_control": {
                "projects": {
                    "agent-skills": {
                        "stage": "ready",
                        "ci": {
                            "status": "passing",
                            "revision": self.c_sha,
                            "evidence": [
                                {
                                    "name": "ci/test",
                                    "status": "COMPLETED",
                                    "conclusion": "SUCCESS",
                                    "head_sha": self.c_sha,
                                }
                            ],
                        },
                    }
                },
                "dispatch_ledger": {
                    disp_id: {
                        "dispatch_id": disp_id,
                        "work_id": work_id,
                        "family": fam,
                        "reviewer_session_id": sess,
                        "session_id": sess,
                    }
                    for fam, model, disp_id, sess in reviews_data
                },
                "work_items": {
                    work_id: {
                        "work_id": work_id,
                        "repo": "selamy-labs/agent-skills",
                        "issue_num": "152",
                        "task_id": task_id,
                        "spec_path": str(self.spec_file),
                        "approval_ref": str(self.approval_file),
                        "claim": {
                            "owner": "lane-01",
                            "session_id": "sess-01",
                            "worktree": str(self.worktree),
                            "claim_token": self.claim_token,
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                        "candidate": {
                            "commit_sha": self.c_sha,
                            "tree_sha": self.t_sha,
                            "gate_results": self.valid_gates,
                        },
                        "commit_sha": self.c_sha,
                        "tree_sha": self.t_sha,
                        "qualified": True,
                        "stage": "review",
                        "reviews": reviews_list,
                        "transitions": [
                            {
                                "action": "merge_pr",
                                "commit_sha": self.c_sha,
                                "tree_sha": self.t_sha,
                                "transitioned_at": "2026-11-03T10:00:00Z",
                            }
                        ],
                    }
                },
            }
        }
        status_file = self.state_dir / "coordinator-status.json"
        status_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return work_id

    def _assert_transition_no_side_effects(self, code: str, doc_before: dict[str, Any], status_file: Path) -> None:
        spec_content = self.spec_file.read_text(encoding="utf-8")
        self.assertIn("- [ ] TASK_01", spec_content, f"SPEC.md modified during {code}!")
        self.assertNotIn("- [x] TASK_01", spec_content, f"SPEC.md checked during {code}!")
        doc_after = json.loads(status_file.read_text(encoding="utf-8"))
        self.assertEqual(doc_before, doc_after, f"Status doc mutated during refusal of {code}!")

    def _assert_admission_no_side_effects(self, code: str, doc_before: Any) -> None:
        spec_content = self.spec_file.read_text(encoding="utf-8")
        self.assertNotIn("- [x]", spec_content, f"SPEC.md checked during refusal of {code}!")
        status_file = self.state_dir / "coordinator-status.json"
        doc_after = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else None
        self.assertEqual(doc_before, doc_after, f"Status doc mutated during refusal of {code}!")

    def _assert_receipt_no_side_effects(self, code: str, doc_before: Any) -> None:
        rec_dir = self.state_dir / "receipts"
        if rec_dir.exists():
            self.assertEqual(list(rec_dir.glob("*.json")), [], f"Receipt files created during refusal of {code}!")
        status_file = self.state_dir / "coordinator-status.json"
        doc_after = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else None
        self.assertEqual(doc_before, doc_after, f"Status doc mutated during refusal of {code}!")

    def test_bidirectional_sync_transition_refusal_codes(self) -> None:
        """Enforces that TRANSITION_REFUSAL_CODES matches the exact codes in transitions.py and providers.py."""
        self.assertEqual(
            set(TRANS_MODIFIERS.keys()),
            TRANSITION_REFUSAL_CODES,
            "TRANS_MODIFIERS does not exactly match TRANSITION_REFUSAL_CODES",
        )
        t_text = (SCRIPTS_DIR / "transitions.py").read_text(encoding="utf-8")
        p_text = (SCRIPTS_DIR / "providers.py").read_text(encoding="utf-8")
        extracted_codes = extract_refusal_codes_from_ast(t_text) | extract_refusal_codes_from_ast(p_text)

        missing_in_inventory = extracted_codes - TRANSITION_REFUSAL_CODES
        self.assertEqual(
            missing_in_inventory,
            set(),
            f"Refusal codes present in transitions/providers but missing from inventory: {missing_in_inventory}",
        )
        missing_in_code = TRANSITION_REFUSAL_CODES - extracted_codes
        self.assertEqual(
            missing_in_code,
            set(),
            f"Refusal codes present in inventory but missing from transitions/providers: {missing_in_code}",
        )

    def test_bidirectional_sync_admission_refusal_codes(self) -> None:
        """Enforces that ADMISSION_REFUSAL_CODES matches the exact codes in admission.py."""
        self.assertEqual(
            set(ADMISSION_DRIVERS.keys()),
            ADMISSION_REFUSAL_CODES,
            "ADMISSION_DRIVERS does not exactly match ADMISSION_REFUSAL_CODES",
        )
        a_text = (SCRIPTS_DIR / "admission.py").read_text(encoding="utf-8")
        extracted_codes = extract_refusal_codes_from_ast(a_text)

        missing_in_inventory = extracted_codes - ADMISSION_REFUSAL_CODES
        self.assertEqual(
            missing_in_inventory,
            set(),
            f"Refusal codes present in admission.py but missing from inventory: {missing_in_inventory}",
        )
        missing_in_code = ADMISSION_REFUSAL_CODES - extracted_codes
        self.assertEqual(
            missing_in_code,
            set(),
            f"Refusal codes present in inventory but missing from admission.py: {missing_in_code}",
        )

    def test_bidirectional_sync_receipt_refusal_codes(self) -> None:
        """Enforces that RECEIPT_REFUSAL_CODES matches the exact codes in receipts.py."""
        self.assertEqual(
            set(RECEIPT_DRIVERS.keys()),
            RECEIPT_REFUSAL_CODES,
            "RECEIPT_DRIVERS does not exactly match RECEIPT_REFUSAL_CODES",
        )
        r_text = (SCRIPTS_DIR / "receipts.py").read_text(encoding="utf-8")
        extracted_codes = extract_refusal_codes_from_ast(r_text)

        missing_in_inventory = extracted_codes - RECEIPT_REFUSAL_CODES
        self.assertEqual(
            missing_in_inventory,
            set(),
            f"Refusal codes present in receipts.py but missing from inventory: {missing_in_inventory}",
        )
        missing_in_code = RECEIPT_REFUSAL_CODES - extracted_codes
        self.assertEqual(
            missing_in_code,
            set(),
            f"Refusal codes present in inventory but missing from receipts.py: {missing_in_code}",
        )

    def test_table_driven_transition_refusals_and_no_side_effects(self) -> None:
        """Table-driven test driving real transitions to every refusal code and asserting no side effect."""
        for code in sorted(TRANSITION_REFUSAL_CODES):
            with self.subTest(refusal_code=code):
                work_id = self._setup_valid_baseline(task_id=f"TASK_{code}")
                status_file = self.state_dir / "coordinator-status.json"
                doc = json.loads(status_file.read_text(encoding="utf-8"))
                wi = doc["delivery_control"]["work_items"][work_id]

                ctx = {
                    "action": "complete_spec_task",
                    "claim_token": self.claim_token,
                    "merge_ident": "selamy-labs-bot",
                    "target_b": "feat/inventory",
                    "cmd_runner": None,
                    "work_id": work_id,
                    "wi": wi,
                    "doc": doc,
                    "save_doc": lambda: status_file.write_text(json.dumps(doc), encoding="utf-8"),
                    "temp_dir": self.temp_dir,
                    "state_dir": self.state_dir,
                    "approval_file": self.approval_file,
                    "c_sha": self.c_sha,
                }

                modifier = TRANS_MODIFIERS.get(code)
                if modifier:
                    modifier(ctx)

                doc_before = json.loads(status_file.read_text(encoding="utf-8"))
                res = execute_transition(
                    control_dir=str(self.state_dir),
                    work_id=ctx["work_id"],
                    action=ctx["action"],
                    claim_token=ctx["claim_token"],
                    branch=ctx["target_b"],
                    merge_identity=ctx["merge_ident"],
                    command_runner=ctx["cmd_runner"],
                )

                self.assertEqual(res.get("status"), "refused", f"Expected refused for {code}, got {res}")
                self.assertEqual(res.get("reason_code"), code, f"Expected reason_code {code}, got {res}")
                self._assert_transition_no_side_effects(code, doc_before, status_file)

    def test_table_driven_admission_refusals_and_no_side_effects(self) -> None:
        """Table-driven test driving real admission actions to every refusal code and asserting no side effect."""
        for code in sorted(ADMISSION_REFUSAL_CODES):
            with self.subTest(refusal_code=code):
                self.setUp()
                driver = ADMISSION_DRIVERS[code]
                res = driver(self, code)
                self.assertEqual(res.get("status"), "refused", f"Expected refused for {code}, got {res}")
                self.assertEqual(res.get("reason_code"), code, f"Expected {code}, got {res}")
                self._assert_admission_no_side_effects(code, self.doc_before)

    def test_table_driven_receipt_refusals_and_no_side_effects(self) -> None:
        """Table-driven test driving real receipt actions to every refusal code and asserting no side effect."""
        for code in sorted(RECEIPT_REFUSAL_CODES):
            with self.subTest(refusal_code=code):
                self.setUp()
                driver = RECEIPT_DRIVERS[code]
                res = driver(self, code)
                self.assertEqual(res.get("status"), "refused", f"Expected refused for {code}, got {res}")
                self.assertEqual(res.get("reason_code"), code, f"Expected {code}, got {res}")
                self._assert_receipt_no_side_effects(code, self.doc_before)

    def test_ratchet_catches_mutant_spellings(self) -> None:
        """Mutant tests proving the AST extractor catches non-canonical spellings."""
        mutant_src = """
def sample_paren():
    return (False, "MUTANT_PAREN_CODE")

def sample_var():
    err_var = "MUTANT_VAR_CODE"
    return False, err_var

def sample_dict():
    d_var = "MUTANT_DICT_CODE"
    return {"status": "refused", "reason_code": d_var}

def sample_call():
    c_var = "MUTANT_CALL_CODE"
    make_refusal(doc, wi, c_var)

def sample_ifexp():
    return False, "MUTANT_IFEXP_A" if True else "MUTANT_IFEXP_B"

def sample_kwarg_dict():
    return dict(status="refused", reason_code="MUTANT_KWARG_DICT")

def sample_dict_get():
    res = {}
    return res.get("reason_code", "MUTANT_GET_CODE")

def sample_fstring():
    s_suffix = "CODE"
    return False, f"MUTANT_FSTRING_{s_suffix}"
"""
        extracted = extract_refusal_codes_from_ast(mutant_src)
        expected_mutants = {
            "MUTANT_PAREN_CODE",
            "MUTANT_VAR_CODE",
            "MUTANT_DICT_CODE",
            "MUTANT_CALL_CODE",
            "MUTANT_IFEXP_A",
            "MUTANT_IFEXP_B",
            "MUTANT_KWARG_DICT",
            "MUTANT_GET_CODE",
            "MUTANT_FSTRING_CODE",
        }
        self.assertEqual(extracted, expected_mutants)
        self.assertTrue(bool(expected_mutants - TRANSITION_REFUSAL_CODES))

    def test_ratchet_fails_when_driver_or_code_dropped(self) -> None:
        """Proves that dropping an inventory code or a driver causes immediate failure."""
        fake_drivers = dict(TRANS_MODIFIERS)
        dropped_code = next(iter(fake_drivers.keys()))
        del fake_drivers[dropped_code]
        with self.assertRaises(AssertionError):
            self.assertEqual(set(fake_drivers.keys()), TRANSITION_REFUSAL_CODES)


if __name__ == "__main__":
    unittest.main()
