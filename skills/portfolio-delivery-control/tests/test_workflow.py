"""Comprehensive unit and end-to-end vertical-slice tests for workflow.py."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

PREFLIGHT_PATH = Path(__file__).parents[1] / "scripts" / "preflight.py"
PREFLIGHT_SPEC = importlib.util.spec_from_file_location("preflight", PREFLIGHT_PATH)
preflight = importlib.util.module_from_spec(PREFLIGHT_SPEC)
sys.modules["preflight"] = preflight
PREFLIGHT_SPEC.loader.exec_module(preflight)

WORKFLOW_PATH = Path(__file__).parents[1] / "scripts" / "workflow.py"
WORKFLOW_SPEC = importlib.util.spec_from_file_location(
    "skills/portfolio-delivery-control/scripts/workflow", WORKFLOW_PATH
)
workflow = importlib.util.module_from_spec(WORKFLOW_SPEC)
sys.modules["workflow"] = workflow
sys.modules["skills/portfolio-delivery-control/scripts/workflow"] = workflow
WORKFLOW_SPEC.loader.exec_module(workflow)

UTC = datetime.UTC

FAM_CANONICAL_MODELS = {
    "argon": "gemini-4.0-argon-high",
    "opus": "claude-opus-5-5",
    "sol": "gpt-6.1-sol-high",
}


def _init_git_repo(repo_dir: Path) -> str:
    """Initialize a git repo with an initial commit signed as selamy-labs-bot."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "selamy-labs-bot"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "346154065+selamy-labs-bot@users.noreply.github.com"],
        cwd=repo_dir,
        check=True,
    )
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo_dir, check=True)
    init_file = repo_dir / "README.md"
    init_file.write_text("# Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    return head_sha


def _create_commit(repo_dir: Path, filename: str, content: str, msg: str) -> tuple[str, str, str]:
    f = repo_dir / filename
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo_dir, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD^"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    return head, tree, parent


class TestWorkflowAdmit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.spec_file = self.root / "tasks.md"
        self.spec_file.write_text("- [ ] TASK-100: implement feature\n", encoding="utf-8")
        self.directive_file = self.root / "directive.md"
        self.directive_file.write_text("Primary directive content\n", encoding="utf-8")
        self.directive_sha = hashlib.sha256(self.directive_file.read_bytes()).hexdigest()
        self.base_sha = "a" * 40

    def tearDown(self):
        self.tmp.cleanup()

    def test_admit_success(self):
        res = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-100",
            project="test-proj",
            repository="owner/repo",
            task_id="TASK-100",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{self.directive_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res["status"], "admitted")
        self.assertEqual(res["work_id"], "W-100")
        self.assertTrue(Path(res["receipt_path"]).exists())

        doc = workflow.load_coordinator_status(self.control_dir)
        self.assertIn("W-100", doc["delivery_control"]["work_items"])
        self.assertIn("test-proj", doc["delivery_control"]["projects"])
        p_row = doc["delivery_control"]["projects"]["test-proj"]
        self.assertEqual(p_row["stage"], "ready")
        self.assertEqual(p_row["approval"]["status"], "approved")

    def test_refuse_invalid_base_sha(self):
        res = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-101",
            project="test-proj",
            repository="owner/repo",
            task_id="TASK-100",
            base_sha="short_sha",
            approval_ref=f"{self.directive_file}:{self.directive_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "INVALID_BASE_SHA")
        self.assertTrue(Path(res["receipt_path"]).exists())

    def test_refuse_absent_approval(self):
        res = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-102",
            project="test-proj",
            repository="owner/repo",
            task_id="TASK-100",
            base_sha=self.base_sha,
            approval_ref="",
            spec_path=self.spec_file,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "ABSENT_APPROVAL")

    def test_refuse_missing_spec_task(self):
        res = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-103",
            project="test-proj",
            repository="owner/repo",
            task_id="NONEXISTENT-TASK",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{self.directive_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "MISSING_SPEC_TASK")

    def test_refuse_duplicate_work_id(self):
        res1 = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-DUP",
            project="test-proj",
            repository="owner/repo",
            task_id="TASK-100",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{self.directive_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res1["status"], "admitted")

        res2 = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-DUP",
            project="test-proj",
            repository="owner/repo",
            task_id="TASK-100",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{self.directive_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "DUPLICATE_WORK_ID")


class TestWorkflowClaim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)
        self.base_sha = "b" * 40
        self.dir_file = self.root / "directive.md"
        self.dir_file.write_text("# Directive\nApproval granted.\n", encoding="utf-8")
        self.dir_sha = hashlib.sha256(self.dir_file.read_bytes()).hexdigest()
        self.approval = {"path": str(self.dir_file), "sha256": self.dir_sha}

        workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-PREREQ",
            project="proj-1",
            repository="owner/repo",
            task_id="T-0",
            base_sha=self.base_sha,
            approval_ref=self.approval,
            verify_spec=False,
        )

        workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-MAIN",
            project="proj-2",
            repository="owner/repo",
            task_id="T-1",
            base_sha=self.base_sha,
            approval_ref=self.approval,
            prerequisites=["W-PREREQ"],
            verify_spec=False,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuse_scope_escape(self):
        res = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-PREREQ",
            owner="lane-1",
            worktree="/var/secret",
            write_paths=["foo/"],
            allowed_roots=[str(self.root)],
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "WORKSPACE_OUTSIDE_ALLOWED_ROOT")

    def test_refuse_unmet_prerequisites(self):
        res = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-MAIN",
            owner="lane-1",
            worktree=self.worktree,
            write_paths=["src/"],
            allowed_roots=[str(self.root)],
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "UNMET_PREREQUISITES")

    def test_claim_success_and_conflicts(self):
        res1 = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-PREREQ",
            owner="lane-1",
            worktree=self.worktree,
            write_paths=["src/prereq/"],
            allowed_roots=[str(self.root)],
            session_id="sess-lane-1",
        )
        self.assertEqual(res1["status"], "claimed")
        self.assertEqual(res1["session_id"], "sess-lane-1")

        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-PREREQ"]["stage"] = "done"
        workflow.save_coordinator_status(self.control_dir, doc)

        res_sess_conflict = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-MAIN",
            owner="different-owner",
            worktree=self.worktree,
            write_paths=["src/main/"],
            allowed_roots=[str(self.root)],
            session_id="sess-lane-1",
        )
        self.assertEqual(res_sess_conflict["status"], "refused")
        self.assertEqual(res_sess_conflict["reason_code"], "WRITER_CONFLICT")

        res_path_conflict = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-MAIN",
            owner="lane-3",
            worktree=self.worktree,
            write_paths=["src/prereq/sub/"],
            allowed_roots=[str(self.root)],
            session_id="sess-lane-3",
        )
        self.assertEqual(res_path_conflict["status"], "refused")
        self.assertEqual(res_path_conflict["reason_code"], "WRITER_CONFLICT")

        res3 = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-MAIN",
            owner="lane-2",
            worktree=self.worktree,
            write_paths=["src/main/"],
            allowed_roots=[str(self.root)],
            session_id="sess-lane-2",
        )
        self.assertEqual(res3["status"], "claimed")

        res4 = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-MAIN",
            owner="lane-4",
            worktree=self.worktree,
            write_paths=["src/main/"],
            allowed_roots=[str(self.root)],
        )
        self.assertEqual(res4["status"], "refused")
        self.assertEqual(res4["reason_code"], "COMPETING_ACTIVE_CLAIM")


class TestWorkflowRecordImplementation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)

        self.base_sha = _init_git_repo(self.worktree)
        self.dir_file = self.root / "dir.md"
        self.dir_file.write_text("# Directive\nApproval granted.\n", encoding="utf-8")
        self.dir_sha = hashlib.sha256(self.dir_file.read_bytes()).hexdigest()
        self.approval = {"path": str(self.dir_file), "sha256": self.dir_sha}

        workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-IMP",
            project="proj-imp",
            repository="owner/repo",
            task_id="T-IMP",
            base_sha=self.base_sha,
            approval_ref=self.approval,
            verify_spec=False,
        )

        claim_res = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            worktree=self.worktree,
            write_paths=["file.py"],
            allowed_roots=[str(self.root)],
        )
        self.claim_token = claim_res["claim_token"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_impl_refuse_missing_mandatory_gate(self):
        res = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            claim_token=self.claim_token,
            round=1,
            commit_sha="c" * 40,
            tree_sha="t" * 40,
            parent_sha=self.base_sha,
            gate_results={"test_exit_code": 1},
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "MISSING_MANDATORY_GATE")

    def test_record_impl_refuse_artifact_hash_mismatch(self):
        dummy_patch = self.root / "patch.patch"
        dummy_patch.write_text("patch content", encoding="utf-8")
        res = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            claim_token=self.claim_token,
            round=1,
            commit_sha="c" * 40,
            tree_sha="t" * 40,
            parent_sha=self.base_sha,
            gate_results={"test_exit_code": 0},
            patch_path=dummy_patch,
            patch_sha256="0" * 64,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "ARTIFACT_HASH_MISMATCH")

    def test_record_impl_refuse_git_mismatches(self):
        c1, t1, p1 = _create_commit(self.worktree, "file.py", "print('hello')\n", "feat: round 1")

        res1 = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            claim_token=self.claim_token,
            round=1,
            commit_sha="f" * 40,
            tree_sha=t1,
            parent_sha=p1,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res1["status"], "refused")
        self.assertEqual(res1["reason_code"], "HEAD_MISMATCH")

        res2 = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            claim_token=self.claim_token,
            round=1,
            commit_sha=c1,
            tree_sha="e" * 40,
            parent_sha=p1,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "TREE_MISMATCH")

    def test_record_impl_refuse_scope_escape_and_protected_path(self):
        c_escape, t_escape, p_escape = _create_commit(
            self.worktree, "escape/bad.py", "malicious\n", "feat: scope escape"
        )
        res_esc = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            claim_token=self.claim_token,
            round=1,
            commit_sha=c_escape,
            tree_sha=t_escape,
            parent_sha=p_escape,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res_esc["status"], "refused")
        self.assertEqual(res_esc["reason_code"], "SCOPE_ESCAPE")

        subprocess.run(["git", "reset", "--hard", self.base_sha], cwd=self.worktree, check=True)
        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-IMP"]["claim"]["write_paths"] = ["skills/portfolio-delivery-control/"]
        doc["delivery_control"]["work_items"]["W-IMP"]["claim"]["allowed_rel_paths"] = [
            "skills/portfolio-delivery-control/"
        ]
        workflow.save_coordinator_status(self.control_dir, doc)

        c_prot, t_prot, p_prot = _create_commit(
            self.worktree, "skills/portfolio-delivery-control/foo.py", "x = 1\n", "feat: modify skill"
        )
        res_prot = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            claim_token=self.claim_token,
            round=1,
            commit_sha=c_prot,
            tree_sha=t_prot,
            parent_sha=p_prot,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res_prot["status"], "refused")
        self.assertEqual(res_prot["reason_code"], "PROTECTED_PATH_MODIFIED")

    def test_record_impl_success_and_invalidation(self):
        c1, t1, p1 = _create_commit(self.worktree, "file.py", "x = 1\n", "feat: implementation R1")
        res1 = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            claim_token=self.claim_token,
            round=1,
            commit_sha=c1,
            tree_sha=t1,
            parent_sha=p1,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res1["status"], "recorded")
        self.assertEqual(res1["stage"], "review")

        doc = workflow.load_coordinator_status(self.control_dir)
        wi = doc["delivery_control"]["work_items"]["W-IMP"]
        wi["reviews"] = [{"reviewer_id": "r1", "verdict": "SHIP", "commit_sha": c1, "tree_sha": t1}]
        wi["qualified"] = True
        workflow.save_coordinator_status(self.control_dir, doc)

        subprocess.run(["git", "reset", "--hard", self.base_sha], cwd=self.worktree, check=True)
        c2, t2, p2 = _create_commit(self.worktree, "file.py", "x = 2\n", "feat: implementation R2")

        res2 = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id="W-IMP",
            owner="owner-imp",
            claim_token=self.claim_token,
            round=2,
            commit_sha=c2,
            tree_sha=t2,
            parent_sha=p2,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res2["status"], "recorded")

        doc2 = workflow.load_coordinator_status(self.control_dir)
        wi2 = doc2["delivery_control"]["work_items"]["W-IMP"]
        self.assertEqual(wi2["reviews"], [])
        self.assertFalse(wi2["qualified"])
        self.assertEqual(wi2["invalidated_reviews_from_sha"], c1)

    def _make_ident_runner(self, ident_str: str):
        def _runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "--format=%an <%ae>|%cn <%ce>" in cmd_str:
                return 0, ident_str
            if "rev-parse" in cmd_str:
                if "HEAD^1" in cmd_str or "HEAD^ " in cmd_str or cmd_str.endswith("HEAD^"):
                    return 0, "p" * 40
                return 0, "a" * 40
            if "rev-list" in cmd_str:
                if f"HEAD..{'p' * 40}" in cmd_str:
                    return 0, "0"
                return 0, "1"
            if "merge-base" in cmd_str:
                return 0, ""
            return 0, ""

        return _runner

    def test_record_impl_identities(self):
        selamy_bot = "selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>"
        approved = [
            f"{selamy_bot}|{selamy_bot}",
            "claude-cir <claude-cir@selamy.dev>|claude-cir <claude-cir@selamy.dev>",
            f"claude-cir <claude-cir@selamy.dev>|{selamy_bot}",
        ]
        for ident in approved:
            ok, err = workflow._inspect_git_commit(
                self.worktree, "a" * 40, "a" * 40, "p" * 40, "p" * 40, runner=self._make_ident_runner(ident)
            )
            self.assertTrue(ok, f"Should accept {ident}, got error {err}")

        unapproved = [
            "dev <dev@selamy.dev>|dev <dev@selamy.dev>",
            f"Human Dev <human@example.com>|{selamy_bot}",
            f"{selamy_bot}|GitHub <noreply@github.com>",
            "Human Dev <human@example.com>|Human Dev <human@example.com>",
            "unknown-bot|unknown-bot",
            "Human <evil+selamy-labs-bot@example.com>|Human <evil+selamy-labs-bot@example.com>",
            "malformed_string_without_pipe",
        ]
        for ident in unapproved:
            ok, err = workflow._inspect_git_commit(
                self.worktree, "a" * 40, "a" * 40, "p" * 40, "p" * 40, runner=self._make_ident_runner(ident)
            )
            self.assertFalse(ok, f"Should reject {ident}")
            self.assertEqual(err, "INVALID_COMMIT_IDENTITY")


class TestWorkflowRecordReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.c_sha = "c" * 40
        self.t_sha = "b" * 40

        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-REV"] = {
            "work_id": "W-REV",
            "project": "proj-rev",
            "candidate": {
                "round": 1,
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
                "implementer_model": "gemini-3.8-flash-high",
            },
            "claim": {
                "owner": "impl-owner",
                "session_id": "impl-session",
                "model": "gemini-3.8-flash-high",
            },
            "reviews": [],
        }
        workflow.save_coordinator_status(self.control_dir, doc)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_report(self, data: dict) -> tuple[Path, str]:
        p = self.root / f"rep_{secrets.token_hex(4)}.json"
        rep_data = dict(data)
        if (
            "dispatch" not in rep_data
            and "session_id" not in rep_data
            and "attestation" not in rep_data
            and "sha256" not in rep_data
            and not rep_data.get("no_prov")
        ):
            rep_data["dispatch"] = f"disp-{secrets.token_hex(4)}"
        rep_data.pop("no_prov", None)
        p.write_text(json.dumps(rep_data), encoding="utf-8")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        return p, h

    def test_refuse_forbidden_model(self):
        p, h = self._write_report({"commit_sha": self.c_sha, "tree_sha": self.t_sha, "verdict": "SHIP"})
        res = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="rev-1",
            model="claude-fable-5-1",
            family="argon",
            session_id="s-1",
            verdict="SHIP",
            report_path=p,
            report_sha256=h,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "FORBIDDEN_MODEL")

    def test_refuse_report_verdict_mismatch(self):
        p1, h1 = self._write_report(
            {"commit_sha": self.c_sha, "tree_sha": self.t_sha, "model": "gemini-4.0-argon-high"}
        )
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="rev-argon",
            model="gemini-4.0-argon-high",
            family="argon",
            session_id="s-1",
            verdict="SHIP",
            report_path=p1,
            report_sha256=h1,
        )
        self.assertEqual(res1["status"], "refused")
        self.assertEqual(res1["reason_code"], "REPORT_VERDICT_MISMATCH")

        p2, h2 = self._write_report(
            {"commit_sha": self.c_sha, "tree_sha": self.t_sha, "verdict": "REVISE", "model": "gemini-4.0-argon-high"}
        )
        res2 = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="rev-argon",
            model="gemini-4.0-argon-high",
            family="argon",
            session_id="s-1",
            verdict="SHIP",
            report_path=p2,
            report_sha256=h2,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "REPORT_VERDICT_MISMATCH")

    def test_refuse_report_identity_mismatch(self):
        p1, h1 = self._write_report(
            {"commit_sha": self.c_sha, "tree_sha": self.t_sha, "verdict": "SHIP", "no_prov": True}
        )
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="rev-1",
            model="claude-opus-5-5",
            family="opus",
            session_id="s-1",
            verdict="SHIP",
            report_path=p1,
            report_sha256=h1,
        )
        self.assertEqual(res1["status"], "refused")
        self.assertIn(res1["reason_code"], ("REPORT_IDENTITY_MISMATCH", "REVIEWER_SESSION_MISMATCH"))

        p2, h2 = self._write_report(
            {"commit_sha": self.c_sha, "tree_sha": self.t_sha, "verdict": "SHIP", "model": "gemini-4.0-argon-high"}
        )
        res2 = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="rev-1",
            model="claude-opus-5-5",
            family="opus",
            session_id="s-1",
            verdict="SHIP",
            report_path=p2,
            report_sha256=h2,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "REPORT_IDENTITY_MISMATCH")

    def test_refuse_self_review(self):
        p, h = self._write_report(
            {"commit_sha": self.c_sha, "tree_sha": self.t_sha, "verdict": "SHIP", "model": "claude-opus-5-5"}
        )

        res_owner = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="impl-owner",
            model="claude-opus-5-5",
            family="opus",
            session_id="s-unique-1",
            verdict="SHIP",
            report_path=p,
            report_sha256=h,
        )
        self.assertEqual(res_owner["status"], "refused")
        self.assertEqual(res_owner["reason_code"], "SELF_REVIEW_FORBIDDEN")

        res_sess = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="other-rev",
            model="claude-opus-5-5",
            family="opus",
            session_id="impl-session",
            verdict="SHIP",
            report_path=p,
            report_sha256=h,
        )
        self.assertEqual(res_sess["status"], "refused")
        self.assertEqual(res_sess["reason_code"], "SELF_REVIEW_FORBIDDEN")

        res_sess_owner = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="other-rev",
            model="claude-opus-5-5",
            family="opus",
            session_id="impl-owner",
            verdict="SHIP",
            report_path=p,
            report_sha256=h,
        )
        self.assertEqual(res_sess_owner["status"], "refused")
        self.assertEqual(res_sess_owner["reason_code"], "SELF_REVIEW_FORBIDDEN")

        p_flash, h_flash = self._write_report(
            {"commit_sha": self.c_sha, "tree_sha": self.t_sha, "verdict": "SHIP", "model": "gemini-3.8-flash-argon"}
        )
        res_flash = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="other-rev",
            model="gemini-3.8-flash-argon",
            family="argon",
            session_id="s-unique-2",
            verdict="SHIP",
            report_path=p_flash,
            report_sha256=h_flash,
        )
        self.assertEqual(res_flash["status"], "refused")
        self.assertEqual(res_flash["reason_code"], "SELF_REVIEW_FORBIDDEN")

    def test_refuse_revision_mismatch(self):
        p, h = self._write_report(
            {"commit_sha": "d" * 40, "tree_sha": self.t_sha, "verdict": "SHIP", "model": "gemini-4-argon-high"}
        )
        res = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="rev-argon",
            model="gemini-4-argon-high",
            family="argon",
            session_id="s-argon",
            verdict="SHIP",
            report_path=p,
            report_sha256=h,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "REVISION_MISMATCH")

    def test_revise_verdict_and_qualification(self):
        p1, h1 = self._write_report(
            {
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
                "defects": ["DEF-01"],
                "verdict": "REVISE",
                "model": "gpt-6.1-sol-high",
            }
        )
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="sol-rev",
            model="gpt-6.1-sol-high",
            family="sol",
            session_id="sess-sol",
            verdict="REVISE",
            report_path=p1,
            report_sha256=h1,
        )
        self.assertEqual(res1["status"], "recorded")
        self.assertEqual(res1["verdict"], "REVISE")
        self.assertTrue(res1["remediation_required"])
        self.assertEqual(res1["defects"], ["DEF-01"])

        p2, h2 = self._write_report(
            {
                "commit_sha": self.c_sha,
                "tree_sha": self.t_sha,
                "verdict": "SHIP",
                "model": "gpt-6.1-sol-high",
            }
        )
        res_dup = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-REV",
            reviewer_id="sol-rev-2",
            model="gpt-6.1-sol-high",
            family="sol",
            session_id="sess-sol-2",
            verdict="SHIP",
            report_path=p2,
            report_sha256=h2,
        )
        self.assertEqual(res_dup["status"], "refused")
        self.assertEqual(res_dup["reason_code"], "DUPLICATE_REVIEWER_FAMILY")

        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-REV"]["reviews"] = []
        doc["delivery_control"]["work_items"]["W-REV"]["defects"] = []
        doc["delivery_control"]["work_items"]["W-REV"]["remediation_required"] = False
        workflow.save_coordinator_status(self.control_dir, doc)

        for fam in ("argon", "opus", "sol"):
            model = FAM_CANONICAL_MODELS[fam]
            rep, rep_h = self._write_report(
                {"commit_sha": self.c_sha, "tree_sha": self.t_sha, "verdict": "SHIP", "model": model}
            )
            res_ship = workflow.record_review(
                control_dir=self.control_dir,
                work_id="W-REV",
                reviewer_id=f"rev-{fam}",
                model=model,
                family=fam,
                session_id=f"sess-{fam}",
                verdict="SHIP",
                report_path=rep,
                report_sha256=rep_h,
            )
            self.assertEqual(res_ship["status"], "recorded")
            self.assertEqual(res_ship["verdict"], "SHIP")

        doc_final = workflow.load_coordinator_status(self.control_dir)
        wi_final = doc_final["delivery_control"]["work_items"]["W-REV"]
        self.assertTrue(wi_final["qualified"])
        self.assertEqual(wi_final["stage"], "merge_ready")
        p_row = doc_final["delivery_control"]["projects"]["proj-rev"]
        self.assertEqual(len(p_row["reviews"]), 3)


class TestWorkflowExecuteTransition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)

        self.commit_sha = _init_git_repo(self.worktree)
        self.tree_sha = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=self.worktree, check=True, capture_output=True, text=True
        ).stdout.strip()

        self.spec_file = self.root / "tasks.md"
        self.spec_file.write_text("- [ ] TASK-10 other\n- [ ] TASK-TRANS\n", encoding="utf-8")

        self.directive_file = self.root / "directive.md"
        self.directive_file.write_text("# Directive\nApproval granted for transition.\n", encoding="utf-8")
        self.directive_sha = hashlib.sha256(self.directive_file.read_bytes()).hexdigest()

        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-TRANS"] = {
            "work_id": "W-TRANS",
            "project": "proj-trans",
            "task_id": "TASK-TRANS",
            "spec_path": str(self.spec_file),
            "approval_ref": f"{self.directive_file}:{self.directive_sha}",
            "required_completion_action": "push",
            "base_sha": self.commit_sha,
            "claim": {
                "owner": "owner-1",
                "session_id": "session-1",
                "claim_token": "claim-tok-1",
                "worktree": str(self.worktree),
                "write_paths": ["src/"],
                "allowed_rel_paths": ["src/", "tasks.md", "drift.txt", "file.txt"],
                "expires_at": (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).isoformat(),
            },
            "candidate": {
                "commit_sha": self.commit_sha,
                "tree_sha": self.tree_sha,
                "gate_results": {"test_exit_code": 0},
            },
            "reviews": [
                {
                    "family": "argon",
                    "verdict": "SHIP",
                    "commit_sha": self.commit_sha,
                    "tree_sha": self.tree_sha,
                    "session_id": "s-argon",
                },
                {
                    "family": "opus",
                    "verdict": "SHIP",
                    "commit_sha": self.commit_sha,
                    "tree_sha": self.tree_sha,
                    "session_id": "s-opus",
                },
                {
                    "family": "sol",
                    "verdict": "SHIP",
                    "commit_sha": self.commit_sha,
                    "tree_sha": self.tree_sha,
                    "session_id": "s-sol",
                },
            ],
            "qualified": True,
            "stage": "merge_ready",
        }
        workflow.save_coordinator_status(self.control_dir, doc)

        receipts_dir = self.control_dir / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        for rev in doc["delivery_control"]["work_items"]["W-TRANS"]["reviews"]:
            fam = rev["family"]
            sess = rev["session_id"]
            rec_file = receipts_dir / f"review_W-TRANS_{fam}_{sess}.json"
            rec_data = {
                "work_id": "W-TRANS",
                "family": fam,
                "reviewer_family": fam,
                "session_id": sess,
                "reviewer_session_id": sess,
                "verdict": "SHIP",
                "commit_sha": self.commit_sha,
                "tree_sha": self.tree_sha,
                "revision": self.commit_sha,
                "head": self.commit_sha,
                "reviewer_model": "model-1",
                "report_path": "/path/to/report",
            }
            rec_file.write_text(json.dumps(rec_data), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuse_missing_or_expired_claim_token(self):
        res1 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="push",
            claim_token=None,
        )
        self.assertEqual(res1["status"], "refused")
        self.assertEqual(res1["reason_code"], "MISSING_OR_EXPIRED_CLAIM")

        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-TRANS"]["claim"]["expires_at"] = (
            datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
        ).isoformat()
        workflow.save_coordinator_status(self.control_dir, doc)

        res2 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="push",
            claim_token="claim-tok-1",
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "CLAIM_EXPIRED")

    def test_refuse_missing_qualification(self):
        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-TRANS"]["qualified"] = False
        workflow.save_coordinator_status(self.control_dir, doc)

        for act in ("push", "publish_pr", "merge_pr", "complete_spec_task"):
            res = workflow.execute_transition(
                control_dir=self.control_dir,
                work_id="W-TRANS",
                action=act,
                claim_token="claim-tok-1",
            )
            self.assertEqual(res["status"], "refused")
            self.assertEqual(res["reason_code"], "MISSING_QUALIFICATION")

    def test_refuse_stale_review(self):
        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-TRANS"]["reviews"][0]["commit_sha"] = "0" * 40
        workflow.save_coordinator_status(self.control_dir, doc)

        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="push",
            claim_token="claim-tok-1",
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "STALE_REVIEW_REVISION")

    def test_refuse_protected_branch_push(self):
        for br in ("main", "master"):
            res = workflow.execute_transition(
                control_dir=self.control_dir,
                work_id="W-TRANS",
                action="push",
                branch=br,
                claim_token="claim-tok-1",
            )
            self.assertEqual(res["status"], "refused")
            self.assertEqual(res["reason_code"], "PROTECTED_BRANCH_PUSH")

    def test_refuse_disallowed_merge_identity(self):
        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="merge_pr",
            claim_token="claim-tok-1",
            merge_identity="unapproved-user",
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "DISALLOWED_MERGE_IDENTITY")

    def test_refuse_missing_transition_evidence_for_complete_spec_task(self):
        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="complete_spec_task",
            claim_token="claim-tok-1",
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "MISSING_TRANSITION_EVIDENCE")

    def test_refuse_head_changed(self):
        _create_commit(self.worktree, "drift.txt", "drift\n", "drift commit")
        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="push",
            claim_token="claim-tok-1",
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "HEAD_CHANGED")

    def test_refuse_merge_missing_ci(self):
        def runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "OPEN",
                        "mergeCommit": None,
                        "headRefOid": self.commit_sha,
                        "number": 1,
                        "statusCheckRollup": [],
                    }
                )
            return 0, ""

        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="merge_pr",
            claim_token="claim-tok-1",
            ci_checks=[{"name": "test", "status": "FAILURE"}],
            merge_identity="selamy-labs-bot",
            command_runner=runner,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "MISSING_EXACT_HEAD_CI")

    def _mock_reconciliation_gh(self, cmd_str: str, cmd: list[str]) -> tuple[int, str]:
        if "api" in cmd and "user" in cmd:
            return 0, json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})
        if "pr view" in cmd_str:
            pr_info = {
                "state": "MERGED",
                "mergedAt": "2026-11-03T10:00:00Z",
                "mergeCommit": {"oid": "e" * 40},
                "headRefOid": self.commit_sha,
                "number": 42,
                "url": "https://github.com/pr/42",
                "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": self.commit_sha}],
            }
            return 0, json.dumps(pr_info)
        if "pr create" in cmd_str:
            return 0, "https://github.com/pr/42"
        if "pr merge" in cmd_str:
            return 0, "Merged PR #42"
        return 0, ""

    def _mock_reconciliation_git(
        self, calls: list[list[str]], cmd_str: str, cmd: list[str], cwd: Any
    ) -> tuple[int, str]:
        if "rev-parse" in cmd_str:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            return p.returncode, p.stdout.strip()
        if "diff" in cmd_str:
            return 0, ""
        if "ls-remote" in cmd_str:
            if len([c for c in calls if "git ls-remote" in " ".join(c)]) == 1:
                return 0, ""
            return 0, f"{self.commit_sha} refs/heads/feat/W-TRANS"
        if "push" in cmd_str:
            return 0, "Everything up-to-date"
        return 0, ""

    def _reconciliation_mock_runner(self, calls, cmd, cwd=None):
        calls.append(cmd)
        cmd_str = " ".join(cmd)
        if cmd and cmd[0] == "gh":
            return self._mock_reconciliation_gh(cmd_str, cmd)
        return self._mock_reconciliation_git(calls, cmd_str, cmd, cwd)

    def test_transitions_with_provider_reconciliation(self):
        calls = []

        def mock_runner(cmd, cwd=None):
            return self._reconciliation_mock_runner(calls, cmd, cwd)

        # 1. Push non-force (branch doesn't exist remotely yet)
        p_res1 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="push",
            claim_token="claim-tok-1",
            command_runner=mock_runner,
        )
        self.assertEqual(p_res1["status"], "transitioned")
        self.assertFalse(p_res1["reconciled_existing_effect"])

        # Push retry (reconciliation: remote exists at commit)
        p_res2 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="push",
            claim_token="claim-tok-1",
            command_runner=mock_runner,
        )
        self.assertEqual(p_res2["status"], "transitioned")
        self.assertTrue(p_res2["reconciled_existing_effect"])

        # 2. Publish PR (already open/merged)
        pr_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="publish_pr",
            claim_token="claim-tok-1",
            command_runner=mock_runner,
        )
        self.assertEqual(pr_res["status"], "transitioned")
        self.assertTrue(pr_res["reconciled_existing_effect"])

        # 3. Merge PR (already merged with mergeCommit)
        merge_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="merge_pr",
            claim_token="claim-tok-1",
            ci_checks=[{"name": "ci", "status": "SUCCESS", "conclusion": "SUCCESS", "sha": self.commit_sha}],
            merge_identity="selamy-labs-bot",
            command_runner=mock_runner,
        )
        self.assertEqual(merge_res["status"], "transitioned")
        self.assertTrue(merge_res["reconciled_existing_effect"])

        # 4. Complete Spec Task (exact token boundary: TASK-TRANS ticked, TASK-10 untouched)
        spec_res1 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="complete_spec_task",
            claim_token="claim-tok-1",
            command_runner=mock_runner,
        )
        self.assertEqual(spec_res1["status"], "transitioned")
        self.assertFalse(spec_res1["reconciled_existing_effect"])
        content = self.spec_file.read_text(encoding="utf-8")
        self.assertIn("- [x] TASK-TRANS", content)
        self.assertIn("- [ ] TASK-10 other", content)

        # Re-run complete_spec_task -> reconciled
        spec_res2 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="complete_spec_task",
            claim_token="claim-tok-1",
            command_runner=mock_runner,
        )
        self.assertEqual(spec_res2["status"], "transitioned")
        self.assertTrue(spec_res2["reconciled_existing_effect"])

    def test_complete_spec_task_and_merge_refused_on_old_review_revision(self):
        receipts_dir = self.control_dir / "receipts"
        old_sha = "0" * 40
        argon_file = receipts_dir / "review_W-TRANS_argon_s-argon.json"
        data = json.loads(argon_file.read_text(encoding="utf-8"))
        data["commit_sha"] = old_sha
        argon_file.write_text(json.dumps(data), encoding="utf-8")

        res_spec = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="complete_spec_task",
            claim_token="claim-tok-1",
        )
        self.assertEqual(res_spec["status"], "refused")
        self.assertEqual(res_spec["reason_code"], "STALE_REVIEW_REVISION")
        self.assertTrue((receipts_dir / "refusal_W-TRANS_STALE_REVIEW_REVISION.json").exists())

        res_merge = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="merge_pr",
            claim_token="claim-tok-1",
        )
        self.assertEqual(res_merge["status"], "refused")
        self.assertEqual(res_merge["reason_code"], "STALE_REVIEW_REVISION")

    def test_complete_spec_task_refused_on_exact_head_ci_failure(self):
        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"]["W-TRANS"]["transitions"] = [
            {"action": "push", "commit_sha": self.commit_sha, "tree_sha": self.tree_sha}
        ]
        workflow.save_coordinator_status(self.control_dir, doc)

        ci_failures = [
            {"name": "CI", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.commit_sha},
            {
                "name": "Credentialless gates (Linux)",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "head_sha": self.commit_sha,
            },
        ]
        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="complete_spec_task",
            claim_token="claim-tok-1",
            ci_checks=ci_failures,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "CI_CHECK_FAILED")
        receipts_dir = self.control_dir / "receipts"
        self.assertTrue((receipts_dir / "refusal_W-TRANS_CI_CHECK_FAILED.json").exists())
        self.assertTrue((receipts_dir / "refusal_W-TRANS_complete_spec_task_CI_CHECK_FAILED.json").exists())
        self.assertIn("- [ ] TASK-TRANS", self.spec_file.read_text(encoding="utf-8"))

        for concl in ("CANCELLED", "SKIPPED"):
            bad_checks = [
                {"name": "test-gate", "status": "COMPLETED", "conclusion": concl, "head_sha": self.commit_sha}
            ]
            res_bad = workflow.execute_transition(
                control_dir=self.control_dir,
                work_id="W-TRANS",
                action="complete_spec_task",
                claim_token="claim-tok-1",
                ci_checks=bad_checks,
            )
            self.assertEqual(res_bad["status"], "refused")
            self.assertEqual(res_bad["reason_code"], "CI_CHECK_FAILED")

        res_missing = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-TRANS",
            action="complete_spec_task",
            claim_token="claim-tok-1",
            ci_checks=[],
        )
        self.assertEqual(res_missing["status"], "refused")
        self.assertEqual(res_missing["reason_code"], "CI_CHECK_FAILED")
        self.assertIn("- [ ] TASK-TRANS", self.spec_file.read_text(encoding="utf-8"))


class TestEndToEndVerticalSlice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)
        self.base_sha = _init_git_repo(self.worktree)
        self.bare_remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.bare_remote)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.bare_remote)], cwd=self.worktree, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=self.worktree, check=True)

        self.spec_file = self.worktree / "tasks.md"
        self.spec_file.write_text("- [ ] TASK-E2E: implement e2e flow\n", encoding="utf-8")
        self.directive_file = self.root / "directive.md"
        self.directive_file.write_text("Directive e2e\n", encoding="utf-8")
        self.directive_sha = hashlib.sha256(self.directive_file.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _mock_e2e_gh(self, cmd, cmd_str, c2):
        if "api" in cmd and "user" in cmd:
            return 0, json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})
        if "pr view" in cmd_str:
            return 0, json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-11-03T10:00:00Z",
                    "mergeCommit": {"oid": "e" * 40},
                    "number": 1,
                    "headRefOid": c2,
                    "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c2}],
                }
            )
        return 0, ""

    def _mock_e2e_git(self, cmd, cmd_str, cwd):
        if "git rev-parse" in cmd_str:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            return p.returncode, p.stdout.strip()
        if "git diff" in cmd_str:
            return 0, ""
        if "git ls-remote" in cmd_str or "git push" in cmd_str:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            return p.returncode, p.stdout
        return 0, ""

    def test_full_e2e_vertical_slice(self):
        work_id = "W-E2E-001"
        project = "e2e-project"

        # 1. Admit work
        res_admit = workflow.admit_work(
            control_dir=self.control_dir,
            work_id=work_id,
            project=project,
            repository="owner/repo",
            task_id="TASK-E2E",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{self.directive_sha}",
            required_completion_action="push",
            spec_path=self.spec_file,
        )
        self.assertEqual(res_admit["status"], "admitted")

        # 2. Claim work
        res_claim = workflow.claim_work(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-lane-e2e",
            worktree=self.worktree,
            write_paths=["src/component/", "tasks.md"],
            allowed_roots=[str(self.root)],
            session_id="sess-e2e",
        )
        self.assertEqual(res_claim["status"], "claimed")
        claim_token = res_claim["claim_token"]

        # 3. Flash implementation Round 1
        c1, t1, p1 = _create_commit(self.worktree, "src/component/code.py", "def run(): return 1\n", "feat: R1 impl")
        res_r1 = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-lane-e2e",
            claim_token=claim_token,
            round=1,
            commit_sha=c1,
            tree_sha=t1,
            parent_sha=p1,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res_r1["status"], "recorded")

        # 4. Sol reviews R1 with REVISE
        rep1_path = self.root / "sol_r1.json"
        rep1_path.write_text(
            json.dumps(
                {
                    "commit_sha": c1,
                    "tree_sha": t1,
                    "defects": ["DEF-101"],
                    "verdict": "REVISE",
                    "model": "gpt-6.1-sol-high",
                    "dispatch": "disp-sol-1",
                }
            ),
            encoding="utf-8",
        )
        rep1_sha = hashlib.sha256(rep1_path.read_bytes()).hexdigest()
        res_rev1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="sol-agent",
            model="gpt-6.1-sol-high",
            family="sol",
            session_id="sess-sol-1",
            verdict="REVISE",
            report_path=rep1_path,
            report_sha256=rep1_sha,
        )
        self.assertEqual(res_rev1["status"], "recorded")
        self.assertEqual(res_rev1["verdict"], "REVISE")

        # 5. Flash remediates in Round 2
        subprocess.run(["git", "reset", "--hard", self.base_sha], cwd=self.worktree, check=True)
        c2, t2, p2 = _create_commit(
            self.worktree, "src/component/code.py", "def run(): return 2\n", "feat: R2 remediation"
        )
        res_r2 = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-lane-e2e",
            claim_token=claim_token,
            round=2,
            commit_sha=c2,
            tree_sha=t2,
            parent_sha=p2,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res_r2["status"], "recorded")

        # 6. Distinct reviews on R2 (argon, opus, sol)
        for fam in ("argon", "opus", "sol"):
            rep_p = self.root / f"review_{fam}.json"
            model = FAM_CANONICAL_MODELS[fam]
            rep_p.write_text(
                json.dumps(
                    {
                        "commit_sha": c2,
                        "tree_sha": t2,
                        "verdict": "SHIP",
                        "model": model,
                        "dispatch": f"disp-{fam}",
                    }
                ),
                encoding="utf-8",
            )
            rep_s = hashlib.sha256(rep_p.read_bytes()).hexdigest()
            rev_res = workflow.record_review(
                control_dir=self.control_dir,
                work_id=work_id,
                reviewer_id=f"rev-{fam}",
                model=model,
                family=fam,
                session_id=f"session-{fam}",
                verdict="SHIP",
                report_path=rep_p,
                report_sha256=rep_s,
            )
            self.assertEqual(rev_res["status"], "recorded")
            self.assertEqual(rev_res["verdict"], "SHIP")

        doc_r2 = workflow.load_coordinator_status(self.control_dir)
        self.assertTrue(doc_r2["delivery_control"]["work_items"][work_id]["qualified"])

        def mock_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if cmd and cmd[0] == "gh":
                return self._mock_e2e_gh(cmd, cmd_str, c2)
            return self._mock_e2e_git(cmd, cmd_str, cwd)

        push_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_token,
            branch="feat/e2e",
            command_runner=mock_runner,
        )
        self.assertEqual(push_res["status"], "transitioned", push_res)

        doc_bp = workflow.load_coordinator_status(self.control_dir)
        doc_bp["delivery_control"]["projects"][project]["branch_protection"] = {
            "verified": True,
            "evidence": ["ruleset.json"],
        }
        workflow.save_coordinator_status(self.control_dir, doc_bp)

        merge_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_token,
            command_runner=mock_runner,
        )
        self.assertEqual(merge_res["status"], "transitioned", merge_res)

        spec_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="complete_spec_task",
            claim_token=claim_token,
            command_runner=mock_runner,
        )
        self.assertEqual(spec_res["status"], "transitioned")
        self.assertIn("- [x] TASK-E2E", self.spec_file.read_text(encoding="utf-8"))

        (self.control_dir / "local-actions.json").write_text(json.dumps({"items": []}), encoding="utf-8")
        policy_path = self.root / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "expected_projects": [project],
                    "implementer_model": "gemini-3.8-flash-high",
                    "reviewer_models": [FAM_CANONICAL_MODELS[f] for f in ("argon", "opus", "sol")],
                    "required_review_roles": ["argon", "opus", "sol"],
                    "required_controls": [],
                    "max_snapshot_age_minutes": 60,
                    "max_progress_age_minutes": 120,
                    "directives": [{"path": str(self.directive_file), "sha256": self.directive_sha}],
                }
            ),
            encoding="utf-8",
        )
        audit_res = preflight.audit(self.control_dir, policy_path)
        self.assertEqual(audit_res["status"], "pass")
        self.assertEqual(len(audit_res.get("diagnostics", [])), 0)


class TestWorkflowCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)
        self.base_sha = _init_git_repo(self.worktree)
        self.directive = self.root / "directive.md"
        self.directive.write_text("Directive\n", encoding="utf-8")
        self.directive_sha = hashlib.sha256(self.directive.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_subcommands(self):
        # 1. CLI admit
        code_admit = workflow.main(
            [
                "admit",
                "--control-dir",
                str(self.control_dir),
                "--work-id",
                "W-CLI",
                "--project",
                "proj-cli",
                "--repository",
                "owner/repo",
                "--task-id",
                "T-CLI",
                "--base-sha",
                self.base_sha,
                "--approval-ref",
                f"{self.directive}:{self.directive_sha}",
            ]
        )
        self.assertEqual(code_admit, 0)

        # 2. CLI claim with --session-id
        code_claim = workflow.main(
            [
                "claim",
                "--control-dir",
                str(self.control_dir),
                "--work-id",
                "W-CLI",
                "--owner",
                "owner-cli",
                "--session-id",
                "sess-cli",
                "--worktree",
                str(self.worktree),
                "--write-paths",
                "src/",
            ]
        )
        self.assertEqual(code_claim, 0)

        doc = workflow.load_coordinator_status(self.control_dir)
        claim_token = doc["delivery_control"]["work_items"]["W-CLI"]["claim"]["claim_token"]

        # 3. CLI record-implementation
        c, t, p = _create_commit(self.worktree, "src/code.py", "x = 1\n", "feat: cli impl")
        code_impl = workflow.main(
            [
                "record-implementation",
                "--control-dir",
                str(self.control_dir),
                "--work-id",
                "W-CLI",
                "--owner",
                "owner-cli",
                "--claim-token",
                claim_token,
                "--round",
                "1",
                "--commit-sha",
                c,
                "--tree-sha",
                t,
                "--parent-sha",
                p,
            ]
        )
        self.assertEqual(code_impl, 0)

        # 4. CLI record-review
        for fam in ("argon", "opus", "sol"):
            model = FAM_CANONICAL_MODELS[fam]
            rep_file = self.root / f"rep_{fam}.json"
            rep_file.write_text(
                json.dumps(
                    {
                        "commit_sha": c,
                        "tree_sha": t,
                        "verdict": "SHIP",
                        "model": model,
                        "dispatch": f"disp-cli-{fam}",
                    }
                ),
                encoding="utf-8",
            )
            rep_h = hashlib.sha256(rep_file.read_bytes()).hexdigest()
            code_rev = workflow.main(
                [
                    "record-review",
                    "--control-dir",
                    str(self.control_dir),
                    "--work-id",
                    "W-CLI",
                    "--reviewer-id",
                    f"rev-{fam}",
                    "--model",
                    model,
                    "--family",
                    fam,
                    "--session-id",
                    f"sess-cli-{fam}",
                    "--verdict",
                    "SHIP",
                    "--report-path",
                    str(rep_file),
                    "--report-sha256",
                    rep_h,
                ]
            )
            self.assertEqual(code_rev, 0)

        # 5. CLI execute-transition (refusal on protected branch push)
        code_trans_refused = workflow.main(
            [
                "execute-transition",
                "--control-dir",
                str(self.control_dir),
                "--work-id",
                "W-CLI",
                "--action",
                "push",
                "--branch",
                "main",
                "--claim-token",
                claim_token,
            ]
        )
        self.assertEqual(code_trans_refused, 2)


class TestWorkflowDetailedEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)
        self.base_sha = _init_git_repo(self.worktree)
        self.directive = self.root / "directive.md"
        self.directive.write_text("Directive\n", encoding="utf-8")
        self.directive_sha = hashlib.sha256(self.directive.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_is_approved_identity_variants(self):
        self.assertTrue(workflow._is_approved_identity("selamy-labs-bot"))
        self.assertTrue(
            workflow._is_approved_identity("selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>")
        )
        self.assertTrue(workflow._is_approved_identity("<346154065+selamy-labs-bot@users.noreply.github.com>"))
        self.assertFalse(workflow._is_approved_identity("unauthorized <bad@example.com>"))
        self.assertFalse(workflow._is_approved_identity("random-identity"))

    def test_sha_and_verdict_extraction(self):
        self.assertIsNone(workflow._extract_sha_from_report({"other": "val"}, ["commit", "sha"]))
        self.assertEqual(workflow._extract_embedded_verdict({"status": "SHIP"}), "SHIP")
        self.assertIsNone(workflow._extract_embedded_verdict({"status": "unknown"}))
        self.assertEqual(workflow._extract_reviewer_strings("Sol-Model"), ["sol-model"])
        self.assertEqual(workflow._extract_reviewer_strings({"model": "M", "name": "N"}), ["m", "n"])
        self.assertEqual(workflow._extract_reviewer_strings(None), [])
        models = workflow._collect_embedded_model_strings({"attestation": {"signedBy": "Argon-Signer"}})
        self.assertIn("argon-signer", models)

    def test_path_and_scope_inspection(self):
        self.assertTrue(workflow._is_path_allowed("file.txt", [""]))
        self.assertTrue(workflow._is_protected_skill_file("skills/portfolio-delivery-control/file.py"))
        self.assertEqual(workflow._inspect_modified_paths(Path("."), "", "", "repo", False, None), (True, ""))
        self.assertEqual(
            workflow._inspect_modified_paths(
                Path("."), "b", "c", "repo", False, None, runner=lambda cmd, cwd: (1, "fail")
            ),
            (False, "BASE_SHA_DRIFT"),
        )

    def test_iso_timestamp_and_paths_overlap(self):
        naive_dt = datetime.datetime(2026, 1, 1, 12, 0)
        ts = workflow.iso_timestamp(naive_dt)
        self.assertIn("+00:00", ts)
        self.assertTrue(workflow.paths_overlap("src/*", "src/foo/bar.py"))

    def test_approval_and_spec_validation(self):
        self.assertTrue(workflow._is_hex_64("a" * 64))
        self.assertFalse(workflow._is_hex_64("not-a-hash"))
        self.assertTrue(workflow._check_sha_match("a" * 64, "a" * 64))
        self.assertFalse(workflow._check_sha_match("a" * 64, "b" * 64))
        self.assertFalse(workflow._check_sha_match("a" * 64, None))
        self.assertFalse(workflow._validate_spec_task(None, "TASK-1"))
        self.assertFalse(workflow._validate_spec_task(Path("/nonexistent"), "TASK-1"))

    def test_duplicate_project_item_and_directive(self):
        self.assertTrue(workflow._is_duplicate_project_item({"p1": {"work_item": {"admission_key": "W-DUP"}}}, "W-DUP"))
        entry = workflow._extract_directive_entry({"path": "dir.md", "sha256": "b" * 64})
        self.assertEqual(entry, {"path": "dir.md", "sha256": "b" * 64})
        self.assertIsNone(workflow._extract_directive_entry(None))

    def test_admit_into_existing_project(self):
        res1 = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-ADMIT-1",
            project="proj-multi",
            repository="owner/repo",
            task_id="TASK-1",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive}:{self.directive_sha}",
            verify_spec=False,
        )
        self.assertEqual(res1["status"], "admitted")

        res2 = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-ADMIT-2",
            project="proj-multi",
            repository="owner/repo",
            task_id="TASK-2",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive}:{self.directive_sha}",
            verify_spec=False,
        )
        self.assertEqual(res2["status"], "admitted")
        doc = workflow.load_coordinator_status(self.control_dir)
        p_row = doc["delivery_control"]["projects"]["proj-multi"]
        self.assertEqual(len(p_row["evidence"]), 2)

    def test_validate_worktree_and_write_paths(self):
        self.assertEqual(
            workflow._validate_worktree(None, ["/tmp"]),
            (False, "SCOPE_ESCAPE", "Missing or invalid worktree path"),
        )
        self.assertFalse(workflow._validate_worktree("/tmp/skills/portfolio-delivery-control", ["/tmp"])[0])
        self.assertEqual(workflow._validate_single_write_path(123), (False, "Invalid write path entry"))
        self.assertEqual(
            workflow._validate_single_write_path("a/../b"),
            (False, "Write path a/../b attempts directory traversal"),
        )
        self.assertFalse(workflow._validate_single_write_path("skills/portfolio-delivery-control/foo")[0])
        self.assertEqual(workflow._validate_write_paths([]), (False, "Missing or invalid write_paths list"))

    def test_prerequisites_and_claim_freshness(self):
        self.assertFalse(workflow._check_prerequisites_met({"delivery_control": {"work_items": {}}}, ["P-MISSING"]))
        self.assertFalse(
            workflow._check_prerequisites_met(
                {"delivery_control": {"work_items": {"P-1": {"stage": "implementing"}}}}, ["P-1"]
            )
        )
        now_dt = datetime.datetime.now(datetime.UTC)
        self.assertFalse(workflow._claim_is_active(None, now_dt))
        self.assertFalse(workflow._claim_is_active({"expires_at": ""}, now_dt))
        self.assertFalse(workflow._claim_is_active({"expires_at": "bad-date"}, now_dt))
        has_claim, _ = workflow._check_work_id_claim(
            {"claim": {"owner": "other", "expires_at": "2099-01-01T00:00:00+00:00"}}, "W-1", "me", now_dt
        )
        self.assertTrue(has_claim)

    def test_mandatory_gates_and_artifacts(self):
        self.assertFalse(workflow._verify_mandatory_gates(None))
        self.assertTrue(workflow._verify_mandatory_gates({"tests": {"status": "passed"}}))
        self.assertTrue(workflow._verify_mandatory_gates({"pytest": {"exit_code": 0}}))
        self.assertFalse(workflow._verify_mandatory_gates({"tests": {"status": "failed"}}))
        self.assertFalse(workflow._verify_artifact_hashes([("nonexistent.txt", "abc")]))

        class MockRes:
            returncode = 0
            stdout = "mock-output"

        code, out = workflow._run_cmd(["git", "status"], self.root, runner=lambda cmd, cwd: MockRes())
        self.assertEqual(code, 0)
        self.assertEqual(out, "mock-output")

    def test_parent_and_count_verification(self):
        def bad_parent_runner(cmd, cwd=None):
            if "HEAD^" in " ".join(cmd):
                return 0, "wrong_parent"
            return 0, "1"

        ok, err = workflow._verify_parent_and_count(
            self.worktree, self.base_sha, self.base_sha, runner=bad_parent_runner
        )
        self.assertFalse(ok)
        self.assertEqual(err, "BASE_SHA_DRIFT")

        def multi_commit_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "HEAD^" in cmd_str:
                return 0, self.base_sha
            if f"HEAD..{self.base_sha}" in cmd_str:
                return 0, "0"
            return 0, "2"

        ok, err = workflow._verify_parent_and_count(
            self.worktree, self.base_sha, self.base_sha, runner=multi_commit_runner
        )
        self.assertFalse(ok)
        self.assertEqual(err, "NON_SINGLE_COMMIT_RANGE")

        def invalid_count_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "HEAD^" in cmd_str:
                return 0, self.base_sha
            if f"HEAD..{self.base_sha}" in cmd_str:
                return 0, "0"
            return 0, "not_an_int"

        ok, err = workflow._verify_parent_and_count(
            self.worktree, self.base_sha, self.base_sha, runner=invalid_count_runner
        )
        self.assertFalse(ok)
        self.assertEqual(err, "NON_SINGLE_COMMIT_RANGE")

    def test_review_report_parsing_and_verification(self):
        ok, err, _ = workflow._parse_and_verify_review_report("nonexistent_report.json", "abc")
        self.assertEqual(err, "ARTIFACT_HASH_MISMATCH")

        rep_path = self.root / "invalid.json"
        rep_path.write_text("not json content", encoding="utf-8")
        h = hashlib.sha256(rep_path.read_bytes()).hexdigest()
        ok, err, _ = workflow._parse_and_verify_review_report(rep_path, h)
        self.assertEqual(err, "REPORT_INVALID")

        rep_path.write_text('"just a string"', encoding="utf-8")
        h = hashlib.sha256(rep_path.read_bytes()).hexdigest()
        ok, err, _ = workflow._parse_and_verify_review_report(rep_path, h)
        self.assertEqual(err, "REPORT_INVALID")

        self.assertFalse(workflow._verify_review_report(rep_path, {"commit_sha": "c"}))
        self.assertFalse(workflow._verify_review_report(12345, {"commit_sha": "c"}))

        rep_valid = self.root / "rep_valid.json"
        rep_valid.write_text(json.dumps({"commit_sha": "c1", "tree_sha": "t_wrong"}), encoding="utf-8")
        self.assertFalse(workflow._verify_review_report(rep_valid, {"commit_sha": "c1", "tree_sha": "t1"}))

        ok, err = workflow._verify_reviewer_uniqueness([{"session_id": "sess-dup"}], "argon", "sess-dup")
        self.assertEqual(err, "DUPLICATE_REVIEWER_SESSION")

    def test_transition_guards_coverage(self):
        ok, err = workflow._guard_approval_and_defects({"approval_ref": ""})
        self.assertEqual(err, "ABSENT_APPROVAL")

        ok, err = workflow._guard_approval_and_defects({"approval_ref": "ref", "remediation_required": True})
        self.assertEqual(err, "UNRESOLVED_REVISE_FINDINGS")

        now_dt = datetime.datetime.now(datetime.UTC)
        ok, err = workflow._guard_claim_freshness(
            {"claim": {"claim_token": "tok1", "expires_at": "2099-01-01T00:00:00+00:00"}}, "tok_wrong", now_dt
        )
        self.assertEqual(err, "STALE_CLAIM")

        ok, err = workflow._guard_claim_freshness(
            {"claim": {"claim_token": "tok1", "expires_at": "2020-01-01T00:00:00+00:00"}}, "tok1", now_dt
        )
        self.assertEqual(err, "CLAIM_EXPIRED")

        ok, err = workflow._check_ship_reviews_match([{"verdict": "SHIP"}], "c", "t")
        self.assertEqual(err, "MISSING_QUALIFICATION")

        self.assertFalse(workflow._check_single_ci("not-a-dict", "c"))
        self.assertFalse(workflow._check_single_ci({"status": "FAILURE"}, "c"))
        self.assertFalse(workflow._check_single_ci({"status": "SUCCESS", "head_sha": "wrong_sha"}, "c"))

        self.assertFalse(workflow._verify_ci_checks_exact_head([], "c", require_sha=True))

    def test_reconcile_and_push_branches(self):
        ok, res = workflow._reconcile_and_push(self.worktree, "origin", "main", "main", "c1")
        self.assertFalse(ok)
        self.assertEqual(res.get("reason_code"), "PROTECTED_BRANCH_PUSH")

        def push_fail_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git ls-remote" in cmd_str:
                return 0, "remote_sha_old refs/heads/feat/branch"
            if "git push" in cmd_str:
                return 1, "rejected by remote"
            return 0, ""

        ok, res = workflow._reconcile_and_push(
            self.worktree, "origin", "feat/branch", "main", "c1", runner=push_fail_runner
        )
        self.assertFalse(ok)
        self.assertIn("rejected by remote", res.get("error", ""))

        def push_success_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git ls-remote" in cmd_str:
                return 0, "remote_sha_old refs/heads/feat/branch"
            if "git push" in cmd_str:
                return 0, "pushed successfully"
            return 0, ""

        ok, res = workflow._reconcile_and_push(
            self.worktree, "origin", "feat/branch", "main", "c1", runner=push_success_runner
        )
        self.assertTrue(ok)

    def test_reconcile_and_publish_pr_branches_existing(self):
        def pr_view_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "gh pr view" in cmd_str:
                return 0, json.dumps({"state": "OPEN", "headRefOid": "c_match", "number": 10, "url": "https://pr/10"})
            return 0, ""

        ok, res = workflow._reconcile_and_publish_pr(
            self.worktree, "feat/b", "main", "c_match", "title", "body", runner=pr_view_runner
        )
        self.assertTrue(ok)
        self.assertEqual(res.get("pr_number"), 10)

    def test_reconcile_and_publish_pr_branches_create_and_fail(self):
        calls = []

        def pr_create_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            calls.append(cmd_str)
            if "git ls-remote" in cmd_str:
                return 0, "c_match refs/heads/feat/b"
            if "gh pr view" in cmd_str:
                if len([c for c in calls if "gh pr view" in c]) == 1:
                    return 1, "not found"
                return 0, json.dumps({"state": "OPEN", "headRefOid": "c_match", "number": 11, "url": "https://pr/11"})
            if "gh pr create" in cmd_str:
                return 0, "https://pr/11"
            return 0, ""

        ok, res = workflow._reconcile_and_publish_pr(
            self.worktree, "feat/b", "main", "c_match", "title", "body", runner=pr_create_runner
        )
        self.assertTrue(ok)

        def pr_create_fail_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git ls-remote" in cmd_str:
                return 0, "c_match refs/heads/feat/b"
            if "gh pr view" in cmd_str:
                return 1, "not found"
            if "gh pr create" in cmd_str:
                return 1, "pr create failed"
            return 0, ""

        ok, res = workflow._reconcile_and_publish_pr(
            self.worktree, "feat/b", "main", "c_match", "title", "body", runner=pr_create_fail_runner
        )
        self.assertFalse(ok)

    def test_reconcile_and_merge_pr_view_failures(self):
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def view_fail_runner(cmd, cwd=None):
            if "api" in cmd and "user" in cmd:
                return 0, bot_user
            return 1, "gh pr view network error"

        ok, res = workflow._reconcile_and_merge_pr(self.worktree, "1", "c1", runner=view_fail_runner)
        self.assertFalse(ok)

        def view_invalid_runner(cmd, cwd=None):
            if "api" in cmd and "user" in cmd:
                return 0, bot_user
            return 0, "not json"

        ok, res = workflow._reconcile_and_merge_pr(self.worktree, "1", "c1", runner=view_invalid_runner)
        self.assertFalse(ok)

    def test_reconcile_and_merge_pr_head_and_success(self):
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def view_head_mismatch_runner(cmd, cwd=None):
            if "api" in cmd and "user" in cmd:
                return 0, bot_user
            return 0, json.dumps({"state": "OPEN", "headRefOid": "c_different"})

        ok, res = workflow._reconcile_and_merge_pr(self.worktree, "1", "c1", runner=view_head_mismatch_runner)
        self.assertFalse(ok)
        self.assertEqual(res.get("reason_code"), "PR_HEAD_MISMATCH")

        def merge_success_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr merge" in cmd_str:
                merge_success_runner.merged = True
                return 0, "merged successfully"
            if "gh pr view" in cmd_str:
                st = "MERGED" if getattr(merge_success_runner, "merged", False) else "OPEN"
                return 0, json.dumps(
                    {
                        "state": st,
                        "mergedAt": "2026-11-03T10:00:00Z" if st == "MERGED" else None,
                        "mergeCommit": {"oid": "e" * 40} if st == "MERGED" else None,
                        "headRefOid": "c1",
                        "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": "c1"}],
                    }
                )
            return 0, ""

        ok, res = workflow._reconcile_and_merge_pr(self.worktree, "1", "c1", runner=merge_success_runner)
        self.assertTrue(ok)

    def test_reconcile_and_complete_spec_task_branches(self):
        ok, res = workflow._reconcile_and_complete_spec_task(None, "TASK-1")
        self.assertFalse(ok)

        ok, res = workflow._reconcile_and_complete_spec_task("/nonexistent/spec.md", "TASK-1")
        self.assertFalse(ok)

        spec = self.root / "no_match_spec.md"
        spec.write_text("# Spec\n", encoding="utf-8")
        ok, res = workflow._reconcile_and_complete_spec_task(spec, "TASK-1")
        self.assertFalse(ok)

    def test_cli_additional_branches(self):
        code_unknown = workflow.main(
            [
                "execute-transition",
                "--control-dir",
                str(self.control_dir),
                "--work-id",
                "W",
                "--action",
                "unknown_action",
            ]
        )
        self.assertEqual(code_unknown, 2)

        code_disallowed_merge = workflow.main(
            [
                "execute-transition",
                "--control-dir",
                str(self.control_dir),
                "--work-id",
                "W",
                "--action",
                "merge_pr",
                "--merge-identity",
                "rogue-bot",
            ]
        )
        self.assertEqual(code_disallowed_merge, 2)


class TestRound4Findings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)
        self.base_sha = _init_git_repo(self.worktree)
        self.spec_file = self.worktree / "tasks.md"
        self.spec_file.write_text("- [ ] TASK-R4\n", encoding="utf-8")
        self.directive_file = self.root / "directive.md"
        self.directive_file.write_text("# Directive R4\nApproved.\n", encoding="utf-8")
        self.directive_sha = hashlib.sha256(self.directive_file.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_report(self, data: dict[str, Any], no_prov: bool = False) -> tuple[Path, str]:
        p = self.root / f"rep_{secrets.token_hex(4)}.json"
        rep_data = dict(data)
        if (
            "dispatch" not in rep_data
            and "session_id" not in rep_data
            and "attestation" not in rep_data
            and "sha256" not in rep_data
            and not no_prov
            and not rep_data.get("no_prov")
        ):
            rep_data["dispatch"] = f"disp-{secrets.token_hex(4)}"
        rep_data.pop("no_prov", None)
        p.write_text(json.dumps(rep_data), encoding="utf-8")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        return p, h

    def _setup_admitted_candidate(self, work_id="W-R4", req_action="merge_pr"):
        workflow.admit_work(
            control_dir=self.control_dir,
            work_id=work_id,
            project="proj-r4",
            repository="owner/repo",
            task_id="TASK-R4",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{self.directive_sha}",
            required_completion_action=req_action,
            spec_path=self.spec_file,
        )
        claim_res = workflow.claim_work(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-impl",
            worktree=self.worktree,
            write_paths=["src/"],
            allowed_roots=[str(self.root)],
            session_id="sess-flash-1",
            model="gemini-3.8-flash-high",
        )
        c, t, p = _create_commit(self.worktree, "src/code.py", "def a(): pass\n", "feat: r4 impl")
        workflow.record_implementation(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-impl",
            claim_token=claim_res["claim_token"],
            round=1,
            commit_sha=c,
            tree_sha=t,
            parent_sha=p,
            gate_results={"test_exit_code": 0},
        )
        return work_id, claim_res["claim_token"], c, t

    def test_r4_unattributed_report_refusal(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        p, h = self._write_report({"commit_sha": c, "tree_sha": t, "verdict": "SHIP"}, no_prov=True)
        res = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-argon",
            model="gemini-4-argon-high",
            family="argon",
            session_id="sess-argon-1",
            verdict="SHIP",
            report_path=p,
            report_sha256=h,
        )
        self.assertEqual(res["status"], "refused")
        self.assertIn(res["reason_code"], ("REPORT_IDENTITY_MISMATCH", "UNAPPROVED_REVIEWER_MODEL"))

    def test_r4_duplicate_report_reuse_refusal(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        p, h = self._write_report({"commit_sha": c, "tree_sha": t, "verdict": "SHIP", "model": "gemini-4-argon-high"})
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-argon",
            model="gemini-4-argon-high",
            family="argon",
            session_id="sess-argon-1",
            verdict="SHIP",
            report_path=p,
            report_sha256=h,
        )
        self.assertEqual(res1["status"], "recorded")

        res2 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-opus-1",
            verdict="SHIP",
            report_path=p,
            report_sha256=h,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "REPORT_IDENTITY_MISMATCH")

    def test_r4_embedded_self_review_refusal(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()

        # Embedded implementer session_id
        p1, h1 = self._write_report(
            {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "claude-opus-5-5",
                "session_id": "sess-flash-1",
            }
        )
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-other",
            verdict="SHIP",
            report_path=p1,
            report_sha256=h1,
        )
        self.assertEqual(res1["status"], "refused")
        self.assertEqual(res1["reason_code"], "SELF_REVIEW_FORBIDDEN")

        # Embedded implementer claim owner
        p2, h2 = self._write_report(
            {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "claude-opus-5-5",
                "attestation": {"signedBy": "flash-impl"},
            }
        )
        res2 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-other-2",
            verdict="SHIP",
            report_path=p2,
            report_sha256=h2,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "SELF_REVIEW_FORBIDDEN")

        # Embedded flash model
        p3, h3 = self._write_report(
            {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "gemini-3.8-flash-argon",
                "reviewer": {"model": "gemini-3.8-flash-argon"},
            }
        )
        res3 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-argon-3",
            model="gemini-3.8-flash-argon",
            family="argon",
            session_id="sess-argon-3",
            verdict="SHIP",
            report_path=p3,
            report_sha256=h3,
        )
        self.assertEqual(res3["status"], "refused")
        self.assertEqual(res3["reason_code"], "SELF_REVIEW_FORBIDDEN")

    def test_r4_embedded_consistency_refusal(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()

        # Contradicting session
        p1, h1 = self._write_report(
            {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "claude-opus-5-5",
                "session": "sess-embedded-99",
            }
        )
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-caller-88",
            verdict="SHIP",
            report_path=p1,
            report_sha256=h1,
        )
        self.assertEqual(res1["status"], "refused")
        self.assertIn(res1["reason_code"], ("REPORT_IDENTITY_MISMATCH", "REVIEWER_SESSION_MISMATCH"))

        # Contradicting reviewer
        p2, h2 = self._write_report(
            {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "claude-opus-5-5",
                "reviewer": {"id": "rev-embedded-diff"},
            }
        )
        res2 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-caller-diff",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-opus-2",
            verdict="SHIP",
            report_path=p2,
            report_sha256=h2,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "REPORT_IDENTITY_MISMATCH")

    def _qualify_work_item(self, work_id, c, t):
        for fam in ("argon", "opus", "sol"):
            model = FAM_CANONICAL_MODELS[fam]
            p, h = self._write_report(
                {
                    "commit_sha": c,
                    "tree_sha": t,
                    "verdict": "SHIP",
                    "model": model,
                    "dispatch": f"disp-{fam}",
                }
            )
            res = workflow.record_review(
                control_dir=self.control_dir,
                work_id=work_id,
                reviewer_id=f"rev-{fam}",
                model=model,
                family=fam,
                session_id=f"sess-{fam}",
                verdict="SHIP",
                report_path=p,
                report_sha256=h,
            )
            self.assertEqual(res["status"], "recorded")

    def test_r4_provider_merge_ci_override_prevented(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        self._qualify_work_item(work_id, c, t)

        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_fail(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "OPEN",
                        "headRefOid": c,
                        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE", "head_sha": c}],
                    }
                )
            return 0, ""

        # Passing caller checks must NOT override failing provider rollup
        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            ci_checks=[{"status": "SUCCESS", "head_sha": c}],
            command_runner=runner_fail,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "MISSING_EXACT_HEAD_CI")

    def test_r4_caller_ci_without_sha_fails(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        self._qualify_work_item(work_id, c, t)

        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_green(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "OPEN",
                        "headRefOid": c,
                        "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
                    }
                )
            return 0, ""

        # Caller CI without SHA field must fail with require_sha=True
        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            ci_checks=[{"status": "SUCCESS"}],
            command_runner=runner_green,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "MISSING_EXACT_HEAD_CI")

    def test_r4_provider_actor_identity_check(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        self._qualify_work_item(work_id, c, t)

        # Human dev identity returned from provider
        def runner_human(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, json.dumps({"login": "pselamy", "name": "Patrick Selamy"})
            return 0, ""

        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_human,
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "DISALLOWED_MERGE_IDENTITY")

        # Mismatched bot identity
        def runner_bot_mismatch(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})
            return 0, ""

        res_mis = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            merge_identity="claude-cir",
            command_runner=runner_bot_mismatch,
        )
        self.assertEqual(res_mis["status"], "refused")
        self.assertEqual(res_mis["reason_code"], "DISALLOWED_MERGE_IDENTITY")

    def test_r4_already_merged_head_mismatch(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        self._qualify_work_item(work_id, c, t)
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_mismatched_head(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": "other-head-sha",
                        "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
                        "mergeCommit": {"oid": "e" * 40},
                    }
                )
            return 0, ""

        res1 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_mismatched_head,
        )
        self.assertEqual(res1["status"], "refused")
        self.assertEqual(res1["reason_code"], "PR_HEAD_MISMATCH")

    def test_r4_already_merged_missing_ci(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        self._qualify_work_item(work_id, c, t)
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_missing_ci(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": c,
                        "statusCheckRollup": [],
                        "mergeCommit": {"oid": "e" * 40},
                    }
                )
            return 0, ""

        res2 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_missing_ci,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "MISSING_EXACT_HEAD_CI")

    def test_r4_already_merged_success_with_ci(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        self._qualify_work_item(work_id, c, t)
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_merged_ok(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "mergedAt": "2026-11-03T10:00:00Z",
                        "headRefOid": c,
                        "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
                        "mergeCommit": {"oid": "e" * 40},
                    }
                )
            return 0, ""

        res3 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_merged_ok,
        )
        self.assertEqual(res3["status"], "transitioned")
        self.assertTrue(res3["reconciled_existing_effect"])
        self.assertEqual(res3.get("merge_commit_sha"), "e" * 40)

    def test_r4_truthful_ci_and_branch_protection_projection(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate()
        self._qualify_work_item(work_id, c, t)

        doc = workflow.load_coordinator_status(self.control_dir)
        p_row = doc["delivery_control"]["projects"]["proj-r4"]
        # Reviews alone must project CI as pending and branch_protection as false
        self.assertEqual(p_row["ci"]["status"], "pending")
        self.assertFalse(p_row["branch_protection"]["verified"])

        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_merged_ok(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "gh" in cmd and "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "mergedAt": "2026-11-03T10:00:00Z",
                        "headRefOid": c,
                        "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
                        "mergeCommit": {"oid": "e" * 40},
                    }
                )
            return 0, ""

        workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_merged_ok,
        )

        doc_after = workflow.load_coordinator_status(self.control_dir)
        p_row_after = doc_after["delivery_control"]["projects"]["proj-r4"]
        self.assertEqual(p_row_after["ci"]["status"], "success")
        self.assertFalse(p_row_after["branch_protection"]["verified"])

    def _runner_mock_git(
        self, cmd: list[str], cwd: Any = None, extra_json: dict[str, Any] | None = None
    ) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if "git rev-parse" in cmd_str:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            return p.returncode, p.stdout.strip()
        if "api" in cmd and "user" in cmd:
            return 0, json.dumps({"login": "selamy-labs-bot"})
        if "gh pr view" in cmd_str and extra_json is not None:
            return 0, json.dumps(extra_json)
        return 0, ""

    def test_r4_revision_bound_transitions_invalidated(self):
        work_id, claim_tok, c1, t1 = self._setup_admitted_candidate(req_action="merge_pr")
        self._qualify_work_item(work_id, c1, t1)

        # Push R1
        p_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_tok,
            command_runner=self._runner_mock_git,
        )
        self.assertEqual(p_res["status"], "transitioned")

        # Now implementation R2 is recorded
        subprocess.run(["git", "reset", "--hard", self.base_sha], cwd=self.worktree, check=True)
        c2, t2, p2 = _create_commit(self.worktree, "src/code.py", "def a(): return 2\n", "feat: r2")
        workflow.record_implementation(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-impl",
            claim_token=claim_tok,
            round=2,
            commit_sha=c2,
            tree_sha=t2,
            parent_sha=p2,
            gate_results={"test_exit_code": 0},
        )
        # R1 transitions should be invalidated/pruned
        doc = workflow.load_coordinator_status(self.control_dir)
        self.assertEqual(len(doc["delivery_control"]["work_items"][work_id]["transitions"]), 0)

    def test_r4_required_completion_action_enforced(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate(req_action="merge_pr")
        self._qualify_work_item(work_id, c, t)

        # complete_spec_task without any transition must fail
        comp_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="complete_spec_task",
            claim_token=claim_tok,
            command_runner=self._runner_mock_git,
        )
        self.assertEqual(comp_res["status"], "refused")
        self.assertEqual(comp_res["reason_code"], "MISSING_TRANSITION_EVIDENCE")

        # Push candidate
        workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_tok,
            command_runner=self._runner_mock_git,
        )

        # Because required_completion_action is merge_pr, lone push must still refuse complete_spec_task
        comp_res2 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="complete_spec_task",
            claim_token=claim_tok,
            command_runner=self._runner_mock_git,
        )
        self.assertEqual(comp_res2["status"], "refused")
        self.assertEqual(comp_res2["reason_code"], "MISSING_TRANSITION_EVIDENCE")

        # Merge candidate
        extra_pr = {
            "state": "MERGED",
            "mergedAt": "2026-11-03T10:00:00Z",
            "headRefOid": c,
            "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
            "mergeCommit": {"oid": "e" * 40},
        }
        m_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=lambda cmd, cwd=None: self._runner_mock_git(cmd, cwd=cwd, extra_json=extra_pr),
        )
        self.assertEqual(m_res["status"], "transitioned")

        # Now complete_spec_task succeeds
        comp_res3 = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="complete_spec_task",
            claim_token=claim_tok,
            command_runner=self._runner_mock_git,
        )
        self.assertEqual(comp_res3["status"], "transitioned")
        self.assertIn("- [x] TASK-R4", self.spec_file.read_text(encoding="utf-8"))

    def test_r4_live_approval_verification(self):
        # 1. Nonexistent file at admission
        res_no_file = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-NOFILE",
            project="proj-appr",
            repository="owner/repo",
            task_id="TASK-R4",
            base_sha=self.base_sha,
            approval_ref=f"{self.root}/nonexistent.md:{'a' * 64}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res_no_file["status"], "refused")
        self.assertEqual(res_no_file["reason_code"], "MISSING_APPROVAL")

        # 2. Stale sha at admission
        res_stale = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-STALE",
            project="proj-appr",
            repository="owner/repo",
            task_id="TASK-R4",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:deadbeef12345678",
            spec_path=self.spec_file,
        )
        self.assertEqual(res_stale["status"], "refused")
        self.assertEqual(res_stale["reason_code"], "STALE_APPROVAL")

        # 3. Withdrawn marker at admission
        withdrawn_file = self.root / "withdrawn.md"
        withdrawn_file.write_text('{"status": "withdrawn"}\n', encoding="utf-8")
        withdrawn_sha = hashlib.sha256(withdrawn_file.read_bytes()).hexdigest()
        res_withdrawn = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-WITHDRAWN",
            project="proj-appr",
            repository="owner/repo",
            task_id="TASK-R4",
            base_sha=self.base_sha,
            approval_ref=f"{withdrawn_file}:{withdrawn_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res_withdrawn["status"], "refused")
        self.assertEqual(res_withdrawn["reason_code"], "APPROVAL_WITHDRAWN")

        # 4. Valid admission followed by modification at transition execution
        work_id, claim_tok, c, t = self._setup_admitted_candidate(work_id="W-LIVE-APPR", req_action="push")
        self._qualify_work_item(work_id, c, t)

        def runner_simple(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            return 0, ""

        # Modifying file with DO NOT PUBLISH withdrawal marker
        self.directive_file.write_text("# Directive\nDO NOT PUBLISH\n", encoding="utf-8")
        res_trans_withdrawn = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_tok,
            command_runner=runner_simple,
        )
        self.assertEqual(res_trans_withdrawn["status"], "refused")
        self.assertEqual(res_trans_withdrawn["reason_code"], "APPROVAL_WITHDRAWN")

        # Modifying file digest (hash drift)
        self.directive_file.write_text("# Directive changed content\n", encoding="utf-8")
        res_trans_stale = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_tok,
            command_runner=runner_simple,
        )
        self.assertEqual(res_trans_stale["status"], "refused")
        self.assertEqual(res_trans_stale["reason_code"], "STALE_APPROVAL")

        # Deleting file from disk
        self.directive_file.unlink()
        res_trans_missing = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_tok,
            command_runner=runner_simple,
        )
        self.assertEqual(res_trans_missing["status"], "refused")
        self.assertEqual(res_trans_missing["reason_code"], "MISSING_APPROVAL")

    def test_r4_coverage_gap_fillers(self):
        # 1. record_review on unadmitted item
        rep_file = self.root / "rep_dummy.json"
        rep_file.write_text(
            json.dumps(
                {
                    "commit_sha": "abc",
                    "tree_sha": "def",
                    "model": "gemini-4.0-argon-high",
                    "verdict": "SHIP",
                    "dispatch": "disp-dummy",
                }
            ),
            encoding="utf-8",
        )
        res_unadm = workflow.record_review(
            control_dir=self.control_dir,
            work_id="UNADMITTED_WORK",
            reviewer_id="argon-rev",
            session_id="sess-argon",
            family="argon",
            model="gemini-4.0-argon-high",
            verdict="SHIP",
            report_path=rep_file,
            report_sha256=hashlib.sha256(rep_file.read_bytes()).hexdigest(),
        )
        self.assertEqual(res_unadm["status"], "refused")
        self.assertEqual(res_unadm["reason_code"], "WORK_NOT_ADMITTED")

        # 2. record_review on admitted item with no candidate revision
        dummy_dir = self.root / "dummy_dir.md"
        dummy_dir.write_text("# Approval\n", encoding="utf-8")
        res_adm = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-NO-CAND",
            project="proj-r4",
            repository="owner/repo",
            task_id="TASK-R4",
            base_sha=self.base_sha,
            spec_path=self.spec_file,
            approval_ref=str(dummy_dir),
            approval_sha256=hashlib.sha256(dummy_dir.read_bytes()).hexdigest(),
        )
        self.assertEqual(res_adm["status"], "admitted")
        res_nocand = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-NO-CAND",
            reviewer_id="argon-rev",
            session_id="sess-argon",
            family="argon",
            model="gemini-4.0-argon-high",
            verdict="SHIP",
            report_path=rep_file,
            report_sha256=hashlib.sha256(rep_file.read_bytes()).hexdigest(),
        )
        self.assertEqual(res_nocand["status"], "refused")
        self.assertEqual(res_nocand["reason_code"], "MISSING_CANDIDATE_REVISION")

        # 3. _check_existing_pr with malformed JSON
        matched, pr_info = workflow._check_existing_pr("invalid json {", "commit_sha")
        self.assertFalse(matched)
        self.assertIsNone(pr_info)

        # 4. _is_token_self_match branch coverage
        self.assertTrue(workflow._is_token_self_match("sess-1", "", "sess-1", ""))
        self.assertTrue(workflow._is_token_self_match("owner-1", "owner-1", "", ""))
        self.assertTrue(workflow._is_token_self_match("model-1", "", "", "model-1"))
        self.assertFalse(workflow._is_token_self_match("other", "owner-1", "sess-1", "model-1"))

        # 5. _check_embedded_account & _check_embedded_dispatch branch coverage
        self.assertFalse(workflow._check_embedded_account({"account": "prod"}, {}, "dev:123"))
        self.assertTrue(workflow._check_embedded_account({"account": "dev"}, {}, "dev:123"))
        self.assertFalse(workflow._check_embedded_dispatch({"dispatch": "disp-2"}, "dispatch-1"))
        self.assertTrue(workflow._check_embedded_dispatch({"dispatch": "dispatch-1"}, "dispatch-1"))

        # 6. _is_same_path fallback exception path
        with mock.patch("pathlib.Path.resolve", side_effect=RuntimeError("boom")):
            self.assertTrue(workflow._is_same_path("same/path", "same/path"))
            self.assertFalse(workflow._is_same_path("path/a", "path/b"))

        # 7. CLI unknown subcommand & error exit in main
        ns = argparse.Namespace(subcommand="bogus")
        res_unknown = workflow._dispatch_cli(ns)
        self.assertEqual(res_unknown["status"], "error")

        with mock.patch.object(workflow, "_dispatch_cli", return_value={"status": "error"}):
            code_err = workflow.main(
                [
                    "admit",
                    "--control-dir",
                    str(self.control_dir),
                    "--work-id",
                    "W-TEST-ERR",
                    "--project",
                    "proj-r4",
                    "--repository",
                    "owner/repo",
                    "--task-id",
                    "TASK-R4",
                    "--base-sha",
                    self.base_sha,
                    "--approval-ref",
                    str(dummy_dir),
                ]
            )
            self.assertEqual(code_err, 1)


class TestRound5Findings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.control_dir.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)
        self.base_sha = _init_git_repo(self.worktree)
        self.spec_file = self.worktree / "tasks.md"
        self.spec_file.write_text("- [ ] TASK-R5\n", encoding="utf-8")
        self.directive_file = self.root / "directive.md"
        self.directive_file.write_text("# Directive R5\nApproved work item.\n", encoding="utf-8")
        self.directive_sha = hashlib.sha256(self.directive_file.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_candidate(self, work_id="W-R5"):
        workflow.admit_work(
            control_dir=self.control_dir,
            work_id=work_id,
            project="proj-r5",
            repository="owner/repo",
            task_id="TASK-R5",
            base_sha=self.base_sha,
            approval_ref=str(self.directive_file),
            required_completion_action="push",
            spec_path=self.spec_file,
        )
        claim_res = workflow.claim_work(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-impl",
            worktree=self.worktree,
            write_paths=["src/"],
            allowed_roots=[str(self.root)],
            session_id="sess-flash-r5",
            model="gemini-3.8-flash-high",
        )
        c, t, p = _create_commit(self.worktree, "src/code.py", "def r5(): pass\n", "feat: r5 commit")
        workflow.record_implementation(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-impl",
            claim_token=claim_res["claim_token"],
            round=1,
            commit_sha=c,
            tree_sha=t,
            parent_sha=p,
            gate_results={"test_exit_code": 0},
        )
        return work_id, claim_res["claim_token"], c, t

    def test_r5_arbitrary_model_name_rejected(self):
        work_id, claim_tok, c, t = self._setup_candidate("W-R5-MODEL")
        rep_p = self.root / "rep_bad_model.json"
        rep_p.write_text(
            json.dumps(
                {
                    "commit_sha": c,
                    "tree_sha": t,
                    "verdict": "SHIP",
                    "model": "argon-by-anyone",
                    "dispatch": "disp-bad",
                }
            ),
            encoding="utf-8",
        )
        rep_h = hashlib.sha256(rep_p.read_bytes()).hexdigest()
        res = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-argon",
            model="argon-by-anyone",
            family="argon",
            session_id="sess-argon-1",
            verdict="SHIP",
            report_path=rep_p,
            report_sha256=rep_h,
        )
        self.assertEqual(res["status"], "refused")
        self.assertIn(res["reason_code"], ("REPORT_IDENTITY_MISMATCH", "UNAPPROVED_REVIEWER_MODEL"))

    def test_r5_model_without_report_provenance_rejected(self):
        work_id, claim_tok, c, t = self._setup_candidate("W-R5-PROV")
        rep_p = self.root / "rep_no_prov.json"
        rep_p.write_text(
            json.dumps(
                {
                    "commit_sha": c,
                    "tree_sha": t,
                    "verdict": "SHIP",
                    "model": "claude-opus-5-5",
                }
            ),
            encoding="utf-8",
        )
        rep_h = hashlib.sha256(rep_p.read_bytes()).hexdigest()
        res = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-opus-1",
            verdict="SHIP",
            report_path=rep_p,
            report_sha256=rep_h,
        )
        self.assertEqual(res["status"], "refused")
        self.assertIn(res["reason_code"], ("REPORT_IDENTITY_MISMATCH", "UNAPPROVED_REVIEWER_MODEL"))

    def test_r5_stored_approval_pins_64_hex_sha256(self):
        res = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-R5-PIN",
            project="proj-r5",
            repository="owner/repo",
            task_id="TASK-R5",
            base_sha=self.base_sha,
            approval_ref=str(self.directive_file),
            required_completion_action="push",
            spec_path=self.spec_file,
        )
        self.assertEqual(res["status"], "admitted")
        doc = workflow.load_coordinator_status(self.control_dir)
        wi = doc["delivery_control"]["work_items"]["W-R5-PIN"]
        stored_appr = wi["approval_ref"]
        self.assertIsInstance(stored_appr, dict)
        self.assertEqual(len(stored_appr["sha256"]), 64)
        self.assertEqual(stored_appr["sha256"], self.directive_sha)

        directives = doc["delivery_control"]["directives"]
        self.assertTrue(any(d.get("sha256") == self.directive_sha for d in directives))

        # Short prefix in approval_ref should be rejected
        res_short = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-R5-SHORT",
            project="proj-r5",
            repository="owner/repo",
            task_id="TASK-R5",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}#{self.directive_sha[:8]}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res_short["status"], "refused")
        self.assertEqual(res_short["reason_code"], "STALE_APPROVAL")

    def test_r5_modified_directive_stale_approval(self):
        work_id, claim_tok, c, t = self._setup_candidate("W-R5-STALE")
        # Qualify work item
        for fam in ("argon", "opus", "sol"):
            model = FAM_CANONICAL_MODELS[fam]
            rep_p = self.root / f"rep_{fam}.json"
            rep_p.write_text(
                json.dumps(
                    {
                        "commit_sha": c,
                        "tree_sha": t,
                        "verdict": "SHIP",
                        "model": model,
                        "dispatch": f"disp-{fam}",
                    }
                ),
                encoding="utf-8",
            )
            rep_h = hashlib.sha256(rep_p.read_bytes()).hexdigest()
            res = workflow.record_review(
                control_dir=self.control_dir,
                work_id=work_id,
                reviewer_id=f"rev-{fam}",
                model=model,
                family=fam,
                session_id=f"sess-{fam}",
                verdict="SHIP",
                report_path=rep_p,
                report_sha256=rep_h,
            )
            self.assertEqual(res["status"], "recorded")

        # Mutate directive on disk without revocation keywords
        self.directive_file.write_text("# Directive R5 modified\nNew edits.\n", encoding="utf-8")
        res_trans = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_tok,
            command_runner=lambda cmd, cwd=None: (0, ""),
        )
        self.assertEqual(res_trans["status"], "refused")
        self.assertEqual(res_trans["reason_code"], "STALE_APPROVAL")

    def test_r5_provider_ci_evidence_only_merge_pr(self):
        c = "a" * 40
        # publish_pr does not count as provider CI
        transitions_pub = [{"action": "publish_pr", "commit_sha": c, "evidence": "pr-1"}]
        has_ci_pub, ev_pub = workflow._find_provider_ci_evidence(transitions_pub, c)
        self.assertFalse(has_ci_pub)
        self.assertEqual(ev_pub, [])

        # merge_pr does count as provider CI
        transitions_merge = [{"action": "merge_pr", "commit_sha": c, "evidence": "pr-1"}]
        has_ci_merge, ev_merge = workflow._find_provider_ci_evidence(transitions_merge, c)
        self.assertTrue(has_ci_merge)
        self.assertEqual(ev_merge, ["pr-1"])

    def test_r5_project_work_item_clears_reviews_on_remediation(self):
        work_id, claim_tok, c1, t1 = self._setup_candidate("W-R5-REMED")
        # Sol reviews R1 with REVISE
        rep_p = self.root / "rep_sol_r1.json"
        rep_p.write_text(
            json.dumps(
                {
                    "commit_sha": c1,
                    "tree_sha": t1,
                    "verdict": "REVISE",
                    "model": "gpt-6.1-sol-high",
                    "dispatch": "disp-sol",
                }
            ),
            encoding="utf-8",
        )
        rep_h = hashlib.sha256(rep_p.read_bytes()).hexdigest()
        workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-sol",
            model="gpt-6.1-sol-high",
            family="sol",
            session_id="sess-sol",
            verdict="REVISE",
            report_path=rep_p,
            report_sha256=rep_h,
        )
        doc1 = workflow.load_coordinator_status(self.control_dir)
        p_row1 = doc1["delivery_control"]["projects"]["proj-r5"]
        self.assertEqual(len(p_row1["reviews"]), 1)

        # Flash records Round 2 implementation
        subprocess.run(["git", "reset", "--hard", self.base_sha], cwd=self.worktree, check=True)
        c2, t2, p2 = _create_commit(self.worktree, "src/code.py", "def r5_v2(): pass\n", "feat: r5 r2")
        res2 = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id=work_id,
            owner="flash-impl",
            claim_token=claim_tok,
            round=2,
            commit_sha=c2,
            tree_sha=t2,
            parent_sha=p2,
            gate_results={"test_exit_code": 0},
        )
        self.assertEqual(res2["status"], "recorded")
        doc2 = workflow.load_coordinator_status(self.control_dir)
        p_row2 = doc2["delivery_control"]["projects"]["proj-r5"]
        # Must be empty on Round 2 before new reviews are recorded
        self.assertEqual(p_row2["reviews"], [])


class TestRound7Findings(TestRound5Findings):
    """Pin all R5, R6, and R7 reviewer findings: AS-WM-R5-SOL61-001..008, WFM5-01..04, W-20261103T0950Z-guardgap."""

    def test_r7_sol61_001_bot_actor_checks_login_never_display_name(self):
        # 1. Human provider account with bot display name must fail
        human_fake_bot = json.dumps(
            {
                "login": "human-review-control",
                "name": "selamy-labs-bot",
                "email": "human@example.com",
                "type": "User",
            }
        )

        def runner_fake_bot(cmd, cwd=None):
            return 0, human_fake_bot

        ok, code = workflow._verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=runner_fake_bot)
        self.assertFalse(ok)
        self.assertEqual(code, "DISALLOWED_MERGE_IDENTITY")

        # 2. Ordinary human login (e.g. pselamy) must fail
        human_user = json.dumps({"login": "pselamy", "name": "Patrick Selamy", "type": "User"})

        def runner_human(cmd, cwd=None):
            return 0, human_user

        ok, code = workflow._verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=runner_human)
        self.assertFalse(ok)
        self.assertEqual(code, "DISALLOWED_MERGE_IDENTITY")

        # 3. Approved bot login passes
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "Any Name", "type": "User"})

        def runner_bot(cmd, cwd=None):
            return 0, bot_user

        ok, code = workflow._verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=runner_bot)
        self.assertTrue(ok)
        self.assertEqual(code, "")

        # 4. GitHub Actions bot passes
        gh_bot = json.dumps({"login": "github-actions[bot]", "name": "github-actions[bot]", "type": "Bot"})

        def runner_gh_bot(cmd, cwd=None):
            return 0, gh_bot

        ok, code = workflow._verify_provider_bot_actor(self.worktree, "github-actions[bot]", runner=runner_gh_bot)
        self.assertTrue(ok)
        self.assertEqual(code, "")

    def test_r7_sol61_002_admission_approval_drift_refused_not_repinned(self):
        # Caller supplies expected digest; if file on disk differs, refuse with STALE_APPROVAL
        work_id = "W-R7-DRIFT"
        wrong_sha = "0" * 64
        res = workflow.admit_work(
            control_dir=self.control_dir,
            work_id=work_id,
            project="proj-r7",
            repository="owner/repo",
            task_id="TASK-R5",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{wrong_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res["status"], "refused")
        self.assertIn(res["reason_code"], ("STALE_APPROVAL", "APPROVAL_DIGEST_MISMATCH"))

        # Also when directive is modified on disk to have withdrawal terms
        withdrawn_dir = self.root / "withdrawn_dir.md"
        withdrawn_dir.write_text("# Directives\nREVISED DIRECTIVE - DO NOT MERGE\n", encoding="utf-8")
        withdrawn_sha = hashlib.sha256(withdrawn_dir.read_bytes()).hexdigest()
        res_withdrawn = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-R7-WITHDRAWN",
            project="proj-r7",
            repository="owner/repo",
            task_id="TASK-R5",
            base_sha=self.base_sha,
            approval_ref=f"{withdrawn_dir}:{withdrawn_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res_withdrawn["status"], "refused")
        self.assertEqual(res_withdrawn["reason_code"], "APPROVAL_WITHDRAWN")

    def test_r7_sol61_002_lock_contention_drift_refused(self):
        work_id = "W-R7-LOCK-DRIFT"
        dir_file = self.root / "lock_drift.md"
        dir_file.write_text("# Directive initial\n", encoding="utf-8")
        orig_sha = hashlib.sha256(dir_file.read_bytes()).hexdigest()

        orig_validate = workflow._validate_admit_params

        def mutate_before_lock(*args, **kwargs):
            res = orig_validate(*args, **kwargs)
            dir_file.write_text("# Directive silently drifted by concurrent process\n", encoding="utf-8")
            return res

        import unittest.mock as mock

        with mock.patch.object(
            workflow,
            "_validate_admit_params",
            side_effect=mutate_before_lock,
        ):
            res = workflow.admit_work(
                control_dir=self.control_dir,
                work_id=work_id,
                project="proj-r7",
                repository="owner/repo",
                task_id="TASK-R5",
                base_sha=self.base_sha,
                approval_ref=f"{dir_file}:{orig_sha}",
                spec_path=self.spec_file,
            )
        self.assertEqual(res["status"], "refused")
        self.assertIn(res["reason_code"], ("STALE_APPROVAL", "APPROVAL_DIGEST_MISMATCH"))

    def test_r7_wfm5_01_approval_projection_never_approved_when_stale_or_withdrawn(self):
        # 1. Non-existent file projects as missing/absent, not approved
        missing_proj = workflow._project_approval(str(self.root / "nonexistent.md"), self.control_dir)
        self.assertNotEqual(missing_proj.get("status"), "approved")

        # 2. None / empty projects as absent
        absent_proj = workflow._project_approval(None, self.control_dir)
        self.assertEqual(absent_proj.get("status"), "absent")

        # 3. Withdrawn directive projects as withdrawn/stale, never approved
        withdrawn_file = self.root / "withdrawn_proj.md"
        withdrawn_file.write_text("# Directives\nWITHDRAWN DIRECTIVE\n", encoding="utf-8")
        w_sha = hashlib.sha256(withdrawn_file.read_bytes()).hexdigest()
        withdrawn_proj = workflow._project_approval({"path": str(withdrawn_file), "sha256": w_sha}, self.control_dir)
        self.assertNotEqual(withdrawn_proj.get("status"), "approved")

        # 4. Content drift projects as stale/invalid, never approved
        stale_file = self.root / "stale_proj.md"
        stale_file.write_text("# Initial directive\n", encoding="utf-8")
        stale_sha = hashlib.sha256(stale_file.read_bytes()).hexdigest()
        stale_file.write_text("# Modified directive without withdrawal\n", encoding="utf-8")
        stale_proj = workflow._project_approval({"path": str(stale_file), "sha256": stale_sha}, self.control_dir)
        self.assertNotEqual(stale_proj.get("status"), "approved")

    def test_r7_sol61_003_wfm5_02_canonical_models_and_embedded_aliases(self):
        # 1. Check _classify_family canonical lookups
        self.assertEqual(workflow._classify_family("gpt-5.4"), "sol")
        self.assertEqual(workflow._classify_family("gpt-5.3-codex"), "sol")
        self.assertEqual(workflow._classify_family("gemini-4.0-argon-high"), "argon")
        self.assertEqual(workflow._classify_family("claude-opus-4-6"), "opus")
        self.assertIsNone(workflow._classify_family("argon-by-anyone"))
        self.assertIsNone(workflow._classify_family("opus-by-anyone"))
        self.assertIsNone(workflow._classify_family("sol-by-anyone"))

        # 2. Embedded report model aliases
        work_id, claim_tok, c, t = self._setup_candidate("W-R7-MODEL-ALIAS")

        # authorModel set to non-canonical string
        rep_p1 = self.root / "rep_alias_bad.json"
        rep_p1.write_text(
            json.dumps(
                {
                    "commit_sha": c,
                    "tree_sha": t,
                    "verdict": "SHIP",
                    "authorModel": "argon-by-anyone",
                    "reviewer_model": "gemini-4.0-argon-high",
                    "dispatch": "disp-1",
                }
            ),
            encoding="utf-8",
        )
        rep_h1 = hashlib.sha256(rep_p1.read_bytes()).hexdigest()
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-argon-1",
            model="gemini-4.0-argon-high",
            family="argon",
            session_id="sess-argon-1",
            verdict="SHIP",
            report_path=rep_p1,
            report_sha256=rep_h1,
        )
        self.assertEqual(res1["status"], "refused")
        self.assertIn(res1["reason_code"], ("REPORT_IDENTITY_MISMATCH", "REVIEWER_SESSION_MISMATCH"))

        # reviewer.model set to non-canonical string
        rep_p2 = self.root / "rep_alias_bad2.json"
        rep_p2.write_text(
            json.dumps(
                {
                    "commit_sha": c,
                    "tree_sha": t,
                    "verdict": "SHIP",
                    "reviewer": {"model": "opus-by-anyone"},
                    "model": "claude-opus-5-5",
                    "dispatch": "disp-2",
                }
            ),
            encoding="utf-8",
        )
        rep_h2 = hashlib.sha256(rep_p2.read_bytes()).hexdigest()
        res2 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus-1",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-opus-1",
            verdict="SHIP",
            report_path=rep_p2,
            report_sha256=rep_h2,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "REPORT_IDENTITY_MISMATCH")

    def test_r7_sol61_004_duplicate_reviewer_session_across_aliases(self):
        work_id, claim_tok, c, t = self._setup_candidate("W-R7-SESS-DUP")

        # Record review 1 with session_id="sess-shared"
        rep_p1 = self.root / "rep_sess_1.json"
        rep_p1.write_text(
            json.dumps(
                {
                    "commit_sha": c,
                    "tree_sha": t,
                    "verdict": "SHIP",
                    "model": "gemini-4.0-argon-high",
                    "session_id": "sess-shared",
                    "dispatch": "disp-1",
                }
            ),
            encoding="utf-8",
        )
        rep_h1 = hashlib.sha256(rep_p1.read_bytes()).hexdigest()
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-argon",
            model="gemini-4.0-argon-high",
            family="argon",
            session_id="sess-shared",
            verdict="SHIP",
            report_path=rep_p1,
            report_sha256=rep_h1,
        )
        self.assertEqual(res1["status"], "recorded")

        # Record review 2 with distinct caller session_id="sess-other", but embedded conversationId="sess-shared"
        rep_p2 = self.root / "rep_sess_2.json"
        rep_p2.write_text(
            json.dumps(
                {
                    "commit_sha": c,
                    "tree_sha": t,
                    "verdict": "SHIP",
                    "model": "claude-opus-5-5",
                    "conversationId": "sess-shared",
                    "dispatch": "disp-2",
                }
            ),
            encoding="utf-8",
        )
        rep_h2 = hashlib.sha256(rep_p2.read_bytes()).hexdigest()

        # Case A: Caller session differs from embedded conversationId -> REPORT_IDENTITY_MISMATCH
        res_mismatch = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-other",
            verdict="SHIP",
            report_path=rep_p2,
            report_sha256=rep_h2,
        )
        self.assertEqual(res_mismatch["status"], "refused")
        self.assertIn(res_mismatch["reason_code"], ("REPORT_IDENTITY_MISMATCH", "REVIEWER_SESSION_MISMATCH"))

        # Case B: Caller session matches embedded conversationId,
        # but sess-shared is already used -> DUPLICATE_REVIEWER_SESSION
        res2 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus",
            model="claude-opus-5-5",
            family="opus",
            session_id="sess-shared",
            verdict="SHIP",
            report_path=rep_p2,
            report_sha256=rep_h2,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "DUPLICATE_REVIEWER_SESSION")

    def _qualify_candidate_triad(self, work_id: str, c: str, t: str, prefix: str = "q") -> None:
        for fam in ("argon", "opus", "sol"):
            model = FAM_CANONICAL_MODELS[fam]
            rep_p = self.root / f"rep_{prefix}_{fam}.json"
            rep_p.write_text(
                json.dumps(
                    {
                        "commit_sha": c,
                        "tree_sha": t,
                        "verdict": "SHIP",
                        "model": model,
                        "dispatch": f"disp-{prefix}-{fam}",
                    }
                ),
                encoding="utf-8",
            )
            rep_h = hashlib.sha256(rep_p.read_bytes()).hexdigest()
            workflow.record_review(
                control_dir=self.control_dir,
                work_id=work_id,
                reviewer_id=f"rev-{prefix}-{fam}",
                model=model,
                family=fam,
                session_id=f"sess-{prefix}-{fam}",
                verdict="SHIP",
                report_path=rep_p,
                report_sha256=rep_h,
            )

    def test_r7_sol61_005_queued_merge_refused_open_state(self):
        work_id, claim_tok, c, t = self._setup_candidate("W-R7-Q1")
        self._qualify_candidate_triad(work_id, c, t, prefix="q1")
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_queued(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "OPEN",
                        "headRefOid": c,
                        "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
                        "mergedAt": None,
                        "mergeCommit": None,
                    }
                )
            if "gh pr merge" in cmd_str:
                return 0, "PR #1 queued for merge"
            return 0, ""

        m_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_queued,
        )
        self.assertEqual(m_res["status"], "refused")
        self.assertEqual(m_res["reason_code"], "MERGE_NOT_COMPLETED")

    def test_r7_sol61_005_queued_merge_refused_empty_merged_at(self):
        work_id, claim_tok, c, t = self._setup_candidate("W-R7-Q2")
        self._qualify_candidate_triad(work_id, c, t, prefix="q2")
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_no_merged_at(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": c,
                        "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
                        "mergedAt": "",
                        "mergeCommit": {"oid": "a" * 40},
                    }
                )
            return 0, ""

        m_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_no_merged_at,
        )
        self.assertEqual(m_res["status"], "refused")
        self.assertEqual(m_res["reason_code"], "MERGE_NOT_COMPLETED")

    def test_r7_sol61_005_queued_merge_refused_short_oid(self):
        work_id, claim_tok, c, t = self._setup_candidate("W-R7-Q3")
        self._qualify_candidate_triad(work_id, c, t, prefix="q3")
        bot_user = json.dumps({"login": "selamy-labs-bot", "name": "selamy-labs-bot"})

        def runner_short_oid(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "api" in cmd and "user" in cmd:
                return 0, bot_user
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": c,
                        "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
                        "mergedAt": "2026-11-03T10:00:00Z",
                        "mergeCommit": {"oid": "abc1234"},
                    }
                )
            return 0, ""

        m_res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_short_oid,
        )
        self.assertEqual(m_res["status"], "refused")
        self.assertEqual(m_res["reason_code"], "MERGE_NOT_COMPLETED")

    def test_r7_sol61_006_allowed_workspace_root_containment(self):
        # Worktree sharing string prefix but outside root (sibling directory)
        allowed_root = self.root / "allowed"
        allowed_root.mkdir(parents=True, exist_ok=True)
        sibling_worktree = self.root / "allowed-sibling"
        sibling_worktree.mkdir(parents=True, exist_ok=True)

        res = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-R7",
            owner="flash-impl",
            worktree=sibling_worktree,
            write_paths=["src/"],
            allowed_roots=[str(allowed_root)],
            session_id="sess-flash-sibling",
        )
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason_code"], "WORKSPACE_OUTSIDE_ALLOWED_ROOT")

    def test_r7_sol61_007_no_renames_git_diff_scope_guard(self):
        # Verify that _inspect_modified_paths includes --no-renames
        recorded_cmds = []

        def diff_runner(cmd, cwd=None):
            recorded_cmds.append(list(cmd))
            return 0, "src/code.py\n"

        ok, err = workflow._inspect_modified_paths(
            self.worktree, self.base_sha, "a" * 40, "owner/repo", True, ["src/"], runner=diff_runner
        )
        self.assertTrue(ok)
        # Check that git diff command was executed with --no-renames
        git_diff_calls = [c for c in recorded_cmds if "diff" in c]
        self.assertTrue(len(git_diff_calls) > 0)
        for c in git_diff_calls:
            self.assertIn("--no-renames", c)

    def test_r7_sol61_008_single_commit_descends_from_base(self):
        # Candidate commit whose ancestry fails or parent != base_sha
        def divergent_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str:
                if "HEAD^1" in cmd_str or "HEAD^ " in cmd_str or cmd_str.endswith("HEAD^"):
                    return 0, "d" * 40  # Different parent
                return 0, "a" * 40
            if "merge-base" in cmd_str:
                return 1, ""  # Not an ancestor
            if "rev-list" in cmd_str:
                return 0, "1"
            return 0, ""

        ok, err = workflow._inspect_git_commit(
            self.worktree, "a" * 40, "a" * 40, self.base_sha, self.base_sha, runner=divergent_runner
        )
        self.assertFalse(ok)
        self.assertEqual(err, "BASE_SHA_DRIFT")

    def test_r7_wfm5_04_mutant_matrix(self):
        # 1. 8-hex vs 64-hex prefix collision
        res_short = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-R7-SHORT",
            project="proj-r7",
            repository="owner/repo",
            task_id="TASK-R5",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{self.directive_sha[:8]}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res_short["status"], "refused")
        self.assertEqual(res_short["reason_code"], "STALE_APPROVAL")

        # 2. Non-canonical model name rejected
        rep_fake = self.root / "rep_fake.json"
        rep_fake.write_text(
            json.dumps(
                {
                    "commit_sha": self.base_sha,
                    "tree_sha": self.base_sha,
                    "verdict": "SHIP",
                    "model": "claude-opus-fake",
                    "dispatch": "disp-fake",
                }
            ),
            encoding="utf-8",
        )
        fake_sha = hashlib.sha256(rep_fake.read_bytes()).hexdigest()
        res_bad_model = workflow.record_review(
            control_dir=self.control_dir,
            work_id="W-R7",
            reviewer_id="rev-fake",
            model="claude-opus-fake",
            family="opus",
            session_id="sess-fake",
            verdict="SHIP",
            report_path=rep_fake,
            report_sha256=fake_sha,
        )
        self.assertEqual(res_bad_model["status"], "refused")
        self.assertIn(res_bad_model["reason_code"], ("REPORT_IDENTITY_MISMATCH", "UNAPPROVED_REVIEWER_MODEL"))

    def test_r7_additional_coverage_branches(self):
        # 1. _check_rep_model_family
        self.assertTrue(workflow._check_rep_model_family({"model": "gpt-5.4"}, "sol"))
        self.assertFalse(workflow._check_rep_model_family({"model": "gpt-5.4"}, "opus"))

        # 2. _verify_active_claim branches
        dt_now = datetime.datetime.now(datetime.UTC)
        wi_no_exp = {"claim": {"owner": "lane-1", "claim_token": "tok-1"}}
        self.assertEqual(workflow._verify_active_claim(wi_no_exp, "lane-1", "tok-1", dt_now), (False, "CLAIM_EXPIRED"))
        wi_bad_exp = {"claim": {"owner": "lane-1", "claim_token": "tok-1", "expires_at": "invalid-date"}}
        self.assertEqual(workflow._verify_active_claim(wi_bad_exp, "lane-1", "tok-1", dt_now), (False, "CLAIM_EXPIRED"))
        wi_past_exp = {"claim": {"owner": "lane-1", "claim_token": "tok-1", "expires_at": "2020-01-01T00:00:00+00:00"}}
        self.assertEqual(
            workflow._verify_active_claim(wi_past_exp, "lane-1", "tok-1", dt_now), (False, "CLAIM_EXPIRED")
        )

        # 3. _verify_mandatory_gates
        self.assertTrue(workflow._verify_mandatory_gates({"tests": {"status": "passed"}}))
        self.assertTrue(workflow._verify_mandatory_gates({"pytest": {"exit_code": 0}}))
        self.assertFalse(workflow._verify_mandatory_gates({"tests": {"status": "failed"}}))
        self.assertFalse(workflow._verify_mandatory_gates(None))

        # 4. _verify_artifact_hashes
        self.assertFalse(workflow._verify_artifact_hashes([(str(self.root / "missing.txt"), "a" * 64)]))

        # 5. _extract_caller_ci_checks
        self.assertEqual(workflow._extract_caller_ci_checks({"status": "failing"}), (False, []))
        chks = [{"name": "test", "status": "SUCCESS"}]
        self.assertEqual(workflow._extract_caller_ci_checks({"checks": chks}), (None, chks))

        # 6. _check_project_row_ci
        c = "a" * 40
        self.assertEqual(workflow._check_project_row_ci({"ci": {"revision": c, "status": "failing"}}, c), (False, []))
        ok, res = workflow._check_project_row_ci({"ci": {"revision": c, "status": "passing"}}, c)
        self.assertTrue(ok)
        self.assertEqual(len(res), 1)

        # 7. _query_live_pr_checks
        def pr_view_runner(cmd, cwd=None):
            return 0, json.dumps({"headRefOid": c, "statusCheckRollup": [{"name": "ci"}]})

        self.assertEqual(len(workflow._query_live_pr_checks(pr_view_runner, self.worktree, "1", c)), 1)

        # 8. Failing project row CI refuses merge_pr with CI_CHECK_FAILED
        work_id, claim_tok, c_cand, t_cand = self._setup_candidate("W-R7-CI-FAIL")
        for fam in ("argon", "opus", "sol"):
            model = FAM_CANONICAL_MODELS[fam]
            rep_p = self.root / f"rep_cifail_{fam}.json"
            rep_p.write_text(
                json.dumps(
                    {
                        "commit_sha": c_cand,
                        "tree_sha": t_cand,
                        "verdict": "SHIP",
                        "model": model,
                        "dispatch": f"disp-{fam}",
                    }
                ),
                encoding="utf-8",
            )
            h = hashlib.sha256(rep_p.read_bytes()).hexdigest()
            workflow.record_review(
                control_dir=self.control_dir,
                work_id=work_id,
                reviewer_id=f"rev-{fam}",
                model=model,
                family=fam,
                session_id=f"sess-{fam}",
                verdict="SHIP",
                report_path=rep_p,
                report_sha256=h,
            )
        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["projects"]["proj-r5"]["ci"] = {"revision": c_cand, "status": "failing"}
        workflow.save_coordinator_status(self.control_dir, doc)

        def fail_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            return 0, ""

        res_fail = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=fail_runner,
        )
        self.assertEqual(res_fail["status"], "refused")
        self.assertEqual(res_fail["reason_code"], "CI_CHECK_FAILED")


class TestRound8Findings(TestRound4Findings):
    def test_r8_sol61_001_session_alias_normalization(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R8-SESS")

        # 1. reviewer_conversation_id mismatched against caller session_id is refused with REVIEWER_SESSION_MISMATCH
        p1, h1 = self._write_report(
            {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "claude-opus-5-5",
                "reviewer_conversation_id": "shared-conv",
                "dispatch": "disp-1",
            }
        )
        res1 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus-1",
            model="claude-opus-5-5",
            family="opus",
            session_id="caller-conv",
            verdict="SHIP",
            report_path=p1,
            report_sha256=h1,
        )
        self.assertEqual(res1["status"], "refused")
        self.assertEqual(res1["reason_code"], "REVIEWER_SESSION_MISMATCH")

        # 2. Simultaneous conflicting nested aliases
        # (reviewer={"session_id": "s1", "session": "shared"}) is refused with REVIEWER_SESSION_MISMATCH
        p2, h2 = self._write_report(
            {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "claude-opus-5-5",
                "reviewer": {"session_id": "s1", "session": "shared"},
                "dispatch": "disp-2",
            }
        )
        res2 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus-2",
            model="claude-opus-5-5",
            family="opus",
            session_id="s1",
            verdict="SHIP",
            report_path=p2,
            report_sha256=h2,
        )
        self.assertEqual(res2["status"], "refused")
        self.assertEqual(res2["reason_code"], "REVIEWER_SESSION_MISMATCH")

        # 3. Report session aliases disagree with each other (session_id="s1" matches caller,
        # but conversation_id="s2" also present in same report) is refused with REVIEWER_SESSION_MISMATCH
        p3, h3 = self._write_report(
            {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "claude-opus-5-5",
                "session_id": "s1",
                "conversation_id": "s2",
                "dispatch": "disp-3",
            }
        )
        res3 = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-opus-3",
            model="claude-opus-5-5",
            family="opus",
            session_id="s1",
            verdict="SHIP",
            report_path=p3,
            report_sha256=h3,
        )
        self.assertEqual(res3["status"], "refused")
        self.assertEqual(res3["reason_code"], "REVIEWER_SESSION_MISMATCH")
        self.assertFalse(workflow._check_embedded_session({"session_id": "s1", "conversation_id": "s2"}, {}, "s1"))

    def test_r8_sol61_002_caller_ci_checks_missing_sha_refused(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R8-CI", req_action="publish_pr")
        self._qualify_work_item(work_id, c, t)

        def dummy_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git ls-remote" in cmd_str:
                return 0, f"{c} refs/heads/feat/{work_id}"
            if "gh pr view" in cmd_str:
                return 0, json.dumps({"state": "OPEN", "number": 1, "headRefOid": c, "url": "https://github.com/pr/1"})
            return 0, ""

        # Publish PR to meet prerequisite
        res_pub = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="publish_pr",
            claim_token=claim_tok,
            command_runner=dummy_runner,
        )
        self.assertEqual(res_pub["status"], "transitioned")

        # Caller-supplied CI check with no SHA field must fail closed with CI_CHECK_FAILED
        res_comp = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="complete_spec_task",
            claim_token=claim_tok,
            ci_checks=[{"name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            command_runner=dummy_runner,
        )
        self.assertEqual(res_comp["status"], "refused")
        self.assertEqual(res_comp["reason_code"], "CI_CHECK_FAILED")

        # Verify _fetch_ci_checks_from_provider stamps head_sha when matching PR headRefOid
        def provider_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "gh pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "headRefOid": c,
                        "statusCheckRollup": [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                    }
                )
            return 0, ""

        fetched = workflow._fetch_ci_checks_from_provider("owner/repo", 1, c, runner=provider_runner)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched[0]["head_sha"], c)

    def test_r8_sol61_003_post_merge_head_ref_oid_binding(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R8-MERGE", req_action="merge_pr")
        self._qualify_work_item(work_id, c, t)

        d = "d" * 40
        view_calls = []
        merge_called = []

        def runner_adv(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "api" in cmd and "user" in cmd:
                return 0, json.dumps({"login": "selamy-labs-bot"})
            if "gh pr merge" in cmd_str:
                merge_called.append(True)
                return 0, "PR merged"
            if "gh pr view" in cmd_str:
                view_calls.append(True)
                if len(view_calls) == 1:
                    # Pre-merge: OPEN with headRefOid=C
                    return 0, json.dumps(
                        {
                            "state": "OPEN",
                            "headRefOid": c,
                            "statusCheckRollup": [{"status": "SUCCESS", "conclusion": "SUCCESS", "head_sha": c}],
                        }
                    )
                # Post-merge: MERGED with mergedAt, valid mergeCommit, but headRefOid=D (D != C)
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "mergedAt": "2026-11-03T11:00:00Z",
                        "mergeCommit": {"oid": "e" * 40},
                        "headRefOid": d,
                    }
                )
            return 0, ""

        res_merge = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner_adv,
        )
        self.assertEqual(res_merge["status"], "refused")
        self.assertEqual(res_merge["reason_code"], "CANDIDATE_REVISION_MISMATCH")
        self.assertTrue(merge_called)

        # Subsequent complete_spec_task must also be refused
        res_comp = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="complete_spec_task",
            claim_token=claim_tok,
            command_runner=runner_adv,
        )
        self.assertEqual(res_comp["status"], "refused")
        self.assertEqual(res_comp["reason_code"], "MISSING_TRANSITION_EVIDENCE")

    def test_r8_wfm5_03_and_04_notes(self):
        # WFM8-01: Verify bot-only identities (neither portfolio-bot, pselamy, nor dev are in bot allowlists)
        for unapproved in ("portfolio-bot", "pselamy", "dev"):
            self.assertNotIn(unapproved, workflow.ALLOWED_MERGE_IDENTITIES)
            self.assertNotIn(f"{unapproved} <{unapproved}@selamy.dev>", workflow.APPROVED_BOT_IDENTITIES)
            self.assertNotIn(f"{unapproved}@selamy.dev", workflow.APPROVED_BOT_EMAILS)
            self.assertNotIn((unapproved, f"{unapproved}@selamy.dev"), workflow.APPROVED_BOT_PAIRS)

        # WFM8-01: Calling execute_transition(action="merge_pr", merge_identity="pselamy")
        # with gh api user returning {login: "pselamy", type: "User"} is refused
        res_merge_human = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id="W-R8-MISMATCH",
            action="merge_pr",
            claim_token="claim-tok-mismatch",
            merge_identity="pselamy",
            command_runner=lambda cmd, cwd=None: (0, json.dumps({"login": "pselamy", "type": "User"})),
        )
        self.assertEqual(res_merge_human["status"], "refused")
        self.assertIn(res_merge_human["reason_code"], {"DISALLOWED_MERGE_IDENTITY", "MERGE_IDENTITY_VIOLATION"})

        # WFM5-04 Item 1: 64-hex expected approval digest sharing 8-char prefix
        # with live digest refused with APPROVAL_DIGEST_MISMATCH
        colliding_sha = self.directive_sha[:8] + "0" * 56
        res_mismatch = workflow.admit_work(
            control_dir=self.control_dir,
            work_id="W-R8-MISMATCH",
            project="proj-r8",
            repository="owner/repo",
            task_id="TASK-R5",
            base_sha=self.base_sha,
            approval_ref=f"{self.directive_file}:{colliding_sha}",
            spec_path=self.spec_file,
        )
        self.assertEqual(res_mismatch["status"], "refused")
        self.assertEqual(res_mismatch["reason_code"], "APPROVAL_DIGEST_MISMATCH")

        # WFM5-04 Item 2 & WFM8-02: Stored approval ref {"path": ...} missing sha256 refused with STALE_APPROVAL
        work_id_ns, claim_tok_ns, c_ns, t_ns = self._setup_admitted_candidate("W-R8-NOSHA")
        self._qualify_work_item(work_id_ns, c_ns, t_ns)
        doc = workflow.load_coordinator_status(self.control_dir)
        doc["delivery_control"]["work_items"][work_id_ns]["approval_ref"] = {"path": str(self.directive_file)}
        workflow.save_coordinator_status(self.control_dir, doc)

        def runner_no_sha(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "rev-parse HEAD^{tree}" in cmd_str:
                return 0, t_ns
            if "rev-parse HEAD" in cmd_str:
                return 0, c_ns
            return 0, ""

        res_no_sha = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id_ns,
            action="push",
            claim_token=claim_tok_ns,
            command_runner=runner_no_sha,
        )
        self.assertEqual(res_no_sha["status"], "refused")
        self.assertEqual(res_no_sha["reason_code"], "STALE_APPROVAL")

        # WFM5-04 Item 3: Non-canonical caller model containing family substring
        # is refused with UNAPPROVED_REVIEWER_MODEL
        p3, h3 = self._write_report(
            {
                "commit_sha": c_ns,
                "tree_sha": t_ns,
                "verdict": "SHIP",
                "model": "claude-opus-5-5",
                "dispatch": "disp-3",
            }
        )
        res_bad_model = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id_ns,
            reviewer_id="rev-custom",
            model="custom-opus-wrapper",
            family="opus",
            session_id="sess-custom",
            verdict="SHIP",
            report_path=p3,
            report_sha256=h3,
        )
        self.assertEqual(res_bad_model["status"], "refused")
        self.assertEqual(res_bad_model["reason_code"], "UNAPPROVED_REVIEWER_MODEL")

        # WFM5-04 Item 4: Candidate commit whose HEAD^1 equals caller-claimed parent_sha
        # but differs from admitted base_sha is refused with PARENT_BASE_MISMATCH
        parent_diff = "p" * 40

        def runner_parent(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "HEAD^{tree}" in cmd_str:
                return 0, t_ns
            if "HEAD^1" in cmd_str:
                return 0, parent_diff
            if "rev-parse HEAD" in cmd_str:
                return 0, c_ns
            return 0, ""

        ok_p, err_p = workflow._verify_candidate_parent(self.worktree, parent_diff, self.base_sha, runner=runner_parent)
        self.assertFalse(ok_p)
        self.assertEqual(err_p, "PARENT_BASE_MISMATCH")

        res_impl = workflow.record_implementation(
            control_dir=self.control_dir,
            work_id=work_id_ns,
            owner="flash-impl",
            claim_token=claim_tok_ns,
            round=2,
            commit_sha=c_ns,
            tree_sha=t_ns,
            parent_sha=parent_diff,
            gate_results={"test_exit_code": 0},
            git_runner=runner_parent,
        )
        self.assertEqual(res_impl["status"], "refused")
        self.assertEqual(res_impl["reason_code"], "PARENT_BASE_MISMATCH")


class TestRound9Findings(TestRound4Findings):
    def test_r9_sol61_001_nested_conversationId_shared_refused(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R9-SESS")

        # Three distinct canonical reviewer models with distinct caller sessions,
        # but nested reviewer.conversationId set to "shared-conversation".
        # Each must be refused with REVIEWER_SESSION_MISMATCH.
        reviewers = [
            ("rev-argon", "gemini-4-argon-high", "argon", "sess-argon-1"),
            ("rev-opus", "claude-opus-5-5", "opus", "sess-opus-2"),
            ("rev-sol", "gpt-6.1-sol-high", "sol", "sess-sol-3"),
        ]
        for rev_id, model, fam, caller_sess in reviewers:
            rep_path, rep_sha = self._write_report(
                {
                    "commit_sha": c,
                    "tree_sha": t,
                    "verdict": "SHIP",
                    "model": model,
                    "reviewer": {
                        "session_id": caller_sess,
                        "conversationId": "shared-conversation",
                    },
                    "dispatch": f"disp-{fam}",
                }
            )
            res = workflow.record_review(
                control_dir=self.control_dir,
                work_id=work_id,
                reviewer_id=rev_id,
                model=model,
                family=fam,
                session_id=caller_sess,
                verdict="SHIP",
                report_path=rep_path,
                report_sha256=rep_sha,
            )
            self.assertEqual(res["status"], "refused")
            self.assertEqual(res["reason_code"], "REVIEWER_SESSION_MISMATCH")

        doc = workflow.load_coordinator_status(self.control_dir)
        self.assertFalse(doc["delivery_control"]["work_items"][work_id].get("qualified", False))

    def test_r9_sol61_002_default_runner_queries_provider_ci_on_path(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R9-DEF-RUNNER", req_action="publish_pr")
        self._qualify_work_item(work_id, c, t)

        shim_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(shim_dir, ignore_errors=True))
        gh_shim = shim_dir / "gh"
        invocations_file = shim_dir / "invocations.txt"

        gh_script_content = f"""#!/bin/sh
echo "$@" >> "{invocations_file}"
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
    echo '{{"headRefOid": "{c}", "statusCheckRollup": ['
    echo '{{"name": "test-ci", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": "{c}"}}]}}'
    exit 0
fi
exit 0
"""
        gh_shim.write_text(gh_script_content, encoding="utf-8")
        gh_shim.chmod(0o755)

        # Publish PR to meet prerequisite for complete_spec_task
        def publish_runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git ls-remote" in cmd_str:
                return 0, f"{c} refs/heads/feat/{work_id}"
            if "gh pr view" in cmd_str:
                return 0, json.dumps({"state": "OPEN", "number": 1, "headRefOid": c, "url": "https://github.com/pr/1"})
            return 0, ""

        res_pub = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="publish_pr",
            claim_token=claim_tok,
            command_runner=publish_runner,
        )
        self.assertEqual(res_pub["status"], "transitioned")

        # Execute complete_spec_task with default runner (command_runner=None) and ci_checks=None
        orig_path = os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": f"{shim_dir}:{orig_path}"}):
            res_comp = workflow.execute_transition(
                control_dir=self.control_dir,
                work_id=work_id,
                action="complete_spec_task",
                claim_token=claim_tok,
                command_runner=None,
                ci_checks=None,
            )
            self.assertEqual(res_comp["status"], "transitioned")
            self.assertEqual(res_comp["action"], "complete_spec_task")
            self.assertTrue(invocations_file.exists())
            self.assertIn("pr view", invocations_file.read_text(encoding="utf-8"))

    def test_r9_sol61_003_cached_ci_failure_does_not_block_retry(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R9-RETRY", req_action="push")
        self._qualify_work_item(work_id, c, t)

        def runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git ls-remote" in cmd_str:
                return 0, ""
            if "git push" in cmd_str:
                return 0, "pushed"
            if "api" in cmd and "user" in cmd:
                return 0, json.dumps({"login": "selamy-labs-bot"})
            if "gh pr view" in cmd_str:
                rollup_item = {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c}
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": c,
                        "mergeCommit": {"oid": "a" * 40},
                        "mergedAt": "2026-11-03T12:00:00Z",
                        "statusCheckRollup": [rollup_item],
                    }
                )
            return 0, ""

        # 0. Execute push to satisfy complete_spec_task prerequisite
        res_push = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_tok,
            command_runner=runner,
        )
        self.assertEqual(res_push["status"], "transitioned")

        # 1. Caller check missing SHA fails complete_spec_task
        res_comp_fail = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="complete_spec_task",
            claim_token=claim_tok,
            ci_checks=[{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            command_runner=runner,
        )
        self.assertEqual(res_comp_fail["status"], "refused")
        self.assertEqual(res_comp_fail["reason_code"], "CI_CHECK_FAILED")

        # Verify project row CI was not permanently poisoned as failing
        doc = workflow.load_coordinator_status(self.control_dir)
        p_row = doc["delivery_control"]["projects"].get("proj-r8", {})
        p_ci = p_row.get("ci")
        if p_ci is not None:
            self.assertNotEqual(p_ci.get("status"), "failing")

        # 2. Subsequent merge_pr with passing exact-SHA caller checks succeeds
        valid_caller_checks = [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c}]
        res_merge = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            ci_checks=valid_caller_checks,
            command_runner=runner,
        )
        self.assertEqual(res_merge["status"], "transitioned")
        self.assertEqual(res_merge["action"], "merge_pr")

        # 3. Subsequent complete_spec_task with valid checks also succeeds
        res_comp_retry = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="complete_spec_task",
            claim_token=claim_tok,
            ci_checks=valid_caller_checks,
            command_runner=runner,
        )
        self.assertEqual(res_comp_retry["status"], "transitioned")
        self.assertEqual(res_comp_retry["action"], "complete_spec_task")


class TestRound10Findings(TestRound4Findings):
    def test_r10_wfm9_01_sol61_001_recursive_session_scan_refuses_aliases(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-SESS")

        # Test various nested and aliased session keys causing mismatch
        cases = [
            ("rev-argon", "gemini-4-argon-high", "argon", "sess-argon", {"sessionId": "shared-conversation"}),
            (
                "rev-opus",
                "claude-opus-5-5",
                "opus",
                "sess-opus",
                {"reviewer": {"sessionId": "shared-conversation"}},
            ),
            (
                "rev-sol",
                "gpt-6.1-sol-high",
                "sol",
                "sess-sol",
                {"metadata": {"conversation_id": "shared-conversation"}},
            ),
            (
                "rev-argon2",
                "gemini-4-argon-high",
                "argon",
                "sess-a2",
                {"trace": [{"userSession": "shared-conversation"}]},
            ),
        ]
        for rev_id, model, fam, caller_sess, extra in cases:
            rep_data = {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": model,
                "session_id": caller_sess,
                "dispatch": f"disp-{fam}",
                **extra,
            }
            rep_path, rep_sha = self._write_report(rep_data)
            res = workflow.record_review(
                control_dir=self.control_dir,
                work_id=work_id,
                reviewer_id=rev_id,
                model=model,
                family=fam,
                session_id=caller_sess,
                verdict="SHIP",
                report_path=rep_path,
                report_sha256=rep_sha,
            )
            self.assertEqual(res["status"], "refused")
            self.assertEqual(res["reason_code"], "REVIEWER_SESSION_MISMATCH")

    def test_r10_wfm9_01_sol61_001_non_string_or_empty_session_refused(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-BADSESS")

        for bad_val in (12345, None, "", "   "):
            rep_data = {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "gemini-4-argon-high",
                "sessionId": bad_val,
                "dispatch": "disp-argon",
            }
            rep_path, rep_sha = self._write_report(rep_data)
            res = workflow.record_review(
                control_dir=self.control_dir,
                work_id=work_id,
                reviewer_id="rev-argon-bad",
                model="gemini-4-argon-high",
                family="argon",
                session_id="valid-session",
                verdict="SHIP",
                report_path=rep_path,
                report_sha256=rep_sha,
            )
            self.assertEqual(res["status"], "refused")
            self.assertEqual(res["reason_code"], "REVIEWER_SESSION_MISMATCH")

    def test_r10_wfm9_01_sol61_001_valid_nested_matching_sessions_pass(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-VALIDSESS")
        caller_sess = "sess-argon-valid"
        rep_data = {
            "commit_sha": c,
            "tree_sha": t,
            "verdict": "SHIP",
            "model": "gemini-4-argon-high",
            "sessionId": caller_sess,
            "reviewer": {"sessionId": caller_sess},
            "metadata": {"conversation_id": caller_sess},
            "dispatch": "disp-argon",
        }
        rep_path, rep_sha = self._write_report(rep_data)
        res = workflow.record_review(
            control_dir=self.control_dir,
            work_id=work_id,
            reviewer_id="rev-argon-ok",
            model="gemini-4-argon-high",
            family="argon",
            session_id=caller_sess,
            verdict="SHIP",
            report_path=rep_path,
            report_sha256=rep_sha,
        )
        self.assertEqual(res["status"], "recorded")
        self.assertEqual(res["verdict"], "SHIP")

    def test_r10_sol61_002_remote_parameter_threaded_through_push(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-REMOTE", req_action="push")
        self._qualify_work_item(work_id, c, t)

        cmds_run: list[list[str]] = []

        def mock_remote_runner(cmd, cwd=None):
            cmds_run.append(list(cmd))
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git ls-remote" in cmd_str:
                return 0, ""
            if "git push" in cmd_str:
                return 0, "pushed to requested remote"
            return 0, ""

        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="push",
            claim_token=claim_tok,
            remote="requested",
            command_runner=mock_remote_runner,
        )
        self.assertEqual(res["status"], "transitioned")

        # Verify 'requested' remote was passed to ls-remote and push
        ls_cmds = [cmd for cmd in cmds_run if "ls-remote" in cmd]
        push_cmds = [cmd for cmd in cmds_run if "push" in cmd]
        self.assertTrue(len(ls_cmds) >= 1)
        self.assertTrue(len(push_cmds) >= 1)
        self.assertIn("requested", ls_cmds[0])
        self.assertNotIn("origin", ls_cmds[0])
        self.assertIn("requested", push_cmds[0])
        self.assertNotIn("origin", push_cmds[0])

    def test_r10_sol61_003_publish_pr_remote_head_mismatch_refused(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-PUBPR-1", req_action="publish_pr")
        self._qualify_work_item(work_id, c, t)

        def runner_remote_mismatch(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "gh pr view" in cmd_str:
                return 1, "no pull requests found"
            if "git ls-remote" in cmd_str:
                return 0, f"{'d' * 40} refs/heads/feat/{work_id}"
            return 0, ""

        res_bad_remote = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="publish_pr",
            claim_token=claim_tok,
            command_runner=runner_remote_mismatch,
        )
        self.assertEqual(res_bad_remote["status"], "refused")
        self.assertEqual(res_bad_remote["reason_code"], "REMOTE_HEAD_MISMATCH")

    def _dispatch_publish_gh(self, cmd_str: str, head_oid: str, calls: list[int]) -> tuple[int, str]:
        if "gh pr view" in cmd_str:
            if calls[0] == 0:
                calls[0] += 1
                return 1, "no pull requests found"
            return 0, json.dumps({"state": "OPEN", "number": 1, "headRefOid": head_oid, "url": "https://pr/1"})
        if "gh pr create" in cmd_str:
            return 0, "https://github.com/pull/1"
        return 0, ""

    def _make_publish_runner(self, c: str, work_id: str, head_oid: str):
        calls = [0]

        def runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git ls-remote" in cmd_str:
                return 0, f"{c} refs/heads/feat/{work_id}"
            return self._dispatch_publish_gh(cmd_str, head_oid, calls)

        return runner

    def test_r10_sol61_003_publish_pr_created_oid_mismatch_refused(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-PUBPR-2", req_action="publish_pr")
        self._qualify_work_item(work_id, c, t)

        runner = self._make_publish_runner(c, work_id, "f" * 40)
        res_bad_head = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="publish_pr",
            claim_token=claim_tok,
            command_runner=runner,
        )
        self.assertEqual(res_bad_head["status"], "refused")
        self.assertEqual(res_bad_head["reason_code"], "PR_HEAD_MISMATCH")

    def test_r10_sol61_003_publish_pr_matching_heads_succeed(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-PUBPR-3", req_action="publish_pr")
        self._qualify_work_item(work_id, c, t)

        runner = self._make_publish_runner(c, work_id, c)
        res_ok = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="publish_pr",
            claim_token=claim_tok,
            command_runner=runner,
        )
        self.assertEqual(res_ok["status"], "transitioned")

    def test_r10_sol61_004_cross_repo_same_write_path_concurrency_allowed(self):
        wt_a = self.root / "wt_a"
        wt_b = self.root / "wt_b"
        wt_a2 = self.root / "wt_a2"
        wt_a.mkdir(parents=True)
        wt_b.mkdir(parents=True)
        wt_a2.mkdir(parents=True)

        self.spec_file.write_text(
            "- [ ] TASK-W-R10-REPOA-1\n- [ ] TASK-W-R10-REPOB-1\n- [ ] TASK-W-R10-REPOA-2\n",
            encoding="utf-8",
        )

        for wid, proj, repo in [
            ("W-R10-REPOA-1", "proj-a", "owner/repoA"),
            ("W-R10-REPOB-1", "proj-b", "owner/repoB"),
            ("W-R10-REPOA-2", "proj-a", "owner/repoA"),
        ]:
            workflow.admit_work(
                control_dir=self.control_dir,
                work_id=wid,
                project=proj,
                repository=repo,
                task_id=f"TASK-{wid}",
                base_sha=self.base_sha,
                approval_ref=f"{self.directive_file}:{self.directive_sha}",
                spec_path=self.spec_file,
            )

        # Claim 1 in repo A with write_paths=["src/"]
        res1 = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-R10-REPOA-1",
            owner="lane-a",
            worktree=str(wt_a),
            write_paths=["src/"],
            allowed_roots=[str(self.root)],
            session_id="sess-a",
        )
        self.assertEqual(res1["status"], "claimed")

        # Claim 2 in repo B with identical write_paths=["src/"] must be ALLOWED concurrently
        res2 = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-R10-REPOB-1",
            owner="lane-b",
            worktree=str(wt_b),
            write_paths=["src/"],
            allowed_roots=[str(self.root)],
            session_id="sess-b",
        )
        self.assertEqual(res2["status"], "claimed")

        # Claim 3 in repo A with overlapping write_paths=["src/sub/"] must be REFUSED
        res3 = workflow.claim_work(
            control_dir=self.control_dir,
            work_id="W-R10-REPOA-2",
            owner="lane-c",
            worktree=str(wt_a2),
            write_paths=["src/sub/"],
            allowed_roots=[str(self.root)],
            session_id="sess-c",
        )
        self.assertEqual(res3["status"], "refused")
        self.assertEqual(res3["reason_code"], "WRITER_CONFLICT")

    def test_r10_sol61_005_merge_pr_does_not_fabricate_branch_protection(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-BP", req_action="merge_pr")
        self._qualify_work_item(work_id, c, t)

        # Initially, project branch protection is not verified
        doc_init = workflow.load_coordinator_status(self.control_dir)
        p_row_init = doc_init["delivery_control"]["projects"]["proj-r4"]
        self.assertFalse(p_row_init.get("branch_protection", {}).get("verified", False))

        def runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "api" in cmd and "user" in cmd:
                return 0, json.dumps({"login": "selamy-labs-bot"})
            if "pr view" in cmd_str:
                rollup = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c}]
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": c,
                        "mergeCommit": {"oid": "b" * 40},
                        "mergedAt": "2026-11-03T12:00:00Z",
                        "statusCheckRollup": rollup,
                    }
                )
            return 0, ""

        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner,
        )
        self.assertEqual(res["status"], "transitioned")

        # After merge_pr, branch_protection must NOT be fabricated as verified=True
        doc_after = workflow.load_coordinator_status(self.control_dir)
        p_row_after = doc_after["delivery_control"]["projects"]["proj-r4"]
        self.assertFalse(p_row_after.get("branch_protection", {}).get("verified", False))

    def test_r10_sol61_005_merge_pr_preserves_verified_branch_protection(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R10-BP-VERIF", req_action="merge_pr")
        self._qualify_work_item(work_id, c, t)

        doc_verif = workflow.load_coordinator_status(self.control_dir)
        doc_verif["delivery_control"]["projects"]["proj-r4"]["branch_protection"] = {
            "verified": True,
            "evidence": ["existing_ruleset.json"],
        }
        workflow.save_coordinator_status(self.control_dir, doc_verif)

        def runner(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git rev-parse" in cmd_str:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if "git diff" in cmd_str:
                return 0, ""
            if "api" in cmd and "user" in cmd:
                return 0, json.dumps({"login": "selamy-labs-bot"})
            if "pr view" in cmd_str:
                rollup = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c}]
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": c,
                        "mergeCommit": {"oid": "c" * 40},
                        "mergedAt": "2026-11-03T12:00:00Z",
                        "statusCheckRollup": rollup,
                    }
                )
            return 0, ""

        res = workflow.execute_transition(
            control_dir=self.control_dir,
            work_id=work_id,
            action="merge_pr",
            claim_token=claim_tok,
            command_runner=runner,
        )
        self.assertEqual(res["status"], "transitioned")

        doc_final = workflow.load_coordinator_status(self.control_dir)
        p_row_final = doc_final["delivery_control"]["projects"]["proj-r4"]
        self.assertTrue(p_row_final["branch_protection"]["verified"])
        self.assertIn("existing_ruleset.json", p_row_final["branch_protection"]["evidence"])
        self.assertTrue(any("transition" in ev for ev in p_row_final["branch_protection"]["evidence"]))


class TestRound11Findings(TestRound4Findings):
    def test_r11_sol61_001_session_extraction_and_verification(self):
        cases = [
            ("matching_nested", {"metadata": [{"reviewer": {"sessionId": "caller"}}]}, True),
            ("nested_mismatch", {"metadata": [{"reviewer": {"sessionId": "other"}}]}, False),
            ("findings_mismatch", {"findings": [{"reviewerSessionId": "other"}]}, False),
            ("nested_findings_mismatch", {"metadata": {"findings": [{"conversationId": "other"}]}}, False),
            ("scalar_list_mismatch", {"reviewerSessions": ["other"]}, False),
            ("nested_scalar_list_mismatch", {"metadata": {"conversationIds": [["other"]]}}, False),
            ("scalar_list_number", {"reviewerSessions": [123]}, False),
            ("scalar_list_null", {"reviewerSessions": [None]}, False),
            ("scalar_list_blank", {"reviewerSessions": [" "]}, False),
            ("scalar_list_empty", {"reviewerSessions": []}, False),
            ("list_dict_mismatch", {"metadata": [{"conversationId": "other"}]}, False),
            ("scalar_number", {"sessionId": 123}, False),
        ]
        for label, extra, expected in cases:
            report = {"session_id": "caller", **extra}
            valid = workflow._verify_report_session(report, {"session_id": "caller"}, "caller")
            self.assertEqual(valid, expected, f"Failed for case: {label}")

        receipt_cases = [
            {"session_id": "caller", "reviewerSessionId": "other"},
            {"session_id": "caller", "metadata": {"conversation_id": "other"}},
            {"session_id": "caller", "sessionId": None},
            {"session_id": None},
        ]
        for rec in receipt_cases:
            valid = workflow._verify_report_session({"session_id": "caller"}, rec, "caller")
            self.assertFalse(valid, f"Receipt case should be invalid: {rec}")

    def test_r11_sol61_001_record_review_refuses_nested_findings_and_scalar_lists(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R11-REVIEWS")
        cases = [
            ("findings", {"findings": [{"reviewerSessionId": "other"}]}),
            ("scalar_list", {"reviewerSessions": ["other"]}),
            ("scalar_number", {"reviewerSessions": [123]}),
            ("scalar_null", {"reviewerSessions": [None]}),
            ("scalar_blank", {"reviewerSessions": [" "]}),
        ]
        for label, extra in cases:
            rep_data = {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": "gemini-4-argon-high",
                "session_id": "caller",
                "dispatch": "disp-argon",
                **extra,
            }
            p, h = self._write_report(rep_data)
            outcome = workflow.record_review(
                self.control_dir,
                work_id,
                "rev-argon",
                "gemini-4-argon-high",
                "argon",
                "caller",
                "SHIP",
                p,
                h,
            )
            self.assertEqual(outcome["status"], "refused", f"Expected refused for {label}")
            self.assertEqual(outcome["reason_code"], "REVIEWER_SESSION_MISMATCH", f"Expected mismatch for {label}")

    def test_r11_sol61_001_findings_quorum_bypass_prevented(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R11-Q-FINDINGS")
        extra = {"findings": [{"conversationId": "shared-conversation"}]}
        for fam, model in FAM_CANONICAL_MODELS.items():
            caller = f"caller-{fam}"
            rep_data = {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": model,
                "session_id": caller,
                "dispatch": f"disp-{fam}",
                **extra,
            }
            p, h = self._write_report(rep_data)
            res = workflow.record_review(
                self.control_dir,
                work_id,
                f"rev-{fam}",
                model,
                fam,
                caller,
                "SHIP",
                p,
                h,
            )
            self.assertEqual(res["status"], "refused")
            self.assertEqual(res["reason_code"], "REVIEWER_SESSION_MISMATCH")

        wi = workflow.load_coordinator_status(self.control_dir)["delivery_control"]["work_items"][work_id]
        self.assertFalse(wi.get("qualified", False))
        self.assertNotEqual(wi.get("stage"), "merge_ready")

    def test_r11_sol61_001_scalar_list_quorum_bypass_prevented(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R11-Q-SCALARS")
        extra = {"reviewerSessions": ["shared-conversation"]}
        for fam, model in FAM_CANONICAL_MODELS.items():
            caller = f"caller-{fam}"
            rep_data = {
                "commit_sha": c,
                "tree_sha": t,
                "verdict": "SHIP",
                "model": model,
                "session_id": caller,
                "dispatch": f"disp-{fam}",
                **extra,
            }
            p, h = self._write_report(rep_data)
            res = workflow.record_review(
                self.control_dir,
                work_id,
                f"rev-{fam}",
                model,
                fam,
                caller,
                "SHIP",
                p,
                h,
            )
            self.assertEqual(res["status"], "refused")
            self.assertEqual(res["reason_code"], "REVIEWER_SESSION_MISMATCH")

        wi = workflow.load_coordinator_status(self.control_dir)["delivery_control"]["work_items"][work_id]
        self.assertFalse(wi.get("qualified", False))
        self.assertNotEqual(wi.get("stage"), "merge_ready")

    def test_r11_sol61_002_exact_ref_matching_with_real_bare_remote_stale(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R11-EXACT-STALE", req_action="publish_pr")
        self._qualify_work_item(work_id, c, t)
        bare = self.root / "requested_stale.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "requested", str(bare)], cwd=self.worktree, check=True)
        branch = f"feat/{work_id}"
        # Set exact branch to base_sha (stale) and colliding suffix branch to candidate sha
        subprocess.run(
            ["git", "push", "requested", f"{self.base_sha}:refs/heads/{branch}"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "requested", f"{c}:refs/heads/a/{branch}"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        )

        # _get_remote_branch_sha must return the exact branch SHA (base_sha), not candidate c
        remote_sha = workflow._get_remote_branch_sha(self.worktree, "requested", branch)
        self.assertEqual(remote_sha, self.base_sha)

        calls = []

        def runner(cmd, cwd=None):
            calls.append(list(cmd))
            if cmd[0] == "git":
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if cmd[:3] == ["gh", "pr", "view"]:
                return 1, "not found"
            if cmd[:3] == ["gh", "pr", "create"]:
                raise AssertionError(f"gh pr create should not be invoked on stale remote head: {cmd}")
            return 0, ""

        # publish_pr must refuse before gh pr create
        pub_res = workflow.execute_transition(
            self.control_dir,
            work_id,
            "publish_pr",
            claim_token=claim_tok,
            remote="requested",
            branch=branch,
            command_runner=runner,
        )
        self.assertEqual(pub_res["status"], "refused")
        self.assertEqual(pub_res["reason_code"], "REMOTE_HEAD_MISMATCH")
        self.assertFalse(any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls))

        # push must actually push to refs/heads/{branch} instead of falsely reconciling
        push_res = workflow.execute_transition(
            self.control_dir,
            work_id,
            "push",
            claim_token=claim_tok,
            remote="requested",
            branch=branch,
            command_runner=runner,
        )
        self.assertEqual(push_res["status"], "transitioned")
        self.assertFalse(push_res["reconciled_existing_effect"])
        self.assertTrue(any(cmd[:2] == ["git", "push"] for cmd in calls))

        # After push, remote head is updated to candidate c
        remote_sha_after = workflow._get_remote_branch_sha(self.worktree, "requested", branch)
        self.assertEqual(remote_sha_after, c)

    def test_r11_sol61_002_exact_ref_matching_with_real_bare_remote_missing(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R11-EXACT-MISSING", req_action="publish_pr")
        self._qualify_work_item(work_id, c, t)
        bare = self.root / "requested_missing.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "requested_missing", str(bare)], cwd=self.worktree, check=True)
        branch = f"feat/{work_id}"
        # Only push colliding suffix branch; requested branch is absent
        subprocess.run(
            ["git", "push", "requested_missing", f"{c}:refs/heads/a/{branch}"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        )

        remote_sha = workflow._get_remote_branch_sha(self.worktree, "requested_missing", branch)
        self.assertIsNone(remote_sha)

        calls = []

        def runner(cmd, cwd=None):
            calls.append(list(cmd))
            if cmd[0] == "git":
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if cmd[:3] == ["gh", "pr", "view"]:
                return 1, "not found"
            if cmd[:3] == ["gh", "pr", "create"]:
                raise AssertionError(f"gh pr create should not be invoked when remote branch is absent: {cmd}")
            return 0, ""

        pub_res = workflow.execute_transition(
            self.control_dir,
            work_id,
            "publish_pr",
            claim_token=claim_tok,
            remote="requested_missing",
            branch=branch,
            command_runner=runner,
        )
        self.assertEqual(pub_res["status"], "refused")
        self.assertEqual(pub_res["reason_code"], "REMOTE_HEAD_MISMATCH")
        self.assertFalse(any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls))

        push_res = workflow.execute_transition(
            self.control_dir,
            work_id,
            "push",
            claim_token=claim_tok,
            remote="requested_missing",
            branch=branch,
            command_runner=runner,
        )
        self.assertEqual(push_res["status"], "transitioned")
        self.assertFalse(push_res["reconciled_existing_effect"])
        self.assertTrue(any(cmd[:2] == ["git", "push"] for cmd in calls))

        remote_sha_after = workflow._get_remote_branch_sha(self.worktree, "requested_missing", branch)
        self.assertEqual(remote_sha_after, c)

    def test_r11_sol61_003_existing_closed_pr_refuses_and_blocks_completion(self):
        work_id, claim_tok, c, t = self._setup_admitted_candidate("W-R11-PUB-CLOSED", req_action="publish_pr")
        self._qualify_work_item(work_id, c, t)

        calls = []

        def runner(cmd, cwd=None):
            calls.append(list(cmd))
            if cmd[:2] in (["git", "rev-parse"], ["git", "diff"]):
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
                return p.returncode, p.stdout.strip()
            if cmd[:3] == ["git", "ls-remote", "--heads"]:
                return 0, f"{c}\trefs/heads/feat/{work_id}"
            if cmd[:3] == ["gh", "pr", "view"]:
                return 0, json.dumps(
                    {
                        "state": "CLOSED",
                        "headRefOid": c,
                        "number": 17,
                        "url": "https://example.invalid/pr/17",
                    }
                )
            return 0, ""

        pub_res = workflow.execute_transition(
            self.control_dir,
            work_id,
            "publish_pr",
            claim_token=claim_tok,
            command_runner=runner,
        )
        self.assertEqual(pub_res["status"], "refused")
        self.assertEqual(pub_res["reason_code"], "PR_NOT_OPEN")

        # Now complete_spec_task must refuse because publication transition was blocked
        comp_res = workflow.execute_transition(
            self.control_dir,
            work_id,
            "complete_spec_task",
            claim_token=claim_tok,
            ci_checks=[{"status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": c}],
            command_runner=runner,
        )
        self.assertEqual(comp_res["status"], "refused")
        self.assertEqual(comp_res["reason_code"], "MISSING_TRANSITION_EVIDENCE")

        # Spec checkbox remains unchecked
        spec_content = self.spec_file.read_text(encoding="utf-8")
        self.assertIn("- [ ]", spec_content)
        self.assertNotIn("- [x]", spec_content)

        # Stage remains merge_ready, not done
        wi = workflow.load_coordinator_status(self.control_dir)["delivery_control"]["work_items"][work_id]
        self.assertNotEqual(wi.get("stage"), "done")

    def test_r11_sol61_003_check_existing_pr_direct_eval(self):
        c = "a" * 40
        # Closed PR string
        matched, pr_info = workflow._check_existing_pr(
            json.dumps({"state": "CLOSED", "headRefOid": c, "number": 1, "url": "https://pr/1"}),
            c,
        )
        self.assertFalse(matched)
        self.assertEqual(pr_info, "PR_NOT_OPEN")

        # Merged PR string without evidence
        matched, pr_info = workflow._check_existing_pr(
            json.dumps({"state": "MERGED", "headRefOid": c, "number": 2, "url": "https://pr/2"}),
            c,
        )
        self.assertFalse(matched)
        self.assertEqual(pr_info, "PR_NOT_OPEN")

        # Open PR matching
        matched, pr_info = workflow._check_existing_pr(
            json.dumps({"state": "OPEN", "headRefOid": c, "number": 3, "url": "https://pr/3"}),
            c,
        )
        self.assertTrue(matched)
        self.assertEqual(pr_info.get("pr_number"), 3)

        # Open PR mismatching sha
        matched, pr_info = workflow._check_existing_pr(
            json.dumps({"state": "OPEN", "headRefOid": "b" * 40, "number": 4, "url": "https://pr/4"}),
            c,
        )
        self.assertFalse(matched)
        self.assertIsNone(pr_info)

        # Repo dir invocation with runner returning closed
        def closed_runner(cmd, cwd=None):
            return 0, json.dumps({"state": "CLOSED", "headRefOid": c, "number": 5, "url": "https://pr/5"})

        matched, pr_info = workflow._check_existing_pr(self.worktree, "feat/branch", c, runner=closed_runner)
        self.assertFalse(matched)
        self.assertEqual(pr_info, "PR_NOT_OPEN")

        # Repo dir invocation with runner returning open
        def open_runner(cmd, cwd=None):
            return 0, json.dumps({"state": "OPEN", "headRefOid": c, "number": 6, "url": "https://pr/6"})

        matched, pr_info = workflow._check_existing_pr(self.worktree, "feat/branch", c, runner=open_runner)
        self.assertTrue(matched)
        self.assertEqual(pr_info.get("pr_number"), 6)
