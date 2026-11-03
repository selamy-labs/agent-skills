#!/usr/bin/env python3
"""Behavioral unit and negative tests for review receipt schema, ledger, and qualification."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from receipts import (  # noqa: E402
    record_dispatch,
    record_review,
    validate_review_receipt,
)
from workflow import load_coordinator_status, save_coordinator_status  # noqa: E402


class TestReviewReceipts(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.control_dir = Path(self.temp_dir.name)
        self.c_sha = "a" * 40
        self.t_sha = "b" * 40
        self.work_id = "test-repo-1-TASK-001"
        self._init_admitted_work()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _init_admitted_work(self) -> None:
        doc = load_coordinator_status(self.control_dir)
        wi = {
            "work_id": self.work_id,
            "repo": "selamy-labs/test-repo",
            "issue_num": "1",
            "task_id": "TASK-001",
            "commit_sha": self.c_sha,
            "tree_sha": self.t_sha,
            "stage": "review",
            "candidate": {"commit_sha": self.c_sha, "tree_sha": self.t_sha},
            "claim": {"owner": "selamy-labs-bot", "session_id": "sess-implementer"},
            "reviews": [],
        }
        doc["delivery_control"]["work_items"][self.work_id] = wi
        save_coordinator_status(self.control_dir, doc)

    def _make_valid_report_data(
        self,
        family: str = "opus",
        model: str = "claude-opus-5-5",
        session_id: str = "sess-opus",
        dispatch_id: str = "disp-opus",
        verdict: str = "SHIP",
        findings: list[dict[str, Any]] | None = None,
        commit_sha: str | None = None,
        tree_sha: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": "review-receipt/v1",
            "dispatch_id": dispatch_id,
            "reviewer_session_id": session_id,
            "family": family,
            "model": model,
            "commit_sha": commit_sha or self.c_sha,
            "tree_sha": tree_sha or self.t_sha,
            "verdict": verdict,
            "findings": findings if findings is not None else [],
        }

    def _write_report(self, data: dict[str, Any]) -> tuple[Path, str]:
        p = self.control_dir / f"report_{secrets.token_hex(4)}.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        return p, h

    def test_valid_review_receipt_v1_parses_successfully(self) -> None:
        data = self._make_valid_report_data()
        ok, err, parsed = validate_review_receipt(data)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(parsed["schema"], "review-receipt/v1")

    def test_schema_rejects_extra_keys_at_top_level(self) -> None:
        # Extra unrecognized fields must fail closed
        data = self._make_valid_report_data()
        data["extra_field"] = "not_allowed"
        ok, err, _ = validate_review_receipt(data)
        self.assertFalse(ok)
        self.assertEqual(err, "SCHEMA_EXTRA_KEYS_FORBIDDEN")

    def test_schema_rejects_legacy_session_aliases(self) -> None:
        # The closed contract forbids 'session_id', 'sessionId', 'conversation', etc.
        for alias in ("session_id", "sessionId", "conversation", "conv_id", "thread_id"):
            data = self._make_valid_report_data()
            data[alias] = "caller"
            ok, err, _ = validate_review_receipt(data)
            self.assertFalse(ok, f"Expected {alias} to be refused as extra key")
            self.assertEqual(err, "SCHEMA_EXTRA_KEYS_FORBIDDEN")

    def test_schema_rejects_extra_keys_in_findings(self) -> None:
        data = self._make_valid_report_data(
            verdict="REVISE",
            findings=[{"id": "F-01", "severity": "P1", "file": "a.py", "summary": "bug", "extra": "forbidden"}],
        )
        ok, err, _ = validate_review_receipt(data)
        self.assertFalse(ok)
        self.assertEqual(err, "FINDING_SCHEMA_VIOLATION")

    def test_schema_accepts_valid_closed_findings(self) -> None:
        data = self._make_valid_report_data(
            verdict="REVISE",
            findings=[{"id": "F-01", "severity": "P1", "file": "a.py", "summary": "bug"}],
        )
        ok, err, _ = validate_review_receipt(data)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_schema_rejects_unapproved_model_for_family(self) -> None:
        data = self._make_valid_report_data(family="opus", model="gemini-4.0-argon-high")
        ok, err, _ = validate_review_receipt(data)
        self.assertFalse(ok)
        self.assertEqual(err, "UNAPPROVED_REVIEWER_MODEL")

    def test_record_dispatch_stores_in_coordinator_ledger(self) -> None:
        res = record_dispatch(
            self.control_dir,
            self.work_id,
            "disp-test-1",
            "opus",
            "sess-opus-1",
        )
        self.assertEqual(res["status"], "dispatched")
        doc = load_coordinator_status(self.control_dir)
        ledger = doc["delivery_control"]["dispatch_ledger"]
        self.assertIn("disp-test-1", ledger)
        self.assertEqual(ledger["disp-test-1"]["family"], "opus")
        self.assertEqual(ledger["disp-test-1"]["reviewer_session_id"], "sess-opus-1")

    def test_record_review_refuses_unbound_dispatch_id(self) -> None:
        data = self._make_valid_report_data(dispatch_id="unregistered-disp")
        rep_p, rep_h = self._write_report(data)
        res = record_review(
            self.control_dir,
            self.work_id,
            "rev-opus",
            "claude-opus-5-5",
            "opus",
            "sess-opus",
            "SHIP",
            rep_p,
            rep_h,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "UNBOUND_DISPATCH_ID")

    def test_record_review_refuses_mismatched_dispatch_family_or_session(self) -> None:
        record_dispatch(self.control_dir, self.work_id, "disp-bound-1", "opus", "sess-opus-1")
        # Try to use disp-bound-1 with family 'argon'
        data_bad_fam = self._make_valid_report_data(
            family="argon",
            model="gemini-4.0-argon-high",
            session_id="sess-opus-1",
            dispatch_id="disp-bound-1",
        )
        p, h = self._write_report(data_bad_fam)
        res = record_review(
            self.control_dir,
            self.work_id,
            "rev-argon",
            "gemini-4.0-argon-high",
            "argon",
            "sess-opus-1",
            "SHIP",
            p,
            h,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "DISPATCH_FAMILY_MISMATCH")

    def test_record_review_refuses_self_review(self) -> None:
        # Implementer session is sess-implementer
        record_dispatch(self.control_dir, self.work_id, "disp-self", "opus", "sess-implementer")
        data = self._make_valid_report_data(session_id="sess-implementer", dispatch_id="disp-self")
        p, h = self._write_report(data)
        res = record_review(
            self.control_dir,
            self.work_id,
            "rev-opus",
            "claude-opus-5-5",
            "opus",
            "sess-implementer",
            "SHIP",
            p,
            h,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "SELF_REVIEW_FORBIDDEN")

    def test_record_review_refuses_duplicate_family(self) -> None:
        record_dispatch(self.control_dir, self.work_id, "disp-opus-1", "opus", "sess-opus-1")
        record_dispatch(self.control_dir, self.work_id, "disp-opus-2", "opus", "sess-opus-2")
        data1 = self._make_valid_report_data(session_id="sess-opus-1", dispatch_id="disp-opus-1")
        p1, h1 = self._write_report(data1)
        res1 = record_review(
            self.control_dir, self.work_id, "rev-1", "claude-opus-5-5", "opus", "sess-opus-1", "SHIP", p1, h1
        )
        self.assertEqual(res1["status"], "recorded")

        data2 = self._make_valid_report_data(session_id="sess-opus-2", dispatch_id="disp-opus-2")
        p2, h2 = self._write_report(data2)
        res2 = record_review(
            self.control_dir, self.work_id, "rev-2", "claude-opus-5-5", "opus", "sess-opus-2", "SHIP", p2, h2
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "DUPLICATE_REVIEWER_FAMILY")

    def test_three_independent_dispatches_qualify_work_item(self) -> None:
        families = [
            ("argon", "gemini-4.0-argon-high", "sess-argon-1", "disp-argon-1"),
            ("opus", "claude-opus-5-5", "sess-opus-1", "disp-opus-1"),
            ("sol", "gpt-6.1-sol-high", "sess-sol-1", "disp-sol-1"),
        ]
        for fam, model, sess, disp in families:
            record_dispatch(self.control_dir, self.work_id, disp, fam, sess)
            data = self._make_valid_report_data(
                family=fam,
                model=model,
                session_id=sess,
                dispatch_id=disp,
                verdict="SHIP",
            )
            p, h = self._write_report(data)
            res = record_review(self.control_dir, self.work_id, f"rev-{fam}", model, fam, sess, "SHIP", p, h)
            self.assertEqual(res["status"], "recorded")

        doc = load_coordinator_status(self.control_dir)
        wi = doc["delivery_control"]["work_items"][self.work_id]
        self.assertTrue(wi.get("qualified"))
        self.assertEqual(wi.get("stage"), "merge_ready")

    # Negative reproductions for SOL61-001, WFM11-01, ARGON-01
    def test_negative_control_sol61_001_dict_wrapper_rejected(self) -> None:
        # Sol R11-001 wrapper cases must be rejected immediately at schema validation
        cases = [
            {"session_id": "caller", "conversation": {"id": "shared-conversation"}},
            {"session_id": "caller", "reviewerSessions": [{"id": "shared-conversation"}]},
            {"reviewerSessions": {"ids": [["shared-conversation"]]}},
            {"reviewerSessions": [{"ids": [["shared-conversation"]]}]},
            {"reviewerSessions": [{}]},
            {"session": {"id": "shared"}},
            {"reviewer": {"conversation": {"value": "shared"}}},
            {"conv_id": "shared"},
            {"thread_id": "shared"},
        ]
        for bad_wrapper in cases:
            report = self._make_valid_report_data()
            report.update(bad_wrapper)
            ok, err, _ = validate_review_receipt(report)
            self.assertFalse(ok, f"Expected {bad_wrapper} to be rejected")
            self.assertEqual(err, "SCHEMA_EXTRA_KEYS_FORBIDDEN")

    def test_record_review_revise_verdict_and_remediation(self) -> None:
        record_dispatch(self.control_dir, self.work_id, "disp-sol-rev", "sol", "sess-sol-rev")
        data = self._make_valid_report_data(
            family="sol",
            model="gpt-6.1-sol-high",
            session_id="sess-sol-rev",
            dispatch_id="disp-sol-rev",
            verdict="REVISE",
        )
        data["findings"] = [{"id": "FIND-01", "severity": "HIGH", "file": "code.py", "summary": "Bug found"}]
        p, h = self._write_report(data)
        res = record_review(
            self.control_dir, self.work_id, "rev-sol", "gpt-6.1-sol-high", "sol", "sess-sol-rev", "REVISE", p, h
        )
        self.assertEqual(res["status"], "recorded")
        self.assertEqual(res["stage"], "implementing")

        doc = load_coordinator_status(self.control_dir)
        wi = doc["delivery_control"]["work_items"][self.work_id]
        self.assertTrue(wi.get("remediation_required"))
        self.assertFalse(wi.get("qualified"))

    def test_record_review_rejections_and_dispatch_validation(self) -> None:
        # Invalid family in record_dispatch
        d_bad = record_dispatch(self.control_dir, self.work_id, "disp-bad", "invalid-fam", "sess-1")
        self.assertEqual(d_bad["reason_code"], "INVALID_FAMILY")

        # Missing params in record_dispatch
        d_bad2 = record_dispatch(self.control_dir, self.work_id, "", "opus", "sess-1")
        self.assertEqual(d_bad2["reason_code"], "INVALID_DISPATCH_PARAMETERS")

        # Artifact hash mismatch in record_review
        record_dispatch(self.control_dir, self.work_id, "disp-hash", "opus", "sess-hash")
        data = self._make_valid_report_data(session_id="sess-hash", dispatch_id="disp-hash")
        p, _ = self._write_report(data)
        r_hash = record_review(
            self.control_dir, self.work_id, "rev-hash", "claude-opus-5-5", "opus", "sess-hash", "SHIP", p, "bad" * 16
        )
        self.assertEqual(r_hash["reason_code"], "ARTIFACT_HASH_MISMATCH")

        # Revision mismatch in record_review
        data_rev = self._make_valid_report_data(session_id="sess-hash", dispatch_id="disp-hash", commit_sha="9" * 40)
        p_rev, h_rev = self._write_report(data_rev)
        r_rev = record_review(
            self.control_dir, self.work_id, "rev-hash", "claude-opus-5-5", "opus", "sess-hash", "SHIP", p_rev, h_rev
        )
        self.assertEqual(r_rev["reason_code"], "REVISION_MISMATCH")

    def test_receipt_validation_structural_and_value_errors(self) -> None:
        # Invalid verdict
        bad_verdict = self._make_valid_report_data(verdict="MAYBE")
        ok, err, _ = validate_review_receipt(bad_verdict)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_VERDICT")

        # Unsupported schema version
        bad_schema = self._make_valid_report_data()
        bad_schema["schema"] = "review-receipt/v2"
        ok, err, _ = validate_review_receipt(bad_schema)
        self.assertFalse(ok)
        self.assertEqual(err, "SCHEMA_VERSION_UNSUPPORTED")

        # Missing required key
        missing_key = self._make_valid_report_data()
        del missing_key["verdict"]
        ok, err, _ = validate_review_receipt(missing_key)
        self.assertFalse(ok)
        self.assertEqual(err, "SCHEMA_MISSING_REQUIRED_KEYS")

        # Findings not a list
        bad_findings = self._make_valid_report_data()
        bad_findings["findings"] = "not a list"
        ok, err, _ = validate_review_receipt(bad_findings)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_FINDINGS_CONTAINER")

        # Non-dict receipt
        ok_nd, err_nd, _ = validate_review_receipt(["not", "dict"])
        self.assertFalse(ok_nd)
        self.assertEqual(err_nd, "REPORT_INVALID")

        # Invalid dispatch_id (empty or non-str)
        bad_disp = self._make_valid_report_data(dispatch_id="")
        ok, err, _ = validate_review_receipt(bad_disp)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_DISPATCH_ID")

        # Invalid reviewer_session_id
        bad_sess = self._make_valid_report_data(session_id="")
        ok, err, _ = validate_review_receipt(bad_sess)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_REVIEWER_SESSION_ID")

        # Invalid family
        bad_fam = self._make_valid_report_data(family="unknown_family")
        ok, err, _ = validate_review_receipt(bad_fam)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_FAMILY")

        # Invalid revision sha (not 40 hex)
        bad_sha = self._make_valid_report_data(commit_sha="not40hex")
        ok, err, _ = validate_review_receipt(bad_sha)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_REVISION_SHA")

        # Finding item not dict
        bad_item1 = self._make_valid_report_data(findings=[123])  # type: ignore
        ok, err, _ = validate_review_receipt(bad_item1)
        self.assertFalse(ok)
        self.assertEqual(err, "FINDING_SCHEMA_VIOLATION")

        # Finding item missing required keys or non-string
        bad_item2 = self._make_valid_report_data(findings=[{"id": 1, "severity": "HIGH", "file": "f.py"}])  # type: ignore
        ok, err, _ = validate_review_receipt(bad_item2)
        self.assertFalse(ok)
        self.assertEqual(err, "FINDING_SCHEMA_VIOLATION")

    def test_verify_dispatch_binding_checks(self) -> None:
        from receipts import verify_dispatch_binding

        ledger = {
            "disp-1": {
                "dispatch_id": "disp-1",
                "work_id": self.work_id,
                "family": "opus",
                "reviewer_session_id": "sess-opus",
            },
            "disp-not-dict": "invalid",
        }

        # Unbound dispatch ID
        ok, err = verify_dispatch_binding({}, "disp-1", self.work_id, "opus", "sess-opus")
        self.assertFalse(ok)
        self.assertEqual(err, "UNBOUND_DISPATCH_ID")

        # Entry not dict
        ok, err = verify_dispatch_binding(ledger, "disp-not-dict", self.work_id, "opus", "sess-opus")
        self.assertFalse(ok)
        self.assertEqual(err, "UNBOUND_DISPATCH_ID")

        # Work ID mismatch
        ok, err = verify_dispatch_binding(ledger, "disp-1", "other-work-id", "opus", "sess-opus")
        self.assertFalse(ok)
        self.assertEqual(err, "DISPATCH_WORK_ID_MISMATCH")

        # Family mismatch
        ok, err = verify_dispatch_binding(ledger, "disp-1", self.work_id, "argon", "sess-opus")
        self.assertFalse(ok)
        self.assertEqual(err, "DISPATCH_FAMILY_MISMATCH")

        # Session mismatch
        ok, err = verify_dispatch_binding(ledger, "disp-1", self.work_id, "opus", "other-session")
        self.assertFalse(ok)
        self.assertEqual(err, "DISPATCH_SESSION_MISMATCH")

        # Valid binding
        ok, err = verify_dispatch_binding(ledger, "disp-1", self.work_id, "opus", "sess-opus")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_record_dispatch_role_and_uniqueness_guards(self) -> None:
        # Session reused across roles (implementer claim session is sess-implementer)
        res_reuse = record_dispatch(self.control_dir, self.work_id, "disp-dup-role", "opus", "sess-implementer")
        self.assertEqual(res_reuse.get("status"), "refused")
        self.assertEqual(res_reuse.get("reason_code"), "SESSION_REUSED_ACROSS_ROLES")

        # First dispatch succeeds
        res1 = record_dispatch(self.control_dir, self.work_id, "disp-unique-1", "opus", "sess-unique-1")
        self.assertEqual(res1.get("status"), "dispatched")

        # Duplicate dispatch_id
        res_dup_id = record_dispatch(self.control_dir, self.work_id, "disp-unique-1", "argon", "sess-unique-2")
        self.assertEqual(res_dup_id.get("status"), "refused")
        self.assertEqual(res_dup_id.get("reason_code"), "DUPLICATE_DISPATCH_ID")

        # Duplicate reviewer_session_id for the same work item
        res_dup_sess = record_dispatch(self.control_dir, self.work_id, "disp-unique-2", "argon", "sess-unique-1")
        self.assertEqual(res_dup_sess.get("status"), "refused")
        self.assertEqual(res_dup_sess.get("reason_code"), "DUPLICATE_REVIEWER_SESSION")

    def test_check_qualification_edge_cases(self) -> None:
        from receipts import check_qualification

        reviews = [
            {
                "family": "opus",
                "reviewer_session_id": "s1",
                "dispatch_id": "d1",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
            {
                "family": "argon",
                "reviewer_session_id": "s2",
                "dispatch_id": "d2",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
            {
                "family": "sol",
                "reviewer_session_id": "s3",
                "dispatch_id": "d3",
                "verdict": "SHIP",
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
            },
        ]
        # Without dispatch_ledger (dispatch_ledger=None)
        self.assertTrue(check_qualification(reviews, self.c_sha, self.t_sha, dispatch_ledger=None))

        # Ledger with non-dict entry
        bad_ledger1 = {"d1": "not-a-dict", "d2": {}, "d3": {}}
        self.assertFalse(check_qualification(reviews, self.c_sha, self.t_sha, dispatch_ledger=bad_ledger1))

        # Ledger with family/session mismatch
        bad_ledger2 = {
            "d1": {"family": "sol", "reviewer_session_id": "s1", "work_id": self.work_id},
            "d2": {"family": "argon", "reviewer_session_id": "s2", "work_id": self.work_id},
            "d3": {"family": "sol", "reviewer_session_id": "s3", "work_id": self.work_id},
        }
        self.assertFalse(check_qualification(reviews, self.c_sha, self.t_sha, dispatch_ledger=bad_ledger2))

        # Ledger with work_id mismatch
        bad_ledger3 = {
            "d1": {"family": "opus", "reviewer_session_id": "s1", "work_id": "other-work"},
            "d2": {"family": "argon", "reviewer_session_id": "s2", "work_id": self.work_id},
            "d3": {"family": "sol", "reviewer_session_id": "s3", "work_id": self.work_id},
        }
        self.assertFalse(
            check_qualification(reviews, self.c_sha, self.t_sha, dispatch_ledger=bad_ledger3, work_id=self.work_id)
        )

    def test_record_review_more_rejection_branches(self) -> None:
        # Report file does not exist
        res_nonexist = record_review(
            self.control_dir,
            self.work_id,
            "rev-1",
            "claude-opus-5-5",
            "opus",
            "sess-1",
            "SHIP",
            "/tmp/nonexistent-rep-file.json",
            "0" * 64,
        )
        self.assertEqual(res_nonexist.get("reason_code"), "ARTIFACT_NOT_FOUND")

        # Report file is invalid JSON
        bad_json_file = self.control_dir / "bad_report.json"
        bad_json_file.write_text("{not valid json", encoding="utf-8")
        h_bad = hashlib.sha256(bad_json_file.read_bytes()).hexdigest()
        res_bad_json = record_review(
            self.control_dir,
            self.work_id,
            "rev-1",
            "claude-opus-5-5",
            "opus",
            "sess-1",
            "SHIP",
            bad_json_file,
            h_bad,
        )
        self.assertEqual(res_bad_json.get("reason_code"), "REPORT_INVALID")

        # Report file is a list, not a dict
        bad_list_file = self.control_dir / "list_report.json"
        bad_list_file.write_text("[1, 2, 3]", encoding="utf-8")
        h_list = hashlib.sha256(bad_list_file.read_bytes()).hexdigest()
        res_bad_list = record_review(
            self.control_dir,
            self.work_id,
            "rev-1",
            "claude-opus-5-5",
            "opus",
            "sess-1",
            "SHIP",
            bad_list_file,
            h_list,
        )
        self.assertEqual(res_bad_list.get("reason_code"), "REPORT_INVALID")

        # Report identity mismatch (model or family)
        record_dispatch(self.control_dir, self.work_id, "disp-mismatch", "opus", "sess-mismatch")
        data = self._make_valid_report_data(session_id="sess-mismatch", dispatch_id="disp-mismatch")
        p, h = self._write_report(data)

        # Call with different model
        res_id_mismatch = record_review(
            self.control_dir, self.work_id, "rev-1", "different-model", "opus", "sess-mismatch", "SHIP", p, h
        )
        self.assertEqual(res_id_mismatch.get("reason_code"), "REPORT_IDENTITY_MISMATCH")

        # Call with different verdict
        res_ver_mismatch = record_review(
            self.control_dir, self.work_id, "rev-1", "claude-opus-5-5", "opus", "sess-mismatch", "REVISE", p, h
        )
        self.assertEqual(res_ver_mismatch.get("reason_code"), "REPORT_VERDICT_MISMATCH")

        # Call with different session
        res_sess_mismatch = record_review(
            self.control_dir, self.work_id, "rev-1", "claude-opus-5-5", "opus", "other-sess", "SHIP", p, h
        )
        self.assertEqual(res_sess_mismatch.get("reason_code"), "REVIEWER_SESSION_MISMATCH")

        # Work not admitted
        res_no_work = record_review(
            self.control_dir, "unadmitted-work-id", "rev-1", "claude-opus-5-5", "opus", "sess-mismatch", "SHIP", p, h
        )
        self.assertEqual(res_no_work.get("reason_code"), "WORK_NOT_ADMITTED")

        # Self review forbidden (reviewer_id == claim owner or model contains "flash")
        res_self_owner = record_review(
            self.control_dir, self.work_id, "selamy-labs-bot", "claude-opus-5-5", "opus", "sess-mismatch", "SHIP", p, h
        )
        self.assertEqual(res_self_owner.get("reason_code"), "SELF_REVIEW_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
