#!/usr/bin/env python3
"""Table-driven guard inventory tests ensuring complete ratchet and no side effects.

Enumerates every refusal code the stage guard layer emits.
Drives real transitions to each refusal code, asserts no side effect,
and enforces bidirectional synchronization between code and inventory.
"""

from __future__ import annotations

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

from transitions import execute_transition  # noqa: E402

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
        return work_id

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

        # Codes in code but not in inventory
        missing_in_inventory = extracted_codes - TRANSITION_REFUSAL_CODES
        self.assertEqual(
            missing_in_inventory,
            set(),
            f"Refusal codes present in transitions.py but missing from inventory: {missing_in_inventory}",
        )

        # Codes in inventory but not in code
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

    def test_table_driven_transition_refusals_and_no_side_effects(self) -> None:  # noqa: C901
        """Table-driven test driving real transitions to every refusal code and asserting no side effect."""
        for code in sorted(TRANSITION_REFUSAL_CODES):
            with self.subTest(refusal_code=code):
                work_id = self._setup_valid_baseline(task_id=f"TASK_{code}")
                status_file = self.state_dir / "coordinator-status.json"
                doc = json.loads(status_file.read_text(encoding="utf-8"))
                wi = doc["delivery_control"]["work_items"][work_id]

                # Specific setup per refusal code
                action = "complete_spec_task"
                claim_token = self.claim_token
                merge_ident = "selamy-labs-bot"
                target_b = "feat/inventory"
                cmd_runner = None

                if code == "INVALID_ACTION":
                    action = "nonexistent_action"
                elif code == "PROTECTED_BRANCH_PUSH":
                    action = "push"
                    target_b = "main"
                elif code == "DISALLOWED_MERGE_IDENTITY":
                    action = "merge_pr"
                    merge_ident = "malicious-hacker"
                elif code == "WORK_NOT_ADMITTED":
                    work_id = "nonexistent-work-item"
                elif code == "ABSENT_APPROVAL":
                    wi["approval_ref"] = None
                    status_file.write_text(json.dumps(doc))
                elif code == "APPROVAL_FILE_NOT_FOUND":
                    wi["approval_ref"] = str(Path(self.temp_dir) / "missing-approval.txt")
                    status_file.write_text(json.dumps(doc))
                elif code == "APPROVAL_READ_FAILED":
                    dir_as_file = Path(self.temp_dir) / "dir_approval"
                    dir_as_file.mkdir(parents=True, exist_ok=True)
                    wi["approval_ref"] = str(dir_as_file)
                    status_file.write_text(json.dumps(doc))
                elif code == "APPROVAL_WITHDRAWN":
                    withdrawn_file = Path(self.temp_dir) / "withdrawn_app.txt"
                    withdrawn_file.write_text("Coordinator [WITHDRAWN] approval", encoding="utf-8")
                    wi["approval_ref"] = str(withdrawn_file)
                    status_file.write_text(json.dumps(doc))
                elif code == "APPROVAL_DIGEST_MISMATCH":
                    wi["approval_ref"] = {"path": str(self.approval_file), "sha256": "f" * 64}
                    status_file.write_text(json.dumps(doc))
                elif code == "UNRESOLVED_REVISE_FINDINGS":
                    wi["remediation_required"] = True
                    wi["defects"] = [{"id": "DEF-01", "summary": "Must fix"}]
                    status_file.write_text(json.dumps(doc))
                elif code == "MISSING_OR_EXPIRED_CLAIM":
                    claim_token = None
                elif code == "STALE_CLAIM":
                    claim_token = "wrong-stale-token"
                elif code == "CLAIM_EXPIRED":
                    wi["claim"]["expires_at"] = "2020-01-01T00:00:00Z"
                    status_file.write_text(json.dumps(doc))
                elif code == "MISSING_QUALIFICATION":
                    wi["qualified"] = False
                    status_file.write_text(json.dumps(doc))
                elif code == "STALE_REVIEW_REVISION":
                    # Tamper with an on-disk receipt to have stale revision
                    stale_rec = self.state_dir / "receipts" / f"review_{work_id}_opus_sess-r1.json"
                    rec_content = json.loads(stale_rec.read_text(encoding="utf-8"))
                    rec_content["commit_sha"] = "9" * 40
                    stale_rec.write_text(json.dumps(rec_content), encoding="utf-8")
                elif code == "MISSING_MANDATORY_GATE":
                    wi["candidate"]["gate_results"]["pytest"] = {"exit_code": 1, "passed": False}
                    status_file.write_text(json.dumps(doc))
                elif code == "MISSING_TRANSITION_EVIDENCE":
                    wi["transitions"] = []
                    status_file.write_text(json.dumps(doc))
                elif code == "CI_CHECK_FAILED":
                    action = "merge_pr"
                    doc["delivery_control"]["projects"]["agent-skills"]["ci"]["status"] = "failing"
                    status_file.write_text(json.dumps(doc))

                res = execute_transition(
                    control_dir=str(self.state_dir),
                    work_id=work_id,
                    action=action,
                    claim_token=claim_token,
                    branch=target_b,
                    merge_identity=merge_ident,
                    command_runner=cmd_runner,
                )

                # 1. Assert status and exact reason code
                self.assertEqual(res.get("status"), "refused", f"Expected refused for {code}, got {res}")
                self.assertEqual(res.get("reason_code"), code, f"Expected reason_code {code}, got {res}")

                # 2. Assert zero side-effects
                spec_content = self.spec_file.read_text(encoding="utf-8")
                self.assertIn("- [ ] TASK_01", spec_content, f"SPEC.md was modified during refusal of {code}!")
                self.assertNotIn("- [x] TASK_01", spec_content, f"SPEC.md was checked during refusal of {code}!")

                # Status file work item should not have advanced stage or added transition
                doc_after = json.loads(status_file.read_text(encoding="utf-8"))
                if work_id in doc_after["delivery_control"]["work_items"]:
                    wi_after = doc_after["delivery_control"]["work_items"][work_id]
                    self.assertNotEqual(wi_after.get("stage"), "done", f"Stage advanced to done during {code}!")
                    # Transition count should not have grown
                    orig_trans_count = 1 if code != "MISSING_TRANSITION_EVIDENCE" else 0
                    self.assertEqual(
                        len(wi_after.get("transitions", [])),
                        orig_trans_count,
                        f"Transitions recorded during refusal of {code}!",
                    )


if __name__ == "__main__":
    unittest.main()
