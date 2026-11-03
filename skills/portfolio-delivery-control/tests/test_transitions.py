#!/usr/bin/env python3
"""Tests for state transitions and stage guards in portfolio delivery control."""

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

from transitions import (  # noqa: E402
    _check_merge_pr_ci_failure,
    _check_receipt_file_on_disk,
    _dispatch_action,
    _guard_spec_task_ci,
    execute_transition,
)


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
                }
                if qualified
                else {},
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
            rep_dir = self.state_dir / "reports"
            rep_dir.mkdir(parents=True, exist_ok=True)
            for fam, model, disp_id, sess in reviews_data:
                rep_path = rep_dir / f"report_{work_id}_{fam}_{sess}.json"
                rep_path.write_text(
                    json.dumps(
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
                    ),
                    encoding="utf-8",
                )
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

        def mock_live_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "statusCheckRollup" in " ".join(cmd):
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            command_runner=mock_live_runner,
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
            command_runner=mock_live_runner,
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

    def test_approval_verification_details(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        wi = doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]

        # Valid dict approval with matching sha256
        import hashlib

        h = hashlib.sha256(self.approval_file.read_bytes()).hexdigest()
        wi["approval_ref"] = {"path": str(self.approval_file), "sha256": h}
        status_file.write_text(json.dumps(doc))
        res_ok = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
            branch="feat/MP464",
            command_runner=lambda cmd, cwd=None: (0, f"{self.c_sha}\trefs/heads/feat/MP464\n"),
        )
        self.assertEqual(res_ok.get("status"), "transitioned")

        # Approval file has [WITHDRAWN]
        withdrawn_app = Path(self.temp_dir) / "withdrawn.txt"
        withdrawn_app.write_text("Coordinator [WITHDRAWN] approval", encoding="utf-8")
        wi["approval_ref"] = str(withdrawn_app)
        status_file.write_text(json.dumps(doc))
        res_w = execute_transition(
            str(self.state_dir), "selamy-labs-agent-skills-152-MP464", "push", claim_token=self.claim_token
        )
        self.assertEqual(res_w.get("reason_code"), "APPROVAL_WITHDRAWN")

        # Approval file read error (pointing to a directory)
        app_dir = Path(self.temp_dir) / "app_dir"
        app_dir.mkdir(parents=True, exist_ok=True)
        wi["approval_ref"] = str(app_dir)
        status_file.write_text(json.dumps(doc))
        res_r = execute_transition(
            str(self.state_dir), "selamy-labs-agent-skills-152-MP464", "push", claim_token=self.claim_token
        )
        self.assertEqual(res_r.get("reason_code"), "APPROVAL_READ_FAILED")

    def test_claim_freshness_timezone_handling(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        wi = doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]

        # Invalid ISO string in expires_at
        wi["claim"]["expires_at"] = "not-an-iso-timestamp"
        status_file.write_text(json.dumps(doc))
        res1 = execute_transition(
            str(self.state_dir), "selamy-labs-agent-skills-152-MP464", "push", claim_token=self.claim_token
        )
        self.assertEqual(res1.get("reason_code"), "CLAIM_EXPIRED")

        # Expired naive timestamp compared to timezone-aware now
        wi["claim"]["expires_at"] = "2020-01-01T00:00:00"
        status_file.write_text(json.dumps(doc))
        res2 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
            now="2026-11-03T12:00:00Z",
        )
        self.assertEqual(res2.get("reason_code"), "CLAIM_EXPIRED")

    def test_receipt_file_on_disk_validations(self) -> None:
        # Nonexistent
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w1", "opus", "s1", self.c_sha, self.t_sha))

        rec_dir = self.state_dir / "receipts"
        rec_dir.mkdir(parents=True, exist_ok=True)
        rec_file = rec_dir / "review_w1_opus_s1.json"

        # Invalid JSON
        rec_file.write_text("not json", encoding="utf-8")
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w1", "opus", "s1", self.c_sha, self.t_sha))

        # Not dict
        rec_file.write_text(json.dumps(["list"]), encoding="utf-8")
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w1", "opus", "s1", self.c_sha, self.t_sha))

        # Verdict != SHIP
        rec_file.write_text(json.dumps({"verdict": "REVISE"}), encoding="utf-8")
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w1", "opus", "s1", self.c_sha, self.t_sha))

        # Family / session mismatch
        rec_file.write_text(json.dumps({"verdict": "SHIP", "family": "sol", "session_id": "s1"}), encoding="utf-8")
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w1", "opus", "s1", self.c_sha, self.t_sha))

        # Dispatch ID mismatch
        rec_file.write_text(
            json.dumps(
                {
                    "verdict": "SHIP",
                    "family": "opus",
                    "session_id": "s1",
                    "dispatch_id": "disp-wrong",
                    "commit_sha": self.c_sha,
                    "tree_sha": self.t_sha,
                    "report_path": "",
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(
            _check_receipt_file_on_disk(
                self.state_dir, "w1", "opus", "s1", self.c_sha, self.t_sha, dispatch_id="disp-expected"
            )
        )

    def test_spec_task_prerequisite_action_variations(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        wi = doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]

        # required_completion_action = "publish_pr" with publish_pr transition
        wi["required_completion_action"] = "publish_pr"
        wi["transitions"] = [
            {"action": "publish_pr", "commit_sha": self.c_sha, "tree_sha": self.t_sha, "transitioned_at": "now"}
        ]
        status_file.write_text(json.dumps(doc))

        def live_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "statusCheckRollup" in " ".join(cmd):
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res1 = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "complete_spec_task",
            claim_token=self.claim_token,
            command_runner=live_runner,
        )
        self.assertEqual(res1.get("status"), "transitioned")

    def test_unknown_action_dispatch(self) -> None:
        ok, res = _dispatch_action(
            "unknown_action",
            self.worktree,
            "feat/branch",
            "main",
            self.c_sha,
            "152",
            "Title",
            "Body",
            self.spec_file,
            "TASK_01",
            None,
            None,
        )
        self.assertFalse(ok)
        self.assertEqual(res.get("reason_code"), "INVALID_ACTION")

    def test_receipt_file_on_disk_report_errors(self) -> None:
        rec_dir = self.state_dir / "receipts"
        rec_dir.mkdir(parents=True, exist_ok=True)
        rec_file = rec_dir / "review_w2_opus_s2.json"

        # Commit sha mismatch
        rec_file.write_text(
            json.dumps(
                {
                    "verdict": "SHIP",
                    "family": "opus",
                    "session_id": "s2",
                    "commit_sha": "wrong_sha",
                    "tree_sha": self.t_sha,
                    "report_path": "/tmp/fake.json",
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w2", "opus", "s2", self.c_sha, self.t_sha))

        # Missing report_path
        rec_file.write_text(
            json.dumps(
                {
                    "verdict": "SHIP",
                    "family": "opus",
                    "session_id": "s2",
                    "commit_sha": self.c_sha,
                    "tree_sha": self.t_sha,
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w2", "opus", "s2", self.c_sha, self.t_sha))

        # report_path does not exist
        rec_file.write_text(
            json.dumps(
                {
                    "verdict": "SHIP",
                    "family": "opus",
                    "session_id": "s2",
                    "commit_sha": self.c_sha,
                    "tree_sha": self.t_sha,
                    "report_path": str(Path(self.temp_dir) / "nonexistent-report.json"),
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w2", "opus", "s2", self.c_sha, self.t_sha))

        # report_path has invalid JSON
        rep_file = Path(self.temp_dir) / "invalid-rep.json"
        rep_file.write_text("corrupted json", encoding="utf-8")
        rec_file.write_text(
            json.dumps(
                {
                    "verdict": "SHIP",
                    "family": "opus",
                    "session_id": "s2",
                    "commit_sha": self.c_sha,
                    "tree_sha": self.t_sha,
                    "report_path": str(rep_file),
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w2", "opus", "s2", self.c_sha, self.t_sha))

        # report_path has schema violation (missing keys)
        rep_file.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
        self.assertFalse(_check_receipt_file_on_disk(self.state_dir, "w2", "opus", "s2", self.c_sha, self.t_sha))

    def test_pr_ref_empty_and_fallback_ci(self) -> None:
        doc = {
            "delivery_control": {
                "projects": {
                    "agent-skills": {
                        "ci": {
                            "status": "passing",
                            "revision": self.c_sha,
                            "evidence": [
                                {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                            ],
                        }
                    }
                }
            }
        }
        wi = {"project": "agent-skills"}

        # Empty pr_ref falls back to project row passing CI
        ok1, err1, checks1 = _guard_spec_task_ci(doc, wi, self.c_sha, None, None, self.worktree, "")
        self.assertTrue(ok1)
        self.assertEqual(len(checks1), 1)

        # Empty pr_ref and no project row CI, falls back to valid caller checks
        empty_doc = {"delivery_control": {"projects": {}}}
        caller_checks = [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}]
        ok2, err2, checks2 = _guard_spec_task_ci(empty_doc, wi, self.c_sha, caller_checks, None, self.worktree, "")
        self.assertTrue(ok2)

        # Empty pr_ref and caller checks has failing check
        failing_caller = [{"name": "test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha}]
        ok3, err3, _ = _guard_spec_task_ci(empty_doc, wi, self.c_sha, failing_caller, None, self.worktree, "")
        self.assertFalse(ok3)

        # Empty pr_ref and no checks at all
        ok4, err4, _ = _guard_spec_task_ci(empty_doc, wi, self.c_sha, None, None, self.worktree, "")
        self.assertFalse(ok4)
        self.assertEqual(err4, "required checks are missing")

        # _check_merge_pr_ci_failure variations
        p_row_pass = doc["delivery_control"]["projects"]["agent-skills"]
        fail_ci1, _ = _check_merge_pr_ci_failure(p_row_pass, self.c_sha, None, None, self.worktree, "")
        self.assertFalse(fail_ci1)

        p_row_empty: dict[str, Any] = {}
        fail_ci2, _ = _check_merge_pr_ci_failure(p_row_empty, self.c_sha, caller_checks, None, self.worktree, "")
        self.assertFalse(fail_ci2)

        fail_ci3, _ = _check_merge_pr_ci_failure(p_row_empty, self.c_sha, failing_caller, None, self.worktree, "")
        self.assertTrue(fail_ci3)

    def test_protected_branch_push_to_base_branch(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")
        res = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
            branch="devel",
            base_branch="devel",
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "PROTECTED_BRANCH_PUSH")

    def test_provider_dispatch_action_failure(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")

        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "ls-remote" in " ".join(cmd):
                return 0, ""
            if "push" in " ".join(cmd):
                return 1, "remote rejected (pre-receive hook declined)"
            return 0, ""

        res = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
            branch="feat/MP464",
            command_runner=fail_runner,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertIn("rejected", res.get("error", ""))

    def test_approval_relative_path_withdrawn_and_digest_mismatch(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")

        # Relative path approval
        rel_app_file = self.state_dir / "rel_approval.md"
        rel_app_file.write_text("LGTM approved for MP464", encoding="utf-8")
        rel_sha = hashlib.sha256(rel_app_file.read_bytes()).hexdigest()

        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        wi = doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]
        wi["approval_ref"] = {"path": "rel_approval.md", "sha256": rel_sha}
        status_file.write_text(json.dumps(doc), encoding="utf-8")

        res_rel = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
            branch="feat/MP464",
            command_runner=lambda cmd, cwd=None: (0, ""),
        )
        self.assertEqual(res_rel.get("status"), "transitioned")

        # Withdrawn approval
        rel_app_file.write_text("[WITHDRAWN] Not approved", encoding="utf-8")
        res_withdrawn = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
            branch="feat/MP464",
            command_runner=lambda cmd, cwd=None: (0, ""),
        )
        self.assertEqual(res_withdrawn.get("status"), "refused")
        self.assertEqual(res_withdrawn.get("reason_code"), "APPROVAL_WITHDRAWN")

        # Digest mismatch
        rel_app_file.write_text("Modified content after approval", encoding="utf-8")
        res_mismatch = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "push",
            claim_token=self.claim_token,
            branch="feat/MP464",
            command_runner=lambda cmd, cwd=None: (0, ""),
        )
        self.assertEqual(res_mismatch.get("status"), "refused")
        self.assertEqual(res_mismatch.get("reason_code"), "APPROVAL_DIGEST_MISMATCH")

    def test_transitions_coverage_branches(self) -> None:
        import datetime

        from transitions import (  # noqa: E402
            _check_project_row_ci,
            _dispatch_action,
            _evaluate_live_ci_checks,
            _guard_action_ci,
            _guard_claim_freshness,
            _verify_live_approval,
        )

        # _verify_live_approval with None
        ok_a, err_a = _verify_live_approval(None)
        self.assertFalse(ok_a)
        self.assertEqual(err_a, "ABSENT_APPROVAL")

        # _guard_claim_freshness with empty expires_at
        ok_c1, err_c1 = _guard_claim_freshness(
            {"claim": {"claim_token": "tok"}},
            "tok",
            datetime.datetime.now(datetime.UTC),
        )
        self.assertFalse(ok_c1)
        self.assertEqual(err_c1, "CLAIM_EXPIRED")

        # _guard_claim_freshness with naive and aware datetimes
        ok_c2, err_c2 = _guard_claim_freshness(
            {"claim": {"claim_token": "tok", "expires_at": "2026-11-03T10:00:00"}},
            "tok",
            datetime.datetime(2026, 11, 3, 12, 0, tzinfo=datetime.UTC),
        )
        self.assertFalse(ok_c2)
        self.assertEqual(err_c2, "CLAIM_EXPIRED")

        # _check_project_row_ci with failing status
        p_row_fail = {"ci": {"revision": self.c_sha, "status": "failing", "evidence": []}}
        ok_ci1, _ = _check_project_row_ci(p_row_fail, self.c_sha)
        self.assertFalse(ok_ci1)

        # _check_project_row_ci with passing status but failing evidence check
        p_row_fail_ev = {
            "ci": {
                "revision": self.c_sha,
                "status": "passing",
                "evidence": [{"name": "pytest", "status": "failing"}],
            }
        }
        ok_ci2, _ = _check_project_row_ci(p_row_fail_ev, self.c_sha)
        self.assertFalse(ok_ci2)

        # _evaluate_live_ci_checks with empty live_checks list
        ok_l1, err_l1, _ = _evaluate_live_ci_checks([], self.c_sha)
        self.assertFalse(ok_l1)
        self.assertEqual(err_l1, "required checks are missing")

        # _guard_action_ci for push action returns (True, None) (no CI guard for push)
        res_g_push = _guard_action_ci(
            "push", {}, {}, self.c_sha, None, None, Path(self.temp_dir), "", "2026-11-03T10:00:00Z"
        )
        self.assertEqual(res_g_push, (True, None))

        # execute_transition with invalid action
        res_bad_act = execute_transition(
            str(self.state_dir),
            "selamy-labs-agent-skills-152-MP464",
            "invalid_action",
            claim_token=self.claim_token,
        )
        self.assertEqual(res_bad_act.get("status"), "refused")
        self.assertEqual(res_bad_act.get("reason_code"), "INVALID_ACTION")

        # _dispatch_action with unknown action
        act_ok, act_res = _dispatch_action(
            "unknown_action",
            Path(self.temp_dir),
            "feat/branch",
            "main",
            self.c_sha,
            "",
            "",
            "",
            None,
            "",
            None,
            None,
        )
        self.assertFalse(act_ok)
        self.assertIn("Unknown action", act_res.get("error", ""))


if __name__ == "__main__":
    unittest.main()
