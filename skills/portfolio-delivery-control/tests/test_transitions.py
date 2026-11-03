#!/usr/bin/env python3
"""Tests for state transitions and stage guards in portfolio delivery control."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from transitions import execute_transition


class TestTransitions(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="pdc-test-transitions-")
        self.state_dir = Path(self.temp_dir) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.worktree = Path(self.temp_dir) / "worktree"
        self.worktree.mkdir(parents=True, exist_ok=True)
        (self.worktree / ".git").mkdir(parents=True, exist_ok=True)

        self.spec_file = self.worktree / "SPEC.md"
        self.spec_file.write_text("- [ ] MP464: Reproduce CI regression and unbound reviews\n- [ ] TASK_02: Task 2\n")

        self.approval_file = Path(self.temp_dir) / "approval.txt"
        self.approval_file.write_text("APPROVED by coordinator\n", encoding="utf-8")

        self.c_sha = "1" * 40
        self.t_sha = "2" * 40
        self.claim_token = "claim-test-token-12345"

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

    def _setup_coordinator_state(
        self,
        task_id: str = "MP464",
        qualified: bool = True,
        project_ci_status: str | None = "passing",
        with_merge_transition: bool = False,
    ) -> None:
        work_id = f"selamy-labs-agent-skills-152-{task_id}"
        doc: dict[str, Any] = {
            "delivery_control": {
                "projects": {
                    "agent-skills": {
                        "stage": "ready",
                    }
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
                        "qualified": qualified,
                        "stage": "review",
                        "reviews": (
                            [
                                {
                                    "dispatch_id": "disp-01",
                                    "reviewer_session_id": "sess-r1",
                                    "family": "opus",
                                    "model": "claude-opus-4-6",
                                    "verdict": "SHIP",
                                    "commit_sha": self.c_sha,
                                    "tree_sha": self.t_sha,
                                },
                                {
                                    "dispatch_id": "disp-02",
                                    "reviewer_session_id": "sess-r2",
                                    "family": "gemini",
                                    "model": "gemini-3.8-pro",
                                    "verdict": "SHIP",
                                    "commit_sha": self.c_sha,
                                    "tree_sha": self.t_sha,
                                },
                                {
                                    "dispatch_id": "disp-03",
                                    "reviewer_session_id": "sess-r3",
                                    "family": "codex",
                                    "model": "gpt-5.3-codex",
                                    "verdict": "SHIP",
                                    "commit_sha": self.c_sha,
                                    "tree_sha": self.t_sha,
                                },
                            ]
                            if qualified
                            else []
                        ),
                        "transitions": (
                            [
                                {
                                    "action": "merge_pr",
                                    "commit_sha": self.c_sha,
                                    "tree_sha": self.t_sha,
                                    "transitioned_at": "2026-11-03T10:00:00Z",
                                }
                            ]
                            if with_merge_transition
                            else []
                        ),
                    }
                },
            }
        }
        if project_ci_status:
            doc["delivery_control"]["projects"]["agent-skills"]["ci"] = {
                "status": project_ci_status,
                "revision": self.c_sha,
                "evidence": [
                    {
                        "name": "ci/test",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS" if project_ci_status == "passing" else "FAILURE",
                        "head_sha": self.c_sha,
                    }
                ],
            }
        status_file = self.state_dir / "coordinator-status.json"
        status_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        if qualified:
            rec_dir = self.state_dir / "receipts"
            rec_dir.mkdir(parents=True, exist_ok=True)
            for fam, sess in [("opus", "sess-r1"), ("gemini", "sess-r2"), ("codex", "sess-r3")]:
                rec_path = rec_dir / f"review_{work_id}_{fam}_{sess}.json"
                rec_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "review-receipt/v1",
                            "work_id": work_id,
                            "dispatch_id": f"disp-{sess}",
                            "reviewer_session_id": sess,
                            "family": fam,
                            "model": "model-test",
                            "commit_sha": self.c_sha,
                            "tree_sha": self.t_sha,
                            "verdict": "SHIP",
                            "findings": [],
                        }
                    ),
                    encoding="utf-8",
                )

    def test_mp464_regression_red_ci_plus_unbound_reviews_refused_no_effect(self) -> None:
        """Regression test reproducing MP464 (ARGON-03):

        Red exact-head CI plus unbound reviews must be refused BEFORE any effect on SPEC.md.
        Caller-supplied synthetic passing ci_checks must NEVER override failing CI.
        """
        self._setup_coordinator_state(
            task_id="MP464",
            qualified=False,  # Unbound / unqualified reviews
            project_ci_status="failing",  # Red exact-head CI
            with_merge_transition=True,
        )

        synthetic_passing_checks = [
            {"name": "fake/ci", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
        ]

        def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            # Live provider returns failing checks
            if "statusCheckRollup" in " ".join(cmd):
                return 0, json.dumps(
                    [
                        {
                            "name": "ci/build",
                            "status": "COMPLETED",
                            "conclusion": "FAILURE",
                            "head_sha": self.c_sha,
                        }
                    ]
                )
            return 0, ""

        # Execute complete_spec_task
        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            ci_checks=synthetic_passing_checks,
            command_runner=runner,
        )

        # 1. Refusal before any effect
        self.assertEqual(res.get("status"), "refused")
        self.assertIn(res.get("reason_code"), ["MISSING_QUALIFICATION", "CI_CHECK_FAILED"])

        # 2. Assert zero side-effects on SPEC.md (checkbox is still unchecked)
        spec_content = self.spec_file.read_text(encoding="utf-8")
        self.assertIn("- [ ] MP464: Reproduce CI regression and unbound reviews", spec_content)
        self.assertNotIn("- [x] MP464", spec_content)

    def test_complete_spec_task_refuses_failing_project_ci_despite_caller_checks(self) -> None:
        """Caller passing ci_checks cannot override failing project row CI."""
        self._setup_coordinator_state(
            task_id="MP464",
            qualified=True,
            project_ci_status="failing",
            with_merge_transition=True,
        )

        synthetic_passing_checks = [
            {"name": "fake/ci", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
        ]

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            ci_checks=synthetic_passing_checks,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "CI_CHECK_FAILED")

        # Spec file unchanged
        self.assertIn("- [ ] MP464", self.spec_file.read_text(encoding="utf-8"))

    def test_complete_spec_task_refuses_failing_live_provider_ci_despite_caller_checks(self) -> None:
        """Caller passing ci_checks cannot override failing live provider checks."""
        self._setup_coordinator_state(
            task_id="MP464",
            qualified=True,
            project_ci_status=None,  # No project row, rely on live provider
            with_merge_transition=True,
        )

        def live_failing_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "statusCheckRollup" in " ".join(cmd):
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        synthetic_passing_checks = [
            {"name": "fake/ci", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
        ]

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            ci_checks=synthetic_passing_checks,
            command_runner=live_failing_runner,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "CI_CHECK_FAILED")
        self.assertIn("- [ ] MP464", self.spec_file.read_text(encoding="utf-8"))

    def test_merge_pr_refuses_disallowed_identity_or_protected_branch_push(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")

        # Disallowed merge identity
        res_id = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="merge_pr",
            claim_token=self.claim_token,
            merge_identity="unauthorized-user",
        )
        self.assertEqual(res_id.get("status"), "refused")
        self.assertEqual(res_id.get("reason_code"), "DISALLOWED_MERGE_IDENTITY")

        # Protected branch push
        res_push = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="push",
            claim_token=self.claim_token,
            branch="main",
            base_branch="main",
        )
        self.assertEqual(res_push.get("status"), "refused")
        self.assertEqual(res_push.get("reason_code"), "PROTECTED_BRANCH_PUSH")

    def test_complete_spec_task_success_when_qualified_and_ci_green(self) -> None:
        self._setup_coordinator_state(
            task_id="MP464",
            qualified=True,
            project_ci_status="passing",
            with_merge_transition=True,
        )

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
        )
        self.assertEqual(res.get("status"), "transitioned")
        self.assertEqual(res.get("action"), "complete_spec_task")

        # Spec file checked!
        spec_content = self.spec_file.read_text(encoding="utf-8")
        self.assertIn("- [x] MP464: Reproduce CI regression and unbound reviews", spec_content)

        # Reconcile on second call
        res_reconcile = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
        )
        self.assertEqual(res_reconcile.get("status"), "transitioned")
        self.assertTrue(res_reconcile.get("reconciled_existing_effect"))

    def test_merge_pr_refuses_failing_live_ci_despite_caller_checks(self) -> None:
        """merge_pr refuses if live provider has failing CI, even if caller passes passing checks."""
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")

        def live_failing_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "statusCheckRollup" in " ".join(cmd):
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        synthetic_passing_checks = [
            {"name": "fake/ci", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
        ]

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="merge_pr",
            claim_token=self.claim_token,
            pr_number=152,
            ci_checks=synthetic_passing_checks,
            command_runner=live_failing_runner,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "CI_CHECK_FAILED")

    def test_push_and_publish_pr_success(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")

        pr_created = [False]

        def mock_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "ls-remote" in cmd_str:
                return 0, f"{self.c_sha}\trefs/heads/feat/MP464\n"
            if "push" in cmd_str:
                return 0, "Everything up-to-date\n"
            if "pr create" in cmd_str:
                pr_created[0] = True
                return 0, "https://github.com/selamy-labs/agent-skills/pull/152\n"
            if "pr view" in cmd_str:
                if pr_created[0]:
                    return 0, json.dumps(
                        {
                            "number": 152,
                            "url": "https://github.com/selamy-labs/agent-skills/pull/152",
                            "state": "OPEN",
                            "headRefOid": self.c_sha,
                        }
                    )
                return 1, "no pull requests found for branch\n"
            return 0, ""

        # Push transition
        res_push = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="push",
            claim_token=self.claim_token,
            branch="feat/MP464",
            command_runner=mock_runner,
        )
        self.assertEqual(res_push.get("status"), "transitioned")
        self.assertEqual(res_push.get("action"), "push")

        # Publish PR transition
        res_pub = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="publish_pr",
            claim_token=self.claim_token,
            branch="feat/MP464",
            pr_title="feat: implement MP464",
            pr_body="Implementation details",
            command_runner=mock_runner,
        )
        self.assertEqual(res_pub.get("status"), "transitioned")
        self.assertEqual(res_pub.get("action"), "publish_pr")

    def test_pre_transition_guard_rejections(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")

        # 1. Missing claim token
        res1 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "complete_spec_task",
            claim_token=None,
        )
        self.assertEqual(res1.get("reason_code"), "MISSING_OR_EXPIRED_CLAIM")

        # 2. Stale claim token
        res2 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "complete_spec_task",
            claim_token="wrong-token",
        )
        self.assertEqual(res2.get("reason_code"), "STALE_CLAIM")

        # 3. Absent approval
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]["approval_ref"] = None
        status_file.write_text(json.dumps(doc))
        res3 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "complete_spec_task",
            claim_token=self.claim_token,
        )
        self.assertEqual(res3.get("reason_code"), "ABSENT_APPROVAL")

        # 4. Approval file not found
        doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]["approval_ref"] = (
            "/tmp/nonexistent-approval.txt"
        )
        status_file.write_text(json.dumps(doc))
        res4 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "complete_spec_task",
            claim_token=self.claim_token,
        )
        self.assertEqual(res4.get("reason_code"), "APPROVAL_FILE_NOT_FOUND")

        # 5. Approval withdrawn
        withdrawn_file = Path(self.temp_dir) / "withdrawn.txt"
        withdrawn_file.write_text("Coordinator [WITHDRAWN]\n")
        doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]["approval_ref"] = str(
            withdrawn_file
        )
        status_file.write_text(json.dumps(doc))
        res5 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "complete_spec_task",
            claim_token=self.claim_token,
        )
        self.assertEqual(res5.get("reason_code"), "APPROVAL_WITHDRAWN")

        # 6. Unresolved revise findings
        doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]["approval_ref"] = str(
            self.approval_file
        )
        doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]["remediation_required"] = True
        status_file.write_text(json.dumps(doc))
        res6 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "complete_spec_task",
            claim_token=self.claim_token,
        )
        self.assertEqual(res6.get("reason_code"), "UNRESOLVED_REVISE_FINDINGS")

        # 7. Missing transition evidence (prerequisite merge)
        doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]["remediation_required"] = False
        doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]["transitions"] = []
        status_file.write_text(json.dumps(doc))
        res7 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "complete_spec_task",
            claim_token=self.claim_token,
        )
        self.assertEqual(res7.get("reason_code"), "MISSING_TRANSITION_EVIDENCE")

        # 8. Missing mandatory gates in candidate
        doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]["candidate"]["gate_results"] = {}
        status_file.write_text(json.dumps(doc))
        res8 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
        )
        self.assertEqual(res8.get("reason_code"), "MISSING_MANDATORY_GATE")

        # 9. Invalid transition action
        res9 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "invalid_action",
            claim_token=self.claim_token,
        )
        self.assertEqual(res9.get("reason_code"), "INVALID_ACTION")

        # 10. Reviews missing on disk
        rec_dir = self.state_dir / "receipts"
        for rf in rec_dir.glob("review_*.json"):
            rf.unlink()
        res10 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
            branch="feat/MP464",
        )
        self.assertEqual(res10.get("reason_code"), "STALE_REVIEW_REVISION")


if __name__ == "__main__":
    unittest.main()
