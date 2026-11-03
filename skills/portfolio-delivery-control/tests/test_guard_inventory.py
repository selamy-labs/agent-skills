"""Guard inventory and bidirectional sync verification.

Guarantees:
1. Every refusal reason code emitted by transitions.py, admission.py, and receipts.py
   is enumerated in the authoritative inventory.
2. Every code in the inventory is tested via a table-driven execution path that asserts
   status="refused", the exact reason_code, and verifiable zero side-effects.
3. Any added or removed refusal code without updating the inventory causes a test failure.
"""

from __future__ import annotations

import hashlib
import json
import re
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
    "INVALID_ACTION",
    "PROTECTED_BRANCH_PUSH",
    "DISALLOWED_MERGE_IDENTITY",
    "WORK_NOT_ADMITTED",
    "ABSENT_APPROVAL",
    "APPROVAL_FILE_NOT_FOUND",
    "APPROVAL_READ_FAILED",
    "APPROVAL_WITHDRAWN",
    "APPROVAL_DIGEST_MISMATCH",
    "UNRESOLVED_REVISE_FINDINGS",
    "MISSING_OR_EXPIRED_CLAIM",
    "STALE_CLAIM",
    "CLAIM_EXPIRED",
    "MISSING_QUALIFICATION",
    "STALE_REVIEW_REVISION",
    "MISSING_MANDATORY_GATE",
    "MISSING_TRANSITION_EVIDENCE",
    "CI_CHECK_FAILED",
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
    ctx["cmd_runner"] = lambda cmd, cwd=None: (
        0,
        json.dumps(
            {
                "headRefOid": c_sha,
                "statusCheckRollup": [
                    {"name": "ci/test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": c_sha}
                ],
            }
        ),
    )


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
    return record_dispatch(tc.state_dir, "work_id", "disp-dup", family="sol", reviewer_session_id="sess-02")


def _rec_duplicate_reviewer_session(tc: Any, code: str) -> dict[str, Any]:
    record_dispatch(tc.state_dir, "work_id", "disp-01", family="opus", reviewer_session_id="sess-dup")
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
                        "reviews": [
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
                            }
                            for fam, model, disp_id, sess in reviews_data
                        ],
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

        rec_dir = self.state_dir / "receipts"
        rec_dir.mkdir(parents=True, exist_ok=True)
        rep_dir = self.state_dir / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
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
        return work_id

    def _assert_transition_no_side_effects(self, code: str, work_id: str, status_file: Path) -> None:
        spec_content = self.spec_file.read_text(encoding="utf-8")
        self.assertIn("- [ ] TASK_01", spec_content, f"SPEC.md modified during {code}!")
        self.assertNotIn("- [x] TASK_01", spec_content, f"SPEC.md checked during {code}!")
        doc_after = json.loads(status_file.read_text(encoding="utf-8"))
        if work_id in doc_after["delivery_control"]["work_items"]:
            wi_after = doc_after["delivery_control"]["work_items"][work_id]
            self.assertNotEqual(wi_after.get("stage"), "done", f"Stage advanced during {code}!")
            orig_trans_count = 1 if code != "MISSING_TRANSITION_EVIDENCE" else 0
            self.assertEqual(len(wi_after.get("transitions", [])), orig_trans_count, f"Transition recorded in {code}!")

    def _assert_admission_no_side_effects(self, code: str) -> None:
        spec_content = self.spec_file.read_text(encoding="utf-8")
        self.assertNotIn("- [x]", spec_content, f"SPEC.md checked during refusal of {code}!")
        status_file = self.state_dir / "coordinator-status.json"
        if status_file.exists():
            doc = json.loads(status_file.read_text(encoding="utf-8"))
            for wi in doc.get("delivery_control", {}).get("work_items", {}).values():
                self.assertNotIn(wi.get("stage"), {"review", "merge_ready", "done"}, f"Stage advanced in {code}!")

    def _assert_receipt_no_side_effects(self, code: str) -> None:
        rec_dir = self.state_dir / "receipts"
        if rec_dir.exists():
            self.assertEqual(list(rec_dir.glob("*.json")), [], f"Receipt files created during refusal of {code}!")
        status_file = self.state_dir / "coordinator-status.json"
        if status_file.exists():
            doc = json.loads(status_file.read_text(encoding="utf-8"))
            for wi in doc.get("delivery_control", {}).get("work_items", {}).values():
                self.assertNotIn(wi.get("stage"), {"merge_ready", "done"}, f"Stage advanced in {code}!")

    def test_bidirectional_sync_transition_refusal_codes(self) -> None:
        """Enforces that TRANSITION_REFUSAL_CODES matches the exact codes in transitions.py."""
        transitions_file = SCRIPTS_DIR / "transitions.py"
        content = transitions_file.read_text(encoding="utf-8")
        extracted_codes: set[str] = set()

        for match in re.finditer(r'["\']reason_code["\']\s*:\s*["\']([A-Z0-9_]+)["\']', content):
            code = match.group(1)
            if code not in {"PROVIDER_EXECUTION_FAILED"}:
                extracted_codes.add(code)

        for match in re.finditer(r'return\s+False,\s*["\']([A-Z0-9_]+)["\']', content):
            extracted_codes.add(match.group(1))

        for match in re.finditer(r'make_refusal\([^,]+,\s*[^,]+,\s*["\']([A-Z0-9_]+)["\']', content):
            extracted_codes.add(match.group(1))

        missing_in_inventory = extracted_codes - TRANSITION_REFUSAL_CODES
        self.assertEqual(
            missing_in_inventory,
            set(),
            f"Refusal codes present in transitions.py but missing from inventory: {missing_in_inventory}",
        )

        missing_in_code = TRANSITION_REFUSAL_CODES - extracted_codes
        self.assertEqual(
            missing_in_code,
            set(),
            f"Refusal codes present in inventory but missing from transitions.py: {missing_in_code}",
        )

    def test_bidirectional_sync_admission_refusal_codes(self) -> None:
        """Enforces that ADMISSION_REFUSAL_CODES matches the exact codes in admission.py."""
        admission_file = SCRIPTS_DIR / "admission.py"
        content = admission_file.read_text(encoding="utf-8")
        extracted_codes: set[str] = set()

        for match in re.finditer(r'["\']reason_code["\']\s*:\s*["\']([A-Z0-9_]+)["\']', content):
            extracted_codes.add(match.group(1))

        for match in re.finditer(r'return\s+False,\s*["\']([A-Z0-9_]+)["\']', content):
            extracted_codes.add(match.group(1))

        for match in re.finditer(r'make_refusal\([^,]+,\s*[^,]+,\s*["\']([A-Z0-9_]+)["\']', content):
            extracted_codes.add(match.group(1))

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
        receipts_file = SCRIPTS_DIR / "receipts.py"
        content = receipts_file.read_text(encoding="utf-8")
        extracted_codes: set[str] = set()

        for match in re.finditer(r'["\']reason_code["\']\s*:\s*["\']([A-Z0-9_]+)["\']', content):
            extracted_codes.add(match.group(1))

        for match in re.finditer(r'return\s+False,\s*["\']([A-Z0-9_]+)["\']', content):
            extracted_codes.add(match.group(1))

        for match in re.finditer(r'make_refusal\([^,]+,\s*[^,]+,\s*["\']([A-Z0-9_]+)["\']', content):
            extracted_codes.add(match.group(1))

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
                self._assert_transition_no_side_effects(code, ctx["work_id"], status_file)

    def test_table_driven_admission_refusals_and_no_side_effects(self) -> None:
        """Table-driven test driving real admission actions to every refusal code and asserting no side effect."""
        for code in sorted(ADMISSION_DRIVERS):
            with self.subTest(refusal_code=code):
                self.setUp()
                driver = ADMISSION_DRIVERS[code]
                res = driver(self, code)
                self.assertEqual(res.get("status"), "refused", f"Expected refused for {code}, got {res}")
                self.assertEqual(res.get("reason_code"), code, f"Expected {code}, got {res}")
                self._assert_admission_no_side_effects(code)

    def test_table_driven_receipt_refusals_and_no_side_effects(self) -> None:
        """Table-driven test driving real receipt actions to every refusal code and asserting no side effect."""
        for code in sorted(RECEIPT_DRIVERS):
            with self.subTest(refusal_code=code):
                self.setUp()
                driver = RECEIPT_DRIVERS[code]
                res = driver(self, code)
                self.assertEqual(res.get("status"), "refused", f"Expected refused for {code}, got {res}")
                self.assertEqual(res.get("reason_code"), code, f"Expected {code}, got {res}")
                self._assert_receipt_no_side_effects(code)


if __name__ == "__main__":
    unittest.main()
