#!/usr/bin/env python3
"""Integration and CLI tests for workflow coordinator."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from workflow import (
    admit_work,
    build_parser,
    check_qualification,
    claim_work,
    execute_transition,
    load_coordinator_status,
    main,
    make_refusal,
    record_dispatch,
    record_implementation,
    record_review,
    save_coordinator_status,
    validate_review_receipt,
    workflow_lock,
    write_receipt,
)


class TestWorkflowIntegrationAndCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="pdc-test-wf-")
        self.state_dir = Path(self.temp_dir) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.worktree = Path(self.temp_dir) / "worktree"
        self.worktree.mkdir(parents=True, exist_ok=True)
        (self.worktree / ".git").mkdir(parents=True, exist_ok=True)

        self.spec_file = self.worktree / "SPEC.md"
        self.spec_file.write_text("- [ ] TASK_01: E2E Integration test\n")

        self.c_sha = "1" * 40
        self.t_sha = "2" * 40

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_workflow_lock(self) -> None:
        with workflow_lock(self.state_dir):
            lock_file = self.state_dir / ".workflow.lock"
            self.assertTrue(lock_file.exists())

    def test_re_exports_and_contract_imports(self) -> None:
        # Verify all expected functions are callable from workflow module
        self.assertTrue(callable(admit_work))
        self.assertTrue(callable(claim_work))
        self.assertTrue(callable(record_implementation))
        self.assertTrue(callable(record_dispatch))
        self.assertTrue(callable(record_review))
        self.assertTrue(callable(check_qualification))
        self.assertTrue(callable(execute_transition))
        self.assertTrue(callable(validate_review_receipt))

    def test_cli_parser_structure(self) -> None:
        parser = build_parser()
        self.assertIsNotNone(parser)
        # Test parsing help or basic args
        with self.assertRaises(SystemExit):
            parser.parse_args(["--help"])

    def test_end_to_end_lifecycle_via_cli(self) -> None:
        # 1. Admit work item via CLI
        code = main(
            [
                "admit",
                "--control-dir",
                str(self.state_dir),
                "--repo",
                "selamy-labs/agent-skills",
                "--issue",
                "152",
                "--spec-path",
                str(self.spec_file),
                "--task-id",
                "TASK_01",
                "--allowed-roots",
                str(self.temp_dir),
            ]
        )
        self.assertEqual(code, 0)
        work_id = "selamy-labs-agent-skills-152-TASK_01"

        # 2. Claim work item via CLI
        code = main(
            [
                "claim",
                "--control-dir",
                str(self.state_dir),
                "--work-id",
                work_id,
                "--owner",
                "lane-flash",
                "--session-id",
                "session-w1",
                "--worktree",
                str(self.worktree),
            ]
        )
        self.assertEqual(code, 0)

        # Read claim token
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        claim_token = doc["delivery_control"]["work_items"][work_id]["claim"]["claim_token"]
        self.assertTrue(claim_token)

        # 3. Record dispatches for 3 reviewers via CLI
        for fam, sess in [("opus", "s-opus"), ("sol", "s-sol"), ("argon", "s-argon")]:
            code = main(
                [
                    "record-dispatch",
                    "--control-dir",
                    str(self.state_dir),
                    "--work-id",
                    work_id,
                    "--dispatch-id",
                    f"disp-{fam}",
                    "--family",
                    fam,
                    "--reviewer-session-id",
                    sess,
                ]
            )
            self.assertEqual(code, 0)

        # Verify dispatches recorded in ledger
        status_file = self.state_dir / "coordinator-status.json"
        doc = json.loads(status_file.read_text(encoding="utf-8"))
        ledger = doc["delivery_control"]["dispatch_ledger"]
        self.assertEqual(len(ledger), 3)

    def test_cli_failure_exit_code_on_refusal(self) -> None:
        # Trying to admit nonexistent spec task should return 1
        code = main(
            [
                "admit",
                "--control-dir",
                str(self.state_dir),
                "--repo",
                "selamy-labs/agent-skills",
                "--issue",
                "152",
                "--spec-path",
                str(self.spec_file),
                "--task-id",
                "NONEXISTENT",
                "--allowed-roots",
                str(self.temp_dir),
            ]
        )
        self.assertEqual(code, 1)

    def test_status_persistence_and_receipt_helpers(self) -> None:
        # Load from fresh dir
        doc = load_coordinator_status(self.state_dir)
        self.assertEqual(doc.get("state"), "active")
        self.assertIn("delivery_control", doc)

        # Save and reload
        doc["delivery_control"]["projects"]["test-proj"] = {"stage": "ready"}
        save_coordinator_status(self.state_dir, doc)
        doc2 = load_coordinator_status(self.state_dir)
        self.assertIn("test-proj", doc2["delivery_control"]["projects"])

        # write_receipt
        rec_path = write_receipt(self.state_dir, "test_receipt.json", {"key": "val"})
        self.assertTrue(rec_path.exists())
        self.assertEqual(json.loads(rec_path.read_text(encoding="utf-8")), {"key": "val"})

        # make_refusal
        ref = make_refusal(self.state_dir, "task-1", "TEST_REASON", "test failure message")
        self.assertEqual(ref.get("status"), "refused")
        self.assertEqual(ref.get("reason_code"), "TEST_REASON")
        self.assertTrue(Path(ref["receipt_path"]).exists())

    def test_cli_record_implementation_and_transitions(self) -> None:
        # Admit and claim
        admit_work(
            control_dir=str(self.state_dir),
            repo="selamy-labs/agent-skills",
            issue_num="152",
            spec_path=str(self.spec_file),
            task_id="TASK_01",
            allowed_roots=[str(self.temp_dir)],
        )
        c_res = claim_work(
            control_dir=str(self.state_dir),
            work_id="selamy-labs-agent-skills-152-TASK_01",
            owner="lane-test",
            session_id="sess-w1",
            worktree=str(self.worktree),
        )
        claim_token = c_res["claim_token"]

        # Record implementation via CLI (failing gate to verify CLI refusal exit code)
        code_fail = main(
            [
                "record-implementation",
                "--control-dir",
                str(self.state_dir),
                "--work-id",
                "selamy-labs-agent-skills-152-TASK_01",
                "--claim-token",
                claim_token,
                "--commit-sha",
                self.c_sha,
                "--tree-sha",
                self.t_sha,
                "--gate-results",
                json.dumps({"pytest": {"exit_code": 1}}),
            ]
        )
        self.assertEqual(code_fail, 1)

        # Execute transition via CLI (push on main should refuse with exit code 1)
        code_trans = main(
            [
                "execute-transition",
                "--control-dir",
                str(self.state_dir),
                "--work-id",
                "selamy-labs-agent-skills-152-TASK_01",
                "--action",
                "push",
                "--claim-token",
                claim_token,
                "--branch",
                "main",
            ]
        )
        self.assertEqual(code_trans, 1)


if __name__ == "__main__":
    unittest.main()
