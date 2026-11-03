#!/usr/bin/env python3
"""Tests for admission and claim logic in portfolio delivery control."""

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
    MANDATORY_GATE_NAMES,
    admit_work,
    claim_work,
    invalidate_prior_reviews_if_revision_changed,
    paths_overlap,
    record_implementation,
    validate_worktree,
    verify_mandatory_gates,
)


class TestAdmission(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="pdc-test-admission-")
        self.state_dir = Path(self.temp_dir) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.worktree = Path(self.temp_dir) / "worktree"
        self.worktree.mkdir(parents=True, exist_ok=True)
        (self.worktree / ".git").mkdir(parents=True, exist_ok=True)

        self.spec_file = self.worktree / "SPEC.md"
        self.spec_file.write_text("- [ ] TASK_01: Implement feature\n- [x] TASK_02: Already completed\n")

        self.c_sha = "1" * 40
        self.t_sha = "2" * 40

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_admit_work_success_and_duplicates(self) -> None:
        res = admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        self.assertEqual(res.get("status"), "admitted")
        self.assertEqual(res.get("work_id"), "selamy-labs-agent-skills-152-TASK_01")

        # Duplicate admission refused
        res2 = admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        self.assertEqual(res2.get("status"), "refused")
        self.assertEqual(res2.get("reason_code"), "DUPLICATE_WORK_ADMISSION")

    def test_admit_work_already_checked_or_missing(self) -> None:
        # Already completed in spec
        res = admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_02",
            allowed_roots=[str(self.temp_dir)],
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "TASK_ALREADY_COMPLETED")

        # Missing task
        res_miss = admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_NONEXISTENT",
            allowed_roots=[str(self.temp_dir)],
        )
        self.assertEqual(res_miss.get("status"), "refused")
        self.assertEqual(res_miss.get("reason_code"), "TASK_NOT_FOUND_IN_SPEC")

        # Missing spec file
        res_nospec = admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path="/tmp/nonexistent-spec-file.md",
            task_id="TASK_01",
            allowed_roots=["/tmp"],
        )
        self.assertEqual(res_nospec.get("status"), "refused")
        self.assertEqual(res_nospec.get("reason_code"), "SPEC_FILE_NOT_FOUND")

    def test_claim_work_success_and_conflicts(self) -> None:
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )

        res = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="lane-test",
            session_id="session-writer-01",
            worktree=str(self.worktree),
            ttl_seconds=3600,
        )
        self.assertEqual(res.get("status"), "claimed")
        claim_token = res.get("claim_token")
        self.assertTrue(claim_token)

        # Refuse second claim with overlapping worktree
        res_overlap = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="lane-test-2",
            session_id="session-writer-02",
            worktree=str(self.worktree),
            ttl_seconds=3600,
        )
        self.assertEqual(res_overlap.get("status"), "refused")
        self.assertEqual(res_overlap.get("reason_code"), "WORKTREE_PATH_CONFLICT")

        # Refuse second claim with non-overlapping worktree (already claimed)
        worktree_other = Path(self.temp_dir) / "worktree-other"
        worktree_other.mkdir(parents=True, exist_ok=True)
        (worktree_other / ".git").mkdir(parents=True, exist_ok=True)
        res_claimed = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="lane-test-2",
            session_id="session-writer-02",
            worktree=str(worktree_other),
            ttl_seconds=3600,
        )
        self.assertEqual(res_claimed.get("status"), "refused")
        self.assertEqual(res_claimed.get("reason_code"), "WORK_ITEM_ALREADY_CLAIMED")

    def test_claim_work_session_reuse_across_writers(self) -> None:
        self.spec_file.write_text("- [ ] TASK_01: First\n- [ ] TASK_02: Second\n")
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_02",
            allowed_roots=[str(self.temp_dir)],
        )

        worktree_2 = Path(self.temp_dir) / "worktree2"
        worktree_2.mkdir(parents=True, exist_ok=True)
        (worktree_2 / ".git").mkdir(parents=True, exist_ok=True)

        res1 = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="lane-01",
            session_id="session-shared-writer",
            worktree=str(self.worktree),
            ttl_seconds=3600,
        )
        self.assertEqual(res1.get("status"), "claimed")

        # Same session trying to claim different task concurrently
        res2 = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_02",
            owner="lane-02",
            session_id="session-shared-writer",
            worktree=str(worktree_2),
            ttl_seconds=3600,
        )
        self.assertEqual(res2.get("status"), "refused")
        self.assertEqual(res2.get("reason_code"), "SESSION_REUSED_ACROSS_WRITERS")

    def test_record_implementation_gates_and_verification(self) -> None:
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        c_res = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="lane-01",
            session_id="session-writer",
            worktree=str(self.worktree),
            ttl_seconds=3600,
        )
        claim_token = c_res.get("claim_token")

        def mock_runner(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
            if "diff-tree" in cmd:
                return 0, "skills/portfolio-delivery-control/scripts/workflow.py\n"
            if "log" in cmd:
                author_log = (
                    "claude-cir <claude-cir@selamy.dev>\n"
                    "selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"{self.t_sha}\nparent\n"
                )
                return 0, author_log
            return 0, ""

        valid_gates = {
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

        # Success case
        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            gate_results=valid_gates,
            modified_paths=["skills/portfolio-delivery-control/scripts/workflow.py"],
            runner=mock_runner,
        )
        self.assertEqual(res.get("status"), "recorded")
        self.assertEqual(res.get("stage"), "review")

        # Refusal when gate failed
        failing_gates = dict(valid_gates)
        failing_gates["pytest"] = {"exit_code": 1, "passed": False}
        res_fail = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            gate_results=failing_gates,
            modified_paths=["skills/portfolio-delivery-control/scripts/workflow.py"],
            runner=mock_runner,
        )
        self.assertEqual(res_fail.get("status"), "refused")
        self.assertEqual(res_fail.get("reason_code"), "MANDATORY_GATES_FAILED")

    def test_record_implementation_disallowed_author(self) -> None:
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        c_res = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="lane-01",
            session_id="session-writer",
            worktree=str(self.worktree),
            ttl_seconds=3600,
        )
        claim_token = c_res.get("claim_token")

        def runner_unapproved_author(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
            if "log" in cmd:
                return 0, f"evil-hacker <evil@hack.com>\nevil-committer <evil@hack.com>\n{self.t_sha}\nparent\n"
            return 0, ""

        valid_gates = {
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
        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            gate_results=valid_gates,
            modified_paths=["skills/portfolio-delivery-control/scripts/workflow.py"],
            runner=runner_unapproved_author,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "DISALLOWED_AUTHOR_IDENTITY")

    def test_paths_overlap_and_validate_worktree(self) -> None:
        self.assertTrue(paths_overlap("/a/b", "/a/b/c"))
        self.assertTrue(paths_overlap("/a/b/c", "/a/b"))
        self.assertFalse(paths_overlap("/a/b", "/a/c"))

        # Nonexistent path
        ok0, err0, _ = validate_worktree("/tmp/nonexistent_path_xyz", [str(self.temp_dir)])
        self.assertFalse(ok0)
        self.assertEqual(err0, "WORKTREE_NOT_FOUND")

        # Outside allowed roots
        outside_dir = tempfile.mkdtemp(prefix="outside-")
        try:
            ok1, err1, _ = validate_worktree(outside_dir, [str(self.temp_dir)])
            self.assertFalse(ok1)
            self.assertEqual(err1, "WORKTREE_SCOPE_ESCAPE")
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

        # Not a git directory
        d = Path(self.temp_dir) / "no_git"
        d.mkdir()
        ok3, err3, _ = validate_worktree(str(d), [str(self.temp_dir)])
        self.assertFalse(ok3)
        self.assertEqual(err3, "NOT_A_GIT_WORKTREE")

    def test_mandatory_gates_and_revision_invalidation(self) -> None:
        # Invalid gate dictionary
        self.assertFalse(verify_mandatory_gates(None))
        self.assertFalse(verify_mandatory_gates({"pytest": "FAIL"}))

    def test_mandatory_gates_sol_r17_004_booleans_and_strict_types(self) -> None:
        # All mandatory gates set to boolean False
        all_false = {g: False for g in MANDATORY_GATE_NAMES}
        all_false["coverage"] = 95.0
        self.assertFalse(verify_mandatory_gates(all_false))

        # Single mandatory gate set to boolean False
        one_false = {g: {"exit_code": 0, "passed": True} for g in MANDATORY_GATE_NAMES}
        one_false["pytest"] = False
        one_false["coverage"] = 95.0
        self.assertFalse(verify_mandatory_gates(one_false))

        # Boolean False as exit_code (False == 0 in Python)
        false_ec = {g: {"exit_code": 0, "passed": True} for g in MANDATORY_GATE_NAMES}
        false_ec["pytest"] = {"exit_code": False, "passed": True}
        false_ec["coverage"] = 95.0
        self.assertFalse(verify_mandatory_gates(false_ec))

        # passed is explicitly False
        passed_false = {g: {"exit_code": 0, "passed": True} for g in MANDATORY_GATE_NAMES}
        passed_false["ruff_check"] = {"exit_code": 0, "passed": False}
        passed_false["coverage"] = 95.0
        self.assertFalse(verify_mandatory_gates(passed_false))

        # empty dict or None exit code
        empty_gate = {g: {"exit_code": 0, "passed": True} for g in MANDATORY_GATE_NAMES}
        empty_gate["vulture"] = {}
        empty_gate["coverage"] = 95.0
        self.assertFalse(verify_mandatory_gates(empty_gate))

        # Coverage as boolean
        bool_cov = {g: {"exit_code": 0, "passed": True} for g in MANDATORY_GATE_NAMES}
        bool_cov["coverage"] = True
        self.assertFalse(verify_mandatory_gates(bool_cov))

        # Valid gates with mixed representations
        valid_mixed = {
            "pytest": 0,
            "complexipy": True,
            "vulture": "PASSED",
            "ruff_check": {"exit_code": 0, "passed": True},
            "ruff_format": {"exit_code": 0},
            "privacy_scan": {"passed": True},
            "security_scan": {"status": "PASS"},
            "lint_skills": 0,
            "coverage": 92.5,
        }
        self.assertTrue(verify_mandatory_gates(valid_mixed))

        # Revision invalidation
        wi = {
            "commit_sha": self.c_sha,
            "tree_sha": self.t_sha,
            "reviews": [{"some": "review"}],
            "qualified": True,
        }
        invalidate_prior_reviews_if_revision_changed(wi, "3" * 40, "4" * 40)
        self.assertEqual(wi["reviews"], [])
        self.assertFalse(wi["qualified"])

        # Unadmitted work item in claim and record_implementation
        res_c = claim_work(
            control_dir=str(self.state_dir),
            work_id="nonexistent-work-id",
            owner="lane",
            session_id="sess",
            worktree=str(self.worktree),
        )
        self.assertEqual(res_c.get("reason_code"), "WORK_NOT_ADMITTED")

        res_i = record_implementation(
            control_dir=str(self.state_dir),
            work_id="nonexistent-work-id",
            claim_token="tok",
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
        )
        self.assertEqual(res_i.get("reason_code"), "WORK_NOT_ADMITTED")

    def test_record_implementation_artifact_verification(self) -> None:
        # Set up admitted and claimed work item
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        c_res = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="selamy-labs-bot",
            session_id="sess-impl",
            worktree=str(self.worktree),
        )
        claim_token = c_res["claim_token"]

        # Missing path when hash provided
        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            patch_path=None,
            patch_sha256="abc" * 10,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "ARTIFACT_NOT_FOUND")

        # Nonexistent file path
        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            patch_path="/tmp/nonexistent-patch-file.patch",
            patch_sha256="abc" * 10,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "ARTIFACT_NOT_FOUND")

        # File exists but hash mismatch
        dummy_patch = Path(self.temp_dir) / "test.patch"
        dummy_patch.write_text("patch content\n")
        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            patch_path=dummy_patch,
            patch_sha256="wronghash" + "0" * 55,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "ARTIFACT_HASH_MISMATCH")

    def test_record_implementation_git_and_path_guards(self) -> None:
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        c_res = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="selamy-labs-bot",
            session_id="sess-impl",
            worktree=str(self.worktree),
        )
        claim_token = c_res["claim_token"]

        # Invalid revision SHA format
        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha="not-a-sha",
            tree_sha=self.t_sha,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "INVALID_REVISION_SHA")

        # Tree SHA mismatch
        def mock_runner_tree_mismatch(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "log" in " ".join(cmd):
                return (
                    0,
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"{'3' * 40}\n",
                )
            return 0, ""

        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            runner=mock_runner_tree_mismatch,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "TREE_SHA_MISMATCH")

        # Empty modified paths (both argument and git diff-tree empty)
        def mock_runner_empty_diff(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "log" in " ".join(cmd):
                return (
                    0,
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"{self.t_sha}\n",
                )
            if "diff-tree" in " ".join(cmd):
                return 0, ""
            return 0, ""

        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            modified_paths=[],
            runner=mock_runner_empty_diff,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "EMPTY_MODIFIED_PATHS")

        # Path scope escape (e.g., ../outside or /etc/passwd)
        def mock_runner_good_git(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "diff-tree" in " ".join(cmd):
                return 0, "../outside/file.py\n"
            if "log" in " ".join(cmd):
                return (
                    0,
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"{self.t_sha}\n",
                )
            return 0, ""

        res = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            modified_paths=["../outside/file.py"],
            runner=mock_runner_good_git,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "PATH_SCOPE_ESCAPE")

    def test_claim_conflict_and_corrupt_status_handling(self) -> None:
        # Corrupt status file
        status_file = self.state_dir / "coordinator-status.json"
        status_file.write_text("{bad json", encoding="utf-8")

        res_c = claim_work(
            control_dir=str(self.state_dir),
            work_id="some-work-id",
            owner="lane",
            session_id="sess",
            worktree=str(self.worktree),
        )
        self.assertEqual(res_c.get("reason_code"), "WORK_NOT_ADMITTED")

        res_i = record_implementation(
            control_dir=str(self.state_dir),
            work_id="some-work-id",
            claim_token="tok",
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
        )
        self.assertEqual(res_i.get("reason_code"), "WORK_NOT_ADMITTED")

    def test_record_implementation_claim_expired_and_disallowed_committer(self) -> None:
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        c_res = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="selamy-labs-bot",
            session_id="sess-impl",
            worktree=str(self.worktree),
            now="2026-11-03T10:00:00Z",
        )
        claim_token = c_res["claim_token"]

        # Expired claim
        res_exp = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            now="2026-11-03T12:00:00Z",  # 2 hours later, TTL was 1800s (30m)
        )
        self.assertEqual(res_exp.get("status"), "refused")
        self.assertEqual(res_exp.get("reason_code"), "CLAIM_EXPIRED")

        # Invalid claim token
        res_tok = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token="wrong-token",
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            now="2026-11-03T10:05:00Z",
        )
        self.assertEqual(res_tok.get("status"), "refused")
        self.assertEqual(res_tok.get("reason_code"), "INVALID_CLAIM_TOKEN")

        # Disallowed committer identity
        def mock_runner_bad_committer(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "log" in " ".join(cmd):
                return (
                    0,
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"unapproved-committer <bad@example.com>\n"
                    f"{self.t_sha}\n",
                )
            return 0, ""

        res_com = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            now="2026-11-03T10:05:00Z",
            runner=mock_runner_bad_committer,
        )
        self.assertEqual(res_com.get("status"), "refused")
        self.assertEqual(res_com.get("reason_code"), "DISALLOWED_COMMITTER_IDENTITY")

        # Mandatory gates failed
        def mock_runner_good(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "diff-tree" in " ".join(cmd):
                return 0, "skills/portfolio-delivery-control/scripts/workflow.py\n"
            if "log" in " ".join(cmd):
                return (
                    0,
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>\n"
                    f"{self.t_sha}\n",
                )
            return 0, ""

        res_gate = record_implementation(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            claim_token=claim_token,
            commit_sha=self.c_sha,
            tree_sha=self.t_sha,
            modified_paths=["skills/portfolio-delivery-control/scripts/workflow.py"],
            gate_results={"pytest": 1, "coverage": 50.0},
            now="2026-11-03T10:05:00Z",
            runner=mock_runner_good,
        )
        self.assertEqual(res_gate.get("status"), "refused")
        self.assertEqual(res_gate.get("reason_code"), "MANDATORY_GATES_FAILED")

    def test_admission_edge_cases_and_helpers(self) -> None:
        import datetime

        from admission import _is_claim_active, _is_coverage_passing, _is_gate_passing, _is_path_allowed

        # _is_path_allowed
        self.assertFalse(_is_path_allowed("/absolute/path", ["skills"]))
        self.assertFalse(_is_path_allowed("../relative/path", ["skills"]))

        # _is_gate_passing
        self.assertTrue(_is_gate_passing({"exit_code": 0, "passed": True}))
        self.assertFalse(_is_gate_passing({"exit_code": 1}))
        self.assertFalse(_is_gate_passing({"exit_code": 0, "passed": False}))

        # _is_coverage_passing
        self.assertTrue(_is_coverage_passing({"percent": 95.0}))
        self.assertFalse(_is_coverage_passing("not-a-number"))

        # _is_claim_active
        now_dt = datetime.datetime.now(datetime.UTC)
        self.assertFalse(_is_claim_active({}, now_dt))
        self.assertFalse(_is_claim_active({"expires_at": "invalid-date"}, now_dt))

        # Idempotent re-claim by same owner & session
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num=152,
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        c1 = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="selamy-labs-bot",
            session_id="sess-impl",
            worktree=str(self.worktree),
        )
        self.assertEqual(c1.get("status"), "claimed")
        c2 = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="selamy-labs-bot",
            session_id="sess-impl",
            worktree=str(self.worktree),
        )
        self.assertEqual(c2.get("status"), "claimed")

        # Invalid worktree path raising OSError
        ok_wt, err_wt, _ = validate_worktree("\0badpath")
        self.assertFalse(ok_wt)
        self.assertEqual(err_wt, "INVALID_WORKTREE_PATH")


if __name__ == "__main__":
    unittest.main()
