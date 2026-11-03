#!/usr/bin/env python3
"""Tests for admission and claim logic in portfolio delivery control."""

import shutil
import tempfile
import unittest
from pathlib import Path

from admission import (
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
            if "log" in cmd:
                author_log = (
                    f"claude-cir <claude-cir@selamy.dev>\ncommitter <committer@selamy.dev>\n{self.t_sha}\nparent\n"
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


if __name__ == "__main__":
    unittest.main()
