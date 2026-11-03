#!/usr/bin/env python3
"""Tests for state transitions and stage guards in portfolio delivery control."""

import datetime
import hashlib
import json
import os
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
    _check_receipt_file_on_disk,
    _dispatch_action,
    _guard_live_provider_ci,
    execute_transition,
)


def _mock_push_pr_runner(c_sha: str, pr_created: list[bool], cmd: list[str]) -> tuple[int, str]:
    cmd_str = " ".join(cmd)
    if "ls-remote" in cmd_str:
        return 0, f"{c_sha}\trefs/heads/feat/MP464\n"
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
                    "headRefOid": c_sha,
                }
            )
        return 1, "no pull requests found for branch\n"
    return 0, ""


def _build_test_ledger(work_id: str, reviews_data: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    return {
        disp_id: {
            "dispatch_id": disp_id,
            "work_id": work_id,
            "family": fam,
            "reviewer_session_id": sess,
            "session_id": sess,
        }
        for fam, model, disp_id, sess in reviews_data
    }


def _build_test_reviews(reviews_data: list[tuple[str, str, str, str]], c_sha: str, t_sha: str) -> list[dict[str, Any]]:
    return [
        {
            "dispatch_id": disp_id,
            "reviewer_session_id": sess,
            "family": fam,
            "model": model,
            "verdict": "SHIP",
            "commit_sha": c_sha,
            "tree_sha": t_sha,
        }
        for fam, model, disp_id, sess in reviews_data
    ]


def _pr_view_resp(cmd_str: str, c_sha: str) -> tuple[int, str]:
    is_merged = "mergedAt" in cmd_str
    state = "MERGED" if is_merged else "OPEN"
    payload = {
        "state": state,
        "headRefOid": c_sha,
        "statusCheckRollup": [{"name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c_sha}],
    }
    if is_merged:
        payload["mergedAt"] = "2026-11-03T12:00:00Z"
        payload["mergeCommit"] = {"oid": "3" * 40}
    return 0, json.dumps(payload)


def _merge_flow_cmd(cmd_str: str, c_sha: str, log_out: str) -> tuple[int, str]:
    if "api user" in cmd_str:
        return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
    if "auth status" in cmd_str:
        return 0, "Logged in to github.com as selamy-labs-bot"
    if "required_status_checks" in cmd_str:
        return 0, json.dumps({"contexts": ["ci/test"]})
    if "pr view" in cmd_str:
        return _pr_view_resp(cmd_str, c_sha)
    if "pr merge" in cmd_str:
        return 0, "Merged PR #152"
    if "log" in cmd_str:
        return 0, log_out
    return 0, ""


class _MergeFlowRunner:
    def __init__(self, c_sha: str, t_sha: str):
        self.c_sha = c_sha
        bot_id = "selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>"
        self.log_out = f"claude-cir <claude-cir@selamy.dev>\n{bot_id}\n{t_sha}\n"

    def __call__(self, cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        return _merge_flow_cmd(" ".join(cmd), self.c_sha, self.log_out)


class _RepoCaptureRunner:
    def __init__(self, commit_sha: str) -> None:
        self.c_sha = commit_sha
        self.captured_cmds: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        self.captured_cmds.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "required_status_checks" in cmd_str:
            return 0, json.dumps({"contexts": ["test-gate"]})
        if "statusCheckRollup" in cmd_str:
            return 0, json.dumps(
                {
                    "headRefOid": self.c_sha,
                    "statusCheckRollup": [
                        {
                            "name": "test-gate",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "head_sha": self.c_sha,
                        }
                    ],
                }
            )
        if "api user" in cmd_str:
            return 0, json.dumps({"login": "selamy-labs-bot"})
        if "pr view" in cmd_str:
            return 0, json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-11-03T12:00:00Z",
                    "mergeCommit": {"oid": "4" * 40},
                    "headRefOid": self.c_sha,
                    "statusCheckRollup": [
                        {
                            "name": "test-gate",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "head_sha": self.c_sha,
                        }
                    ],
                }
            )
        return 0, ""


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

    def _write_test_review_receipts(self, work_id: str, reviews_data: list[tuple[str, str, str, str]]) -> None:
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
                        "report_sha256": hashlib.sha256(rep_path.read_bytes()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )

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
                "dispatch_ledger": _build_test_ledger(work_id, reviews_data) if qualified else {},
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
                        "reviews": _build_test_reviews(reviews_data, self.c_sha, self.t_sha) if qualified else [],
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
            self._write_test_review_receipts(work_id, reviews_data)

    def test_mp464_regression_red_ci_plus_unbound_reviews_refused_no_effect(self) -> None:
        """Regression test reproducing MP464 (ARGON-01 & ARGON-03):

        Tests both paths distinctly with exact assertions:
        (a) qualified work item with live provider returning failure refuses with CI_CHECK_FAILED and runner_calls > 0
        (b) unbound/unqualified reviews refuse with MISSING_QUALIFICATION, both with zero side-effects.
        """
        # Path A: Qualified work item, failing live provider CI
        self._setup_coordinator_state(
            task_id="MP464",
            qualified=True,
            project_ci_status="passing",
            with_merge_transition=True,
        )

        runner_calls = [0]

        def live_failing_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["ci/test"]})
            if "statusCheckRollup" in cmd_str:
                runner_calls[0] += 1
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "ci/test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res_a = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=live_failing_runner,
        )
        self.assertEqual(res_a.get("status"), "refused")
        self.assertEqual(res_a.get("reason_code"), "CI_CHECK_FAILED")
        self.assertGreater(runner_calls[0], 0, "Expected runner to be queried for live CI")

        # Zero side effects on SPEC.md
        spec_content = self.spec_file.read_text(encoding="utf-8")
        self.assertIn("- [ ] MP464: Reproduce CI regression and unbound reviews", spec_content)
        self.assertNotIn("- [x] MP464", spec_content)

        # Path B: Unqualified / unbound reviews
        self._setup_coordinator_state(
            task_id="MP464",
            qualified=False,
            project_ci_status="passing",
            with_merge_transition=True,
        )
        runner_calls[0] = 0

        res_b = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=live_failing_runner,
        )
        self.assertEqual(res_b.get("status"), "refused")
        self.assertEqual(res_b.get("reason_code"), "MISSING_QUALIFICATION")
        self.assertEqual(runner_calls[0], 0, "Unqualified work item must refuse before live CI query")

        spec_content_b = self.spec_file.read_text(encoding="utf-8")
        self.assertIn("- [ ] MP464: Reproduce CI regression and unbound reviews", spec_content_b)
        self.assertNotIn("- [x] MP464", spec_content_b)

    def test_complete_spec_task_refuses_failing_project_ci_despite_caller_checks(self) -> None:
        """Passing project row CI cannot override failing live provider CI."""
        self._setup_coordinator_state(
            task_id="MP464",
            qualified=True,
            project_ci_status="passing",
            with_merge_transition=True,
        )

        def live_failing_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "statusCheckRollup" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=live_failing_runner,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "CI_CHECK_FAILED")
        self.assertIn("- [ ] MP464", self.spec_file.read_text(encoding="utf-8"))

    def test_complete_spec_task_refuses_failing_live_provider_ci_despite_caller_checks(self) -> None:
        """Live provider failing check refuses complete_spec_task before effect."""
        self._setup_coordinator_state(
            task_id="MP464",
            qualified=True,
            project_ci_status=None,
            with_merge_transition=True,
        )

        def live_failing_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "statusCheckRollup" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
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
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "statusCheckRollup" in cmd_str:
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
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "statusCheckRollup" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="merge_pr",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=live_failing_runner,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "CI_CHECK_FAILED")

    def test_push_and_publish_pr_success(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")

        pr_created = [False]

        def mock_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return _mock_push_pr_runner(self.c_sha, pr_created, cmd)

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
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "statusCheckRollup" in cmd_str:
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
        """Negative tests for empty pr_ref, provider None, provider error, empty rollup, and failing checks."""
        # 1. Empty pr_ref
        ok1, err1 = _guard_live_provider_ci("", self.c_sha, None, self.worktree)
        self.assertFalse(ok1)
        self.assertEqual(err1, "CI_CHECK_FAILED")

        # 2. Provider None (e.g. runner command error)
        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "gh: command error"

        ok2, err2 = _guard_live_provider_ci("152", self.c_sha, fail_runner, self.worktree)
        self.assertFalse(ok2)
        self.assertEqual(err2, "CI_CHECK_MISSING")

        # 3. Provider returns empty rollup
        def empty_rollup_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["pytest"]})
            return 0, json.dumps({"headRefOid": self.c_sha, "statusCheckRollup": []})

        ok3, err3 = _guard_live_provider_ci("152", self.c_sha, empty_rollup_runner, self.worktree)
        self.assertFalse(ok3)
        self.assertEqual(err3, "CI_CHECK_MISSING")

        # 4. Provider returns failing check
        def failing_check_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["pytest"]})
            return 0, json.dumps(
                {
                    "headRefOid": self.c_sha,
                    "statusCheckRollup": [
                        {"name": "pytest", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha}
                    ],
                }
            )

        ok4, err4 = _guard_live_provider_ci("152", self.c_sha, failing_check_runner, self.worktree)
        self.assertFalse(ok4)
        self.assertEqual(err4, "CI_CHECK_FAILED")

        # 5. Provider returns passing checks for exact head
        def success_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["pytest"]})
            return 0, json.dumps(
                {
                    "headRefOid": self.c_sha,
                    "statusCheckRollup": [
                        {"name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                    ],
                }
            )

        ok5, err5 = _guard_live_provider_ci("152", self.c_sha, success_runner, self.worktree)
        self.assertTrue(ok5)
        self.assertEqual(err5, "")

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
            _dispatch_action,
            _guard_action_ci,
            _guard_claim_freshness,
            _guard_live_provider_ci,
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

        # _guard_live_provider_ci with empty rollup
        def empty_rollup_runner(cmd: Any, cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"headRefOid": self.c_sha, "statusCheckRollup": []})

        ok_l1, err_l1 = _guard_live_provider_ci("152", self.c_sha, empty_rollup_runner, self.worktree)
        self.assertFalse(ok_l1)
        self.assertEqual(err_l1, "CI_CHECK_MISSING")

        # _guard_action_ci for push action returns (True, None) (no CI guard for push)
        res_g_push = _guard_action_ci("push", self.c_sha, None, Path(self.temp_dir), "", "2026-11-03T10:00:00Z")
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
        )
        self.assertFalse(act_ok)
        self.assertIn("Unknown action", act_res.get("error", ""))

    def test_merge_pr_success(self) -> None:
        self._setup_coordinator_state(task_id="MP464_MERGE", qualified=True, project_ci_status="passing")
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        wi = doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464_MERGE"]
        wi["pr_number"] = 152
        status_file.write_text(json.dumps(doc), encoding="utf-8")

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464_MERGE",
            action="merge_pr",
            claim_token=self.claim_token,
            merge_identity="selamy-labs-bot",
            command_runner=_MergeFlowRunner(self.c_sha, self.t_sha),
        )
        self.assertEqual(res.get("status"), "transitioned")
        self.assertIn("merge_commit_sha", res)
        doc_after = json.loads(status_file.read_text(encoding="utf-8"))
        wi_after = doc_after["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464_MERGE"]
        self.assertEqual(wi_after.get("stage"), "ci")

    def test_check_on_disk_report_and_receipt_branches(self) -> None:
        from transitions import (
            _check_on_disk_report,
            _guard_claim_freshness,
            _matches_receipt_fields,
            _matches_report_fields,
        )

        self.assertFalse(_check_on_disk_report(None, None, self.state_dir, "opus", "s1", self.c_sha, self.t_sha, "d1"))
        self.assertFalse(
            _check_on_disk_report("/nonexistent.json", None, self.state_dir, "opus", "s1", self.c_sha, self.t_sha, "d1")
        )

        rep_p = Path(self.temp_dir) / "rep_temp.json"
        rep_p.write_text("{}", encoding="utf-8")
        self.assertFalse(
            _check_on_disk_report(str(rep_p), "wrong_sha", self.state_dir, "opus", "s1", self.c_sha, self.t_sha, "d1")
        )

        rep_p.write_text("{bad", encoding="utf-8")
        self.assertFalse(
            _check_on_disk_report(str(rep_p), None, self.state_dir, "opus", "s1", self.c_sha, self.t_sha, "d1")
        )

        rep_p.write_text(json.dumps({"schema": "bad"}), encoding="utf-8")
        self.assertFalse(
            _check_on_disk_report(str(rep_p), None, self.state_dir, "opus", "s1", self.c_sha, self.t_sha, "d1")
        )

        valid_rep = {
            "verdict": "SHIP",
            "commit_sha": self.c_sha,
            "tree_sha": self.t_sha,
            "family": "opus",
            "reviewer_session_id": "s1",
            "dispatch_id": "d1",
        }
        self.assertFalse(
            _matches_report_fields(dict(valid_rep, verdict="REVISE"), "opus", "s1", self.c_sha, self.t_sha, "d1")
        )
        self.assertFalse(
            _matches_report_fields(dict(valid_rep, commit_sha="bad"), "opus", "s1", self.c_sha, self.t_sha, "d1")
        )
        self.assertFalse(
            _matches_report_fields(dict(valid_rep, family="sol"), "opus", "s1", self.c_sha, self.t_sha, "d1")
        )
        self.assertFalse(
            _matches_report_fields(
                dict(valid_rep, reviewer_session_id="s2"), "opus", "s1", self.c_sha, self.t_sha, "d1"
            )
        )
        self.assertFalse(
            _matches_report_fields(dict(valid_rep, dispatch_id="d2"), "opus", "s1", self.c_sha, self.t_sha, "d1")
        )
        self.assertFalse(
            _matches_receipt_fields(dict(valid_rep, dispatch_id="d2"), "opus", "s1", self.c_sha, self.t_sha, "d1")
        )

        now_naive = datetime.datetime(2026, 11, 3, 10, 0)
        exp_aware = "2026-11-03T12:00:00Z"
        ok_tz, _ = _guard_claim_freshness({"claim": {"expires_at": exp_aware, "claim_token": "tok"}}, "tok", now_naive)
        self.assertTrue(ok_tz)

    def test_load_and_guard_transition_item_corrupted_json(self) -> None:
        status_file = self.state_dir / "coordinator-status.json"
        status_file.write_text("{bad json", encoding="utf-8")
        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="any-id",
            action="complete_spec_task",
            claim_token="tok",
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "WORK_NOT_ADMITTED")

    def test_default_runner_ci_with_fake_gh_on_path(self) -> None:
        """Default runner (command_runner=None) executes real gh executable from PATH."""
        fake_bin = Path(self.temp_dir) / "fake_bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_gh = fake_bin / "gh"
        fake_gh_content = f"""#!/bin/sh
case "$*" in
  *required_status_checks*)
    echo '{{"contexts": ["test"]}}'
    exit 0
    ;;
  *statusCheckRollup*)
    echo '{{"headRefOid": "{self.c_sha}", "statusCheckRollup": ['\\
         '{{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}}]}}'
    exit 0
    ;;
esac
exit 0
"""
        fake_gh.write_text(fake_gh_content, encoding="utf-8")
        fake_gh.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{fake_bin}:{old_path}"
        try:
            self._setup_coordinator_state(task_id="MP464", qualified=True, with_merge_transition=True)
            res = execute_transition(
                control_dir=str(self.state_dir),
                work_id="selamy-labs-agent-skills-152-MP464",
                action="complete_spec_task",
                claim_token=self.claim_token,
                pr_number=152,
                command_runner=None,
            )
            self.assertEqual(res.get("status"), "transitioned")
        finally:
            os.environ["PATH"] = old_path

    def test_required_checks_unrelated_check_only_refused(self) -> None:
        """Negative test for WFM14-01: rollup with only unrelated passing check refuses CI_CHECK_MISSING."""
        self._setup_coordinator_state(task_id="MP464", qualified=True, with_merge_transition=True)

        def unrelated_only_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["Quality"]})
            if "statusCheckRollup" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "zizmor", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=unrelated_only_runner,
        )
        self.assertEqual(res.get("status"), "refused")
        self.assertEqual(res.get("reason_code"), "CI_CHECK_MISSING")

    def test_required_checks_fetch_error_and_empty_list_fail_closed(self) -> None:
        """Negative tests for WFM14-01: provider fetch error or empty required list fail-closed."""
        self._setup_coordinator_state(task_id="MP464", qualified=True, with_merge_transition=True)

        def fetch_error_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str or "rules/branches" in cmd_str:
                return 1, "API rate limit or branch protection not configured"
            if "statusCheckRollup" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res_err = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=fetch_error_runner,
        )
        self.assertEqual(res_err.get("status"), "refused")
        self.assertEqual(res_err.get("reason_code"), "CI_CHECK_MISSING")

        def empty_list_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": []})
            if "statusCheckRollup" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        res_empty = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=empty_list_runner,
        )
        self.assertEqual(res_empty.get("status"), "refused")
        self.assertEqual(res_empty.get("reason_code"), "CI_CHECK_MISSING")

    def test_required_checks_policy_fallback_success_and_negative_control(self) -> None:
        """Tests policy fallback when provider fetch fails, plus negative control for missing check."""
        self._setup_coordinator_state(task_id="MP464", qualified=True, with_merge_transition=True)
        policy_file = self.state_dir / "delivery-policy.json"
        policy_file.write_text(
            json.dumps({"required_checks": {"agent-skills": ["policy-gate"]}}),
            encoding="utf-8",
        )

        def runner_with_policy_gate(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str or "rules/branches" in cmd_str:
                return 1, "Provider protection endpoint unavailable"
            if "statusCheckRollup" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {
                                "name": "policy-gate",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                                "head_sha": self.c_sha,
                            }
                        ],
                    }
                )
            return 0, ""

        res_ok = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=runner_with_policy_gate,
            policy_path=policy_file,
        )
        self.assertEqual(res_ok.get("status"), "transitioned")

        def runner_missing_policy_gate(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "required_status_checks" in cmd_str or "rules/branches" in cmd_str:
                return 1, "Provider protection endpoint unavailable"
            if "statusCheckRollup" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {
                                "name": "other-gate",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                                "head_sha": self.c_sha,
                            }
                        ],
                    }
                )
            return 0, ""

        res_fail = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=runner_missing_policy_gate,
            policy_path=policy_file,
        )
        self.assertEqual(res_fail.get("status"), "refused")
        self.assertEqual(res_fail.get("reason_code"), "CI_CHECK_MISSING")

    def test_malformed_reviews_family_and_session_refused_cleanly(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        wi = doc["delivery_control"]["work_items"]["selamy-labs-agent-skills-152-MP464"]

        # Case 1: non-string dict family in wi['reviews']
        wi["reviews"] = [
            {
                "family": {"bad": "opus"},
                "reviewer_session_id": "s1",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
            {
                "family": "sol",
                "reviewer_session_id": "s2",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
            {
                "family": "argon",
                "reviewer_session_id": "s3",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
        ]
        status_file.write_text(json.dumps(doc), encoding="utf-8")

        res1 = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
        )
        self.assertEqual(res1.get("status"), "refused")
        self.assertEqual(res1.get("reason_code"), "MISSING_QUALIFICATION")

        # Case 2: non-string reviewer_session_id
        wi["reviews"] = [
            {
                "family": "opus",
                "reviewer_session_id": ["bad", "sess"],
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
            {
                "family": "sol",
                "reviewer_session_id": "s2",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
            {
                "family": "argon",
                "reviewer_session_id": "s3",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
        ]
        status_file.write_text(json.dumps(doc), encoding="utf-8")

        res2 = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
        )
        self.assertEqual(res2.get("status"), "refused")
        self.assertEqual(res2.get("reason_code"), "MISSING_QUALIFICATION")

        # Case 3: non-dict item in wi['reviews']
        wi["reviews"] = [
            "not-a-dict",
            {
                "family": "sol",
                "reviewer_session_id": "s2",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
            {
                "family": "argon",
                "reviewer_session_id": "s3",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
        ]
        status_file.write_text(json.dumps(doc), encoding="utf-8")

        res3 = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="complete_spec_task",
            claim_token=self.claim_token,
            pr_number=152,
        )
        self.assertEqual(res3.get("status"), "refused")
        self.assertEqual(res3.get("reason_code"), "MISSING_QUALIFICATION")

    def test_merge_pr_passes_repo_and_checks_to_provider(self) -> None:
        self._setup_coordinator_state(task_id="MP464", qualified=True, project_ci_status="passing")
        capture_runner = _RepoCaptureRunner(self.c_sha)

        res = execute_transition(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-MP464",
            action="merge_pr",
            claim_token=self.claim_token,
            pr_number=152,
            command_runner=capture_runner,
        )
        self.assertEqual(res.get("status"), "transitioned")
        pr_view_cmds = [c for c in capture_runner.captured_cmds if "pr" in c and "view" in c]
        self.assertTrue(any("--repo" in c and "selamy-labs/agent-skills" in c for c in pr_view_cmds))


if __name__ == "__main__":
    unittest.main()
