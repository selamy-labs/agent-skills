#!/usr/bin/env python3
"""Behavioral tests for git and GitHub CLI provider adapters."""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from providers import (  # noqa: E402
    _check_app_matches,
    _extract_repo_required_checks,
    _is_named_check_passing,
    _post_merge_verify,
    _read_policy_dict,
    _run_cmd,
    _verify_created_pr,
    _verify_merge_pr_view,
    check_existing_pr,
    fetch_ci_checks_from_provider,
    fetch_commit_check_runs_from_provider,
    fetch_pr_base_branch,
    fetch_required_checks_from_provider,
    get_commit_modified_paths,
    get_remote_branch_sha,
    has_app_pinned_checks,
    inspect_git_commit,
    is_approved_identity,
    is_ci_check_passing,
    is_hex_40,
    is_hex_64,
    is_policy_authoritative,
    iso_timestamp,
    load_policy_required_checks,
    parse_required_checks,
    reconcile_and_complete_spec_task,
    reconcile_and_merge_pr,
    reconcile_and_publish_pr,
    reconcile_and_push,
    resolve_required_checks,
    sha256_file,
    stamp_rollup_head_sha,
    verify_live_status_rollup,
    verify_provider_bot_actor,
)


def _init_git_repo(repo_dir: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "selamy-labs-bot"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "346154065+selamy-labs-bot@users.noreply.github.com"],
        cwd=repo_dir,
        check=True,
    )
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo_dir, check=True)
    f = repo_dir / "README.md"
    f.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _make_app_pinned_merge_runner(c_sha: str, merged: list[bool], fail_check_runs: bool = False) -> Any:
    def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
        cmd_str = " ".join(cmd)
        if fail_check_runs and "check-runs" in cmd_str:
            return 1, "failed check-runs"
        if "api user" in cmd_str:
            return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
        if "required_status_checks" in cmd_str:
            return 0, json.dumps({"checks": [{"context": "Quality", "app_id": 15368}]})
        if "rules/branches" in cmd_str:
            return 0, json.dumps([])
        if "check-runs" in cmd_str:
            return 0, json.dumps(
                {
                    "total_count": 1,
                    "check_runs": [
                        {
                            "name": "Quality",
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": c_sha,
                            "app": {"id": 15368},
                        }
                    ],
                }
            )
        if "pr view" in cmd_str:
            if merged[0]:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": c_sha,
                        "baseRefName": "main",
                        "mergedAt": "2026-11-03T18:00:00Z",
                        "mergeCommit": {"oid": "4" * 40},
                    }
                )
            return 0, json.dumps(
                {
                    "state": "OPEN",
                    "headRefOid": c_sha,
                    "baseRefName": "main",
                    "statusCheckRollup": [{"name": "Quality", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                }
            )
        if "pr merge" in cmd_str:
            merged[0] = True
            return 0, "Merged"
        return 0, ""

    return runner


class TestProviders(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.worktree = Path(self.temp_dir.name)
        self.c_sha = _init_git_repo(self.worktree)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_inspect_git_commit(self) -> None:
        ok, author, committer, tree, _ = inspect_git_commit(self.worktree, self.c_sha)
        self.assertTrue(ok)
        self.assertIn("selamy-labs-bot", author)
        self.assertIn("selamy-labs-bot", committer)
        self.assertEqual(len(tree), 40)

    def test_is_approved_identity(self) -> None:
        self.assertTrue(is_approved_identity("selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>"))
        self.assertTrue(is_approved_identity("claude-cir <claude-cir@selamy.dev>"))
        self.assertFalse(is_approved_identity("unknown-dev <dev@example.com>"))

    def test_get_remote_branch_sha_exact_ref_matching(self) -> None:
        # Mock runner returning exact and suffix-colliding branch refs
        def mock_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            lines = [
                f"{'1' * 40}\trefs/heads/a/feat/W-100",
                f"{'2' * 40}\trefs/heads/feat/W-100",
                f"{'3' * 40}\trefs/heads/feat/W-100/extra",
            ]
            return 0, "\n".join(lines)

        sha = get_remote_branch_sha(self.worktree, "origin", "feat/W-100", runner=mock_runner)
        self.assertEqual(sha, "2" * 40)

    def test_check_existing_pr_strict_json_projection_merged_pr(self) -> None:
        # Strict runner projecting ONLY the requested --json fields (SOL61-002, WFM11-02, ARGON-02)
        pr_store = {
            "number": 152,
            "url": "https://github.com/selamy-labs/agent-skills/pull/152",
            "state": "MERGED",
            "headRefOid": self.c_sha,
            "mergedAt": "2026-11-03T10:00:00Z",
            "mergeCommit": {"oid": "c" * 40},
        }

        def strict_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            self.assertEqual(cmd[:3], ["gh", "pr", "view"])
            self.assertIn("--json", cmd)
            fields_str = cmd[cmd.index("--json") + 1]
            requested_fields = [f.strip() for f in fields_str.split(",")]
            # Required fields MUST be in the requested --json fields
            self.assertIn("mergedAt", requested_fields)
            self.assertIn("mergeCommit", requested_fields)
            filtered = {k: v for k, v in pr_store.items() if k in requested_fields}
            return 0, json.dumps(filtered)

        ok, pr_info = check_existing_pr(self.worktree, "feat/W-100", self.c_sha, runner=strict_runner)
        self.assertTrue(ok)
        self.assertIsInstance(pr_info, dict)
        self.assertTrue(pr_info.get("reconciled_existing_effect"))
        self.assertEqual(pr_info.get("state"), "MERGED")
        self.assertEqual(pr_info.get("merge_commit_sha"), "c" * 40)

    def test_check_existing_pr_fails_closed_when_merged_fields_omitted(self) -> None:
        # If mergedAt or mergeCommit is missing from output, MERGED state must refuse PR_NOT_OPEN
        pr_incomplete = {
            "number": 152,
            "url": "https://github.com/selamy-labs/agent-skills/pull/152",
            "state": "MERGED",
            "headRefOid": self.c_sha,
            # mergedAt and mergeCommit omitted
        }

        def incomplete_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps(pr_incomplete)

        ok, pr_info = check_existing_pr(self.worktree, "feat/W-100", self.c_sha, runner=incomplete_runner)
        self.assertFalse(ok)
        self.assertEqual(pr_info, "PR_NOT_OPEN")

    def test_check_existing_pr_refuses_closed_unmerged_pr(self) -> None:
        pr_closed = {
            "number": 152,
            "url": "https://github.com/selamy-labs/agent-skills/pull/152",
            "state": "CLOSED",
            "headRefOid": self.c_sha,
        }

        def closed_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps(pr_closed)

        ok, pr_info = check_existing_pr(self.worktree, "feat/W-100", self.c_sha, runner=closed_runner)
        self.assertFalse(ok)
        self.assertEqual(pr_info, "PR_NOT_OPEN")

    def test_reconcile_and_push_reconciles_when_matching(self) -> None:
        def matching_remote(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "ls-remote" in cmd:
                return 0, f"{self.c_sha}\trefs/heads/feat/branch\n"
            return 1, "push should not be called"

        ok, res = reconcile_and_push(self.worktree, "origin", "feat/branch", self.c_sha, runner=matching_remote)
        self.assertTrue(ok)
        self.assertTrue(res.get("reconciled_existing_effect"))

    def test_reconcile_and_complete_spec_task(self) -> None:
        spec_file = self.worktree / "spec.md"
        spec_file.write_text("# Spec\n- [ ] TASK-100 First task\n- [ ] TASK-101 Second task\n", encoding="utf-8")
        ok, res = reconcile_and_complete_spec_task(spec_file, "TASK-100")
        self.assertTrue(ok)
        self.assertFalse(res.get("reconciled_existing_effect"))
        self.assertIn("- [x] TASK-100", spec_file.read_text(encoding="utf-8"))

        # Re-running reconciles existing effect
        ok2, res2 = reconcile_and_complete_spec_task(spec_file, "TASK-100")
        self.assertTrue(ok2)
        self.assertTrue(res2.get("reconciled_existing_effect"))

    def test_verify_provider_bot_actor(self) -> None:
        def ok_bot_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})

        ok, err = verify_provider_bot_actor(self.worktree, runner=ok_bot_runner)
        self.assertTrue(ok)
        self.assertEqual(err, "")

        def unapproved_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"login": "random-user", "type": "User"})

        ok_bad, err_bad = verify_provider_bot_actor(self.worktree, runner=unapproved_runner)
        self.assertFalse(ok_bad)
        self.assertEqual(err_bad, "DISALLOWED_MERGE_IDENTITY")

    def test_reconcile_and_merge_pr_already_merged(self) -> None:
        merged_pr_view = {
            "state": "MERGED",
            "mergedAt": "2026-11-03T10:00:00Z",
            "mergeCommit": {"oid": "c" * 40},
            "headRefOid": self.c_sha,
            "statusCheckRollup": [
                {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
            ],
        }

        def merged_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "pr view" in cmd_str:
                return 0, json.dumps(merged_pr_view)
            return 0, ""

        ok, res = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=merged_runner)
        self.assertTrue(ok)
        self.assertTrue(res.get("reconciled_existing_effect"))

    def test_reconcile_and_merge_pr_head_mismatch(self) -> None:
        mismatch_pr_view = {
            "state": "MERGED",
            "mergedAt": "2026-11-03T10:00:00Z",
            "mergeCommit": {"oid": "c" * 40},
            "headRefOid": "e" * 40,
            "statusCheckRollup": [
                {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
            ],
        }

        def mismatch_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr view" in cmd_str:
                return 0, json.dumps(mismatch_pr_view)
            return 0, ""

        ok_mis, res_mis = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=mismatch_runner)
        self.assertFalse(ok_mis)
        self.assertEqual(res_mis.get("reason_code"), "PR_HEAD_MISMATCH")

    def test_reconcile_and_merge_pr_missing_ci(self) -> None:
        no_ci_pr_view = {
            "state": "OPEN",
            "headRefOid": self.c_sha,
            "statusCheckRollup": [],
        }

        def no_ci_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr view" in cmd_str:
                return 0, json.dumps(no_ci_pr_view)
            return 0, ""

        ok_noci, res_noci = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=no_ci_runner)
        self.assertFalse(ok_noci)
        self.assertEqual(res_noci.get("reason_code"), "MISSING_EXACT_HEAD_CI")

    def test_fetch_ci_checks_and_stamp_head_sha(self) -> None:
        raw_checks = [
            {"name": "gate-1", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "gate-2", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        stamp_rollup_head_sha(raw_checks, self.c_sha)
        self.assertEqual(raw_checks[0].get("head_sha"), self.c_sha)
        self.assertEqual(raw_checks[1].get("head_sha"), self.c_sha)

        def view_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "pr view" in " ".join(cmd):
                return 0, json.dumps(
                    {
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": raw_checks,
                    }
                )
            return 1, "error"

        checks = fetch_ci_checks_from_provider("selamy-labs/agent-skills", "152", self.c_sha, runner=view_runner)
        self.assertIsNotNone(checks)
        self.assertEqual(len(checks), 2)

    def test_reconcile_and_push_execution(self) -> None:
        # Push succeeds
        def push_ok_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "ls-remote" in cmd:
                return 0, ""
            if "push" in cmd:
                return 0, "To origin feat/branch"
            return 0, ""

        ok, res = reconcile_and_push(self.worktree, "origin", "feat/branch", self.c_sha, runner=push_ok_runner)
        self.assertTrue(ok)
        self.assertFalse(res.get("reconciled_existing_effect"))

        # Push fails
        def push_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "ls-remote" in cmd:
                return 0, ""
            if "push" in cmd:
                return 1, "remote rejected"
            return 0, ""

        ok_f, res_f = reconcile_and_push(self.worktree, "origin", "feat/branch", self.c_sha, runner=push_fail_runner)
        self.assertFalse(ok_f)
        self.assertIn("error", res_f)

    def test_reconcile_and_publish_pr_existing_pr(self) -> None:
        # 1. Existing open PR
        def open_pr_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "pr view" in " ".join(cmd):
                return 0, json.dumps(
                    {
                        "number": 152,
                        "url": "https://github.com/selamy-labs/agent-skills/pull/152",
                        "state": "OPEN",
                        "headRefOid": self.c_sha,
                    }
                )
            return 0, ""

        ok, res = reconcile_and_publish_pr(
            self.worktree, "feat/branch", "main", self.c_sha, "Title", "Body", runner=open_pr_runner
        )
        self.assertTrue(ok)
        self.assertTrue(res.get("reconciled_existing_effect"))

        # 2. Existing closed PR
        def closed_pr_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "pr view" in " ".join(cmd):
                return 0, json.dumps(
                    {
                        "number": 152,
                        "url": "https://github.com/selamy-labs/agent-skills/pull/152",
                        "state": "CLOSED",
                        "headRefOid": self.c_sha,
                    }
                )
            return 0, ""

        ok_c, res_c = reconcile_and_publish_pr(
            self.worktree, "feat/branch", "main", self.c_sha, "Title", "Body", runner=closed_pr_runner
        )
        self.assertFalse(ok_c)
        self.assertEqual(res_c.get("reason_code"), "PR_NOT_OPEN")

    def test_reconcile_and_publish_pr_remote_head_mismatch(self) -> None:
        def remote_mismatch_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "pr view" in " ".join(cmd):
                return 1, "no PR found"
            if "ls-remote" in " ".join(cmd):
                return 0, f"{'9' * 40}\trefs/heads/feat/branch\n"
            return 0, ""

        ok_m, res_m = reconcile_and_publish_pr(
            self.worktree, "feat/branch", "main", self.c_sha, "Title", "Body", runner=remote_mismatch_runner
        )
        self.assertFalse(ok_m)
        self.assertEqual(res_m.get("reason_code"), "REMOTE_HEAD_MISMATCH")

    def test_reconcile_and_publish_pr_creation_success(self) -> None:
        created = [False]

        def create_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "pr view" in cmd_str:
                if created[0]:
                    return 0, json.dumps(
                        {
                            "number": 152,
                            "url": "https://github.com/selamy-labs/agent-skills/pull/152",
                            "state": "OPEN",
                            "headRefOid": self.c_sha,
                        }
                    )
                return 1, "no PR found"
            if "ls-remote" in cmd_str:
                return 0, f"{self.c_sha}\trefs/heads/feat/branch\n"
            if "pr create" in cmd_str:
                created[0] = True
                return 0, "https://github.com/selamy-labs/agent-skills/pull/152"
            return 0, ""

        ok_cr, res_cr = reconcile_and_publish_pr(
            self.worktree, "feat/branch", "main", self.c_sha, "Title", "Body", runner=create_runner
        )
        self.assertTrue(ok_cr)
        self.assertFalse(res_cr.get("reconciled_existing_effect"))
        self.assertEqual(res_cr.get("pr_number"), 152)

    def test_reconcile_and_merge_pr_execution_and_post_verify(self) -> None:
        merged_state = [False]

        def _flow_pr_view() -> str:
            if merged_state[0]:
                return json.dumps(
                    {
                        "state": "MERGED",
                        "mergedAt": "2026-11-03T10:00:00Z",
                        "mergeCommit": {"oid": "c" * 40},
                        "headRefOid": self.c_sha,
                    }
                )
            return json.dumps(
                {
                    "state": "OPEN",
                    "headRefOid": self.c_sha,
                    "statusCheckRollup": [
                        {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                    ],
                }
            )

        def merge_flow_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "pr view" in cmd_str:
                return 0, _flow_pr_view()
            if "pr merge" in cmd_str:
                merged_state[0] = True
                return 0, "Merged PR #152"
            return 0, ""

        ok, res = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=merge_flow_runner)
        self.assertTrue(ok)
        self.assertEqual(res.get("state"), "MERGED")

    def test_identity_and_hex_validation(self) -> None:
        self.assertTrue(is_hex_40(self.c_sha))
        self.assertFalse(is_hex_40("not-hex-at-all"))
        self.assertFalse(is_hex_40(12345))
        self.assertFalse(is_hex_40("z" * 40))

        self.assertTrue(is_hex_64("a" * 64))
        self.assertFalse(is_hex_64("a" * 40))
        self.assertFalse(is_hex_64(12345))
        self.assertFalse(is_hex_64("z" * 64))

        self.assertTrue(is_approved_identity("selamy-labs-bot"))
        self.assertTrue(is_approved_identity("claude-cir"))
        self.assertFalse(is_approved_identity("unauthorized-user"))
        self.assertFalse(is_approved_identity("selamy-labs-bot <wrong-email@example.com>"))
        self.assertFalse(is_approved_identity("<wrong-email@example.com>"))
        self.assertTrue(is_approved_identity("<346154065+selamy-labs-bot@users.noreply.github.com>"))

    def test_sha256_and_timestamp(self) -> None:
        p = self.worktree / "test_file.txt"
        p.write_bytes(b"hello world")
        digest = sha256_file(p)
        self.assertEqual(len(digest), 64)

        t1 = iso_timestamp()
        self.assertTrue(t1.endswith("Z"))
        dt = datetime.datetime(2026, 11, 3, 12, 0, 0, tzinfo=datetime.UTC)
        t2 = iso_timestamp(dt)
        self.assertEqual(t2, "2026-11-03T12:00:00Z")

    def test_provider_bot_actor_bot_prefix_and_json_error(self) -> None:
        def prefix_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"login": "selamy-labs-bot[bot]", "type": "Bot"})

        ok, err = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=prefix_runner)
        self.assertTrue(ok)

        def bad_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "corrupted json"

        ok2, err2 = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=bad_json_runner)
        self.assertFalse(ok2)
        self.assertEqual(err2, "PROVIDER_AUTH_FAILED")

        def list_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps(["not-a-dict"])

        ok3, err3 = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=list_json_runner)
        self.assertFalse(ok3)
        self.assertEqual(err3, "DISALLOWED_MERGE_IDENTITY")

    def test_run_cmd_error_and_timeout(self) -> None:
        code, out = _run_cmd(["false"], self.worktree)
        self.assertNotEqual(code, 0)

        with patch("providers.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["sleep"], timeout=1)):
            code_t, out_t = _run_cmd(["sleep"], self.worktree)
            self.assertEqual(code_t, 1)
            self.assertIn("timed out", out_t)

        with patch("providers.subprocess.run", side_effect=OSError("permission denied")):
            code_e, out_e = _run_cmd(["foo"], self.worktree)
            self.assertEqual(code_e, 1)
            self.assertIn("permission denied", out_e)

    def test_inspect_git_commit_failures(self) -> None:
        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "fatal: bad object"

        ok, author, committer, tree, _ = inspect_git_commit(self.worktree, self.c_sha, runner=fail_runner)
        self.assertFalse(ok)

        def short_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "only-one-line\n"

        ok2, _, _, _, _ = inspect_git_commit(self.worktree, self.c_sha, runner=short_runner)
        self.assertFalse(ok2)

    def test_get_remote_branch_sha_failures(self) -> None:
        def error_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "network error"

        sha = get_remote_branch_sha(self.worktree, "origin", "feat/branch", runner=error_runner)
        self.assertIsNone(sha)

        def empty_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, ""

        sha2 = get_remote_branch_sha(self.worktree, "origin", "feat/branch", runner=empty_runner)
        self.assertIsNone(sha2)

    def test_check_existing_pr_failures(self) -> None:
        def bad_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not-json"

        ok, err = check_existing_pr(self.worktree, "feat/branch", self.c_sha, runner=bad_json_runner)
        self.assertFalse(ok)
        self.assertIsNone(err)

    def test_verify_provider_bot_actor_variations(self) -> None:
        # Fallback to gh auth status when gh api user fails
        def auth_fallback_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "api user" in " ".join(cmd):
                return 1, "Forbidden"
            if "auth status" in " ".join(cmd):
                return 0, "Logged in to github.com account selamy-labs-bot"
            return 0, ""

        ok, err = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=auth_fallback_runner)
        self.assertTrue(ok)
        self.assertEqual(err, "")

        # Fallback auth fails
        def auth_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "Not logged in"

        ok2, err2 = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=auth_fail_runner)
        self.assertFalse(ok2)
        self.assertEqual(err2, "PROVIDER_AUTH_FAILED")

        # User type is User (not Bot) and login does not match merge_identity
        def user_type_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"login": "external-human", "type": "User"})

        ok3, err3 = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=user_type_runner)
        self.assertFalse(ok3)
        self.assertEqual(err3, "DISALLOWED_MERGE_IDENTITY")

    def test_fetch_ci_checks_from_provider_variations(self) -> None:
        # Exit code != 0
        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "Not found"

        self.assertIsNone(fetch_ci_checks_from_provider(self.worktree, "152", self.c_sha, runner=fail_runner))

        # Bad JSON
        def bad_json(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not json"

        self.assertIsNone(fetch_ci_checks_from_provider(self.worktree, "152", self.c_sha, runner=bad_json))

        # Head OID mismatch
        def head_mismatch(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"headRefOid": "9" * 40, "statusCheckRollup": []})

        self.assertIsNone(fetch_ci_checks_from_provider(self.worktree, "152", self.c_sha, runner=head_mismatch))

        # Missing statusCheckRollup
        def no_rollup(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"headRefOid": self.c_sha, "statusCheckRollup": None})

        self.assertIsNone(fetch_ci_checks_from_provider(self.worktree, "152", self.c_sha, runner=no_rollup))

    def test_verify_created_pr_variations(self) -> None:
        ok1, res1 = _verify_created_pr(1, "Command failed", self.c_sha)
        self.assertFalse(ok1)
        self.assertEqual(res1.get("reason_code"), "CREATED_PR_VIEW_FAILED")

        ok2, res2 = _verify_created_pr(0, "not-json", self.c_sha)
        self.assertFalse(ok2)
        self.assertEqual(res2.get("reason_code"), "PR_HEAD_MISMATCH")

        ok3, res3 = _verify_created_pr(0, json.dumps(["list", "not", "dict"]), self.c_sha)
        self.assertFalse(ok3)
        self.assertEqual(res3.get("reason_code"), "PR_HEAD_MISMATCH")

        ok4, res4 = _verify_created_pr(0, json.dumps({"state": "CLOSED", "headRefOid": self.c_sha}), self.c_sha)
        self.assertFalse(ok4)
        self.assertEqual(res4.get("reason_code"), "PR_HEAD_MISMATCH")

        ok5, res5 = _verify_created_pr(0, json.dumps({"state": "OPEN", "headRefOid": "9" * 40}), self.c_sha)
        self.assertFalse(ok5)
        self.assertEqual(res5.get("reason_code"), "PR_HEAD_MISMATCH")

    def test_ci_check_passing_and_rollup(self) -> None:
        # Non-dict
        self.assertFalse(is_ci_check_passing("not-dict", self.c_sha))

        # Head sha mismatch
        self.assertFalse(is_ci_check_passing({"head_sha": "9" * 40}, self.c_sha))

        # Conclusion failure
        self.assertFalse(is_ci_check_passing({"head_sha": self.c_sha, "conclusion": "FAILURE"}, self.c_sha))
        self.assertFalse(is_ci_check_passing({"head_sha": self.c_sha, "conclusion": "TIMED_OUT"}, self.c_sha))

        # State error
        self.assertFalse(is_ci_check_passing({"head_sha": self.c_sha, "state": "ERROR"}, self.c_sha))

        # Success state
        self.assertTrue(is_ci_check_passing({"head_sha": self.c_sha, "state": "SUCCESS"}, self.c_sha))
        self.assertTrue(
            is_ci_check_passing({"head_sha": self.c_sha, "status": "COMPLETED", "conclusion": "SUCCESS"}, self.c_sha)
        )
        self.assertFalse(
            is_ci_check_passing({"head_sha": self.c_sha, "status": "COMPLETED", "conclusion": "SKIPPED"}, self.c_sha)
        )
        self.assertFalse(
            is_ci_check_passing({"head_sha": self.c_sha, "status": "COMPLETED", "conclusion": "NEUTRAL"}, self.c_sha)
        )

        # verify_live_status_rollup
        self.assertEqual(verify_live_status_rollup([], self.c_sha), (False, "MISSING_EXACT_HEAD_CI"))
        self.assertEqual(verify_live_status_rollup(["not-dict"], self.c_sha), (False, "CI_CHECK_FAILED"))
        self.assertEqual(verify_live_status_rollup([{"state": "ERROR"}], self.c_sha), (False, "CI_CHECK_FAILED"))
        self.assertEqual(
            verify_live_status_rollup([{"state": "SUCCESS", "head_sha": self.c_sha}], self.c_sha), (True, "")
        )

    def test_post_merge_verify_variations(self) -> None:
        # View command failed
        def view_fail(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "view failed"

        ok1, err1, _ = _post_merge_verify(self.worktree, "152", self.c_sha, runner=view_fail)
        self.assertFalse(ok1)
        self.assertEqual(err1, "MERGE_NOT_COMPLETED")

        # Post-info not merged (e.g. queued)
        def queued_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha})

        ok2, err2, _ = _post_merge_verify(self.worktree, "152", self.c_sha, runner=queued_runner)
        self.assertFalse(ok2)
        self.assertEqual(err2, "MERGE_NOT_COMPLETED")

        # Head OID mismatch post-merge
        def head_mismatch_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-11-03T10:00:00Z",
                    "mergeCommit": {"oid": "c" * 40},
                    "headRefOid": "9" * 40,
                }
            )

        ok3, err3, _ = _post_merge_verify(self.worktree, "152", self.c_sha, runner=head_mismatch_runner)
        self.assertFalse(ok3)
        self.assertEqual(err3, "CANDIDATE_REVISION_MISMATCH")

    def test_reconcile_and_complete_spec_task_variations(self) -> None:
        # Missing spec path
        ok1, res1 = reconcile_and_complete_spec_task(None, "TASK_01")
        self.assertFalse(ok1)

        # Nonexistent spec path
        ok2, res2 = reconcile_and_complete_spec_task("/tmp/nonexistent-spec-file.md", "TASK_01")
        self.assertFalse(ok2)

        # Task not found in spec
        spec = self.worktree / "SPEC.md"
        spec.write_text("- [ ] OTHER_TASK: Desc\n", encoding="utf-8")
        ok3, res3 = reconcile_and_complete_spec_task(spec, "TASK_01")
        self.assertFalse(ok3)

        # Task already completed
        spec.write_text("- [x] TASK_01: Desc\n", encoding="utf-8")
        ok4, res4 = reconcile_and_complete_spec_task(spec, "TASK_01")
        self.assertTrue(ok4)
        self.assertTrue(res4.get("reconciled_existing_effect"))

    def test_get_commit_modified_paths(self) -> None:
        # Mock runner with diff-tree output
        def diff_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "file1.py\nfile2.py\n"

        paths = get_commit_modified_paths(self.worktree, self.c_sha, runner=diff_runner)
        self.assertEqual(paths, ["file1.py", "file2.py"])

        # Diff failure
        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "error"

        paths_f = get_commit_modified_paths(self.worktree, self.c_sha, runner=fail_runner)
        self.assertEqual(paths_f, [])

    def test_reconcile_and_publish_pr_create_fails(self) -> None:
        def pr_create_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "pr view" in cmd_str:
                return 1, "no PR found"
            if "ls-remote" in cmd_str:
                return 0, f"{self.c_sha}\trefs/heads/feat/branch\n"
            if "pr create" in cmd_str:
                return 1, "Failed to create PR: GraphQL error"
            return 0, ""

        ok, res = reconcile_and_publish_pr(
            self.worktree, "feat/branch", "main", self.c_sha, "Title", "Body", runner=pr_create_fail_runner
        )
        self.assertFalse(ok)
        self.assertIn("GraphQL error", res.get("error", ""))

    def test_reconcile_and_publish_pr_view_after_create_fails(self) -> None:
        def pr_view_after_create_fail(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "pr view" in cmd_str:
                return 1, "not found"
            if "ls-remote" in cmd_str:
                return 0, f"{self.c_sha}\trefs/heads/feat/branch\n"
            if "pr create" in cmd_str:
                return 0, "https://github.com/selamy-labs/agent-skills/pull/152"
            return 0, ""

        ok2, res2 = reconcile_and_publish_pr(
            self.worktree, "feat/branch", "main", self.c_sha, "Title", "Body", runner=pr_view_after_create_fail
        )
        self.assertFalse(ok2)
        self.assertEqual(res2.get("reason_code"), "CREATED_PR_VIEW_FAILED")

    def test_reconcile_and_merge_pr_actor_disallowed(self) -> None:
        def disallowed_actor_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "api user" in " ".join(cmd):
                return 0, json.dumps({"login": "attacker", "type": "User"})
            return 0, ""

        ok1, res1 = reconcile_and_merge_pr(
            self.worktree, "152", self.c_sha, merge_identity="selamy-labs-bot", runner=disallowed_actor_runner
        )
        self.assertFalse(ok1)
        self.assertEqual(res1.get("reason_code"), "DISALLOWED_MERGE_IDENTITY")

    def test_reconcile_and_merge_pr_view_failed(self) -> None:
        def pr_view_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "api user" in " ".join(cmd):
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr view" in " ".join(cmd):
                return 1, "PR not found"
            return 0, ""

        ok2, res2 = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=pr_view_fail_runner)
        self.assertFalse(ok2)
        self.assertEqual(res2.get("reason_code"), "PR_VIEW_FAILED")

    def test_reconcile_and_merge_pr_no_rollup(self) -> None:
        def no_rollup_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "api user" in " ".join(cmd):
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr view" in " ".join(cmd):
                return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha, "statusCheckRollup": None})
            return 0, ""

        ok3, res3 = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=no_rollup_runner)
        self.assertFalse(ok3)
        self.assertEqual(res3.get("reason_code"), "MISSING_EXACT_HEAD_CI")

    def test_reconcile_and_merge_pr_reconciles_already_merged(self) -> None:
        def already_merged_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": self.c_sha,
                        "mergedAt": "2026-11-03T10:00:00Z",
                        "mergeCommit": {"oid": "d" * 40},
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        ok4, res4 = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=already_merged_runner)
        self.assertTrue(ok4)
        self.assertTrue(res4.get("reconciled_existing_effect"))
        self.assertEqual(res4.get("merge_commit_sha"), "d" * 40)

    def test_reconcile_and_merge_pr_merge_command_fails(self) -> None:
        def pr_merge_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "OPEN",
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                        ],
                    }
                )
            if "pr merge" in cmd_str:
                return 1, "Merge conflict on server"
            return 0, ""

        ok5, res5 = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=pr_merge_fail_runner)
        self.assertFalse(ok5)
        self.assertEqual(res5.get("reason_code"), "MERGE_FAILED")

    def test_reconcile_and_merge_pr_post_merge_verify_fails(self) -> None:
        def post_merge_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["test"]})
            if "pr merge" in cmd_str:
                return 0, "Merge queued"
            if "pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "OPEN",
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}
                        ],
                    }
                )
            return 0, ""

        ok6, res6 = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=post_merge_fail_runner)
        self.assertFalse(ok6)
        self.assertEqual(res6.get("reason_code"), "MERGE_NOT_COMPLETED")

    def test_parse_required_checks_rulesets(self) -> None:
        ruleset_payload = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "ci/test", "integration_id": 12345},
                        {"context": "lint", "app_id": 67890},
                    ]
                },
            },
            {"type": "pull_request", "parameters": {}},
            "not-a-dict-rule",
            {"type": "required_status_checks", "parameters": "not-a-dict"},
            {"type": "required_status_checks", "parameters": {"required_status_checks": "not-a-list"}},
        ]
        parsed = parse_required_checks(ruleset_payload)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0], {"context": "ci/test", "app_id": 12345})
        self.assertEqual(parsed[1], {"context": "lint", "app_id": 67890})

        # Non-dict and non-list returns empty
        self.assertEqual(parse_required_checks("string"), [])
        self.assertEqual(parse_required_checks(12345), [])

    def test_parse_required_checks_branch_protection(self) -> None:
        bp_payload = {
            "contexts": ["build", "Quality"],
            "checks": [
                {"context": "Quality", "app_id": 12345},
                {"context": "Release", "integration_id": 67890},
                "not-a-dict",
            ],
        }
        parsed = parse_required_checks(bp_payload)
        self.assertEqual(len(parsed), 3)
        quality_entry = next(c for c in parsed if c["context"] == "Quality")
        self.assertEqual(quality_entry["app_id"], 12345)
        build_entry = next(c for c in parsed if c["context"] == "build")
        self.assertIsNone(build_entry["app_id"])
        release_entry = next(c for c in parsed if c["context"] == "Release")
        self.assertEqual(release_entry["app_id"], 67890)

        # Malformed branch protection
        self.assertEqual(parse_required_checks({"contexts": "not-a-list", "checks": "not-a-list"}), [])

    def test_query_branch_protection_checks_variations(self) -> None:
        def error_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "404 Not Found"

        res1 = fetch_required_checks_from_provider(self.worktree, "main", runner=error_runner)
        self.assertIsNone(res1)

        def bad_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not-valid-json"

        res2 = fetch_required_checks_from_provider(self.worktree, "main", runner=bad_json_runner)
        self.assertIsNone(res2)

    def test_fetch_required_checks_from_provider_unions_protection_and_rulesets(self) -> None:
        def union_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "protection/required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["Quality"]})
            if "rules/branches" in cmd_str:
                return 0, json.dumps(
                    [
                        {
                            "type": "required_status_checks",
                            "parameters": {"required_status_checks": [{"context": "Release", "integration_id": 99}]},
                        }
                    ]
                )
            return 0, ""

        checks = fetch_required_checks_from_provider("selamy-labs/agent-skills", "main", runner=union_runner)
        self.assertIsNotNone(checks)
        contexts = [c["context"] for c in checks]  # type: ignore[index]
        self.assertIn("Quality", contexts)
        self.assertIn("Release", contexts)

    def test_policy_required_checks_extract_and_read(self) -> None:
        # _extract_repo_required_checks list
        self.assertEqual(_extract_repo_required_checks(["check1", " "], "org/repo"), ["check1"])
        # _extract_repo_required_checks dict formats
        d = {
            "selamy-labs/agent-skills": ["check-exact"],
            "my-repo": ["check-base"],
            "repo_dash": ["check-dash"],
        }
        self.assertEqual(_extract_repo_required_checks(d, "selamy-labs/agent-skills"), ["check-exact"])
        self.assertEqual(_extract_repo_required_checks(d, "org/my-repo"), ["check-base"])
        self.assertEqual(_extract_repo_required_checks(d, "org/repo-dash"), ["check-dash"])
        self.assertIsNone(_extract_repo_required_checks(d, "unknown/repo"))
        self.assertIsNone(_extract_repo_required_checks(12345, "org/repo"))

        # _read_policy_dict
        nonexistent = self.worktree / "does-not-exist.json"
        self.assertIsNone(_read_policy_dict(nonexistent))

        bad_json_file = self.worktree / "bad.json"
        bad_json_file.write_text("invalid json", encoding="utf-8")
        self.assertIsNone(_read_policy_dict(bad_json_file))

        list_json_file = self.worktree / "list.json"
        list_json_file.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(_read_policy_dict(list_json_file))

        valid_file = self.worktree / "valid.json"
        valid_file.write_text(json.dumps({"required_checks": ["gate1"]}), encoding="utf-8")
        self.assertEqual(_read_policy_dict(valid_file), {"required_checks": ["gate1"]})

        # load_policy_required_checks
        self.assertIsNone(load_policy_required_checks("selamy-labs/agent-skills", control_dir=None))
        ctrl = self.worktree / "ctrl"
        ctrl.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(load_policy_required_checks("selamy-labs/agent-skills", control_dir=ctrl))
        (ctrl / "delivery-policy.json").write_text(
            json.dumps({"required_checks": {"agent-skills": ["custom-gate"]}}), encoding="utf-8"
        )
        loaded = load_policy_required_checks("selamy-labs/agent-skills", control_dir=ctrl)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0]["context"], "custom-gate")  # type: ignore[index]

    def test_resolve_required_checks_union_behavior(self) -> None:
        def provider_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "protection/required_status_checks" in cmd_str:
                return 0, json.dumps({"contexts": ["Quality", "Release"]})
            return 0, json.dumps([])

        ctrl = self.worktree / "ctrl_res"
        ctrl.mkdir(parents=True, exist_ok=True)
        (ctrl / "delivery-policy.json").write_text(
            json.dumps({"required_checks": {"agent-skills": ["Quality", "Security"]}}),
            encoding="utf-8",
        )
        resolved = resolve_required_checks("selamy-labs/agent-skills", runner=provider_runner, control_dir=ctrl)
        self.assertIsNotNone(resolved)
        ctxs = [c["context"] for c in resolved]  # type: ignore[index]
        self.assertIn("Quality", ctxs)
        self.assertIn("Release", ctxs)
        self.assertIn("Security", ctxs)

        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "error"

        self.assertIsNone(resolve_required_checks("unknown", runner=fail_runner, control_dir=Path("/nonexistent")))

    def test_verify_merge_pr_view_failure_branches(self) -> None:
        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "network error"

        ok1, reason1, _, _ = _verify_merge_pr_view(self.worktree, "152", self.c_sha, runner=fail_runner)
        self.assertFalse(ok1)
        self.assertEqual(reason1, "PR_VIEW_FAILED")

        def bad_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not-json"

        ok2, reason2, _, _ = _verify_merge_pr_view(self.worktree, "152", self.c_sha, runner=bad_json_runner)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "PR_VIEW_FAILED")

        def non_dict_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "[]"

        ok3, reason3, _, _ = _verify_merge_pr_view(self.worktree, "152", self.c_sha, runner=non_dict_runner)
        self.assertFalse(ok3)
        self.assertEqual(reason3, "PR_VIEW_FAILED")

        def head_mismatch_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"headRefOid": "9" * 40, "baseRefName": "main"})

        ok4, reason4, _, _ = _verify_merge_pr_view(self.worktree, "152", self.c_sha, runner=head_mismatch_runner)
        self.assertFalse(ok4)
        self.assertEqual(reason4, "PR_HEAD_MISMATCH")

        def base_mismatch_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"headRefOid": self.c_sha, "baseRefName": "weak"})

        ok5, reason5, _, _ = _verify_merge_pr_view(
            self.worktree, "152", self.c_sha, runner=base_mismatch_runner, expected_base_branch="main"
        )
        self.assertFalse(ok5)
        self.assertEqual(reason5, "BASE_BRANCH_MISMATCH")

        def ok_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"headRefOid": self.c_sha, "baseRefName": "main"})

        ok6, reason6, _, info6 = _verify_merge_pr_view(
            self.worktree, "152", self.c_sha, runner=ok_runner, expected_base_branch="main"
        )
        self.assertTrue(ok6)
        self.assertEqual(reason6, "")
        self.assertEqual(info6.get("baseRefName"), "main")

    def test_fetch_pr_base_branch_variations(self) -> None:
        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "error"

        self.assertIsNone(fetch_pr_base_branch("selamy-labs/agent-skills", "152", runner=fail_runner))

        def bad_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not-json"

        self.assertIsNone(fetch_pr_base_branch("selamy-labs/agent-skills", "152", runner=bad_json_runner))

        def non_dict_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "[]"

        self.assertIsNone(fetch_pr_base_branch("selamy-labs/agent-skills", "152", runner=non_dict_runner))

        def ok_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"baseRefName": "main"})

        self.assertEqual(fetch_pr_base_branch("selamy-labs/agent-skills", "152", runner=ok_runner), "main")

    def test_check_app_matches_behavior(self) -> None:
        self.assertTrue(_check_app_matches({"app": {"id": 123}}, None))
        self.assertTrue(_check_app_matches({}, None))

        self.assertTrue(_check_app_matches({"app": {"id": 12345}}, 12345))
        self.assertTrue(_check_app_matches({"app": 12345}, 12345))
        self.assertTrue(_check_app_matches({"app_id": 12345}, 12345))
        self.assertTrue(_check_app_matches({"integration_id": 12345}, 12345))
        self.assertFalse(_check_app_matches({"app": {"id": 99999}}, 12345))
        self.assertFalse(_check_app_matches({"app": None}, 12345))
        self.assertFalse(_check_app_matches({}, 12345))

    def test_reconcile_and_merge_pr_defaults_to_resolving_required_checks_and_fails_closed_when_none(self) -> None:
        def fail_checks_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "OPEN",
                        "headRefOid": self.c_sha,
                        "statusCheckRollup": [
                            {
                                "name": "any-check",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                                "head_sha": self.c_sha,
                            }
                        ],
                    }
                )
            return 1, "Not found"

        ok, res = reconcile_and_merge_pr(
            self.worktree, "152", self.c_sha, runner=fail_checks_runner, required_checks=None
        )
        self.assertFalse(ok)
        self.assertEqual(res.get("reason_code"), "CI_CHECK_MISSING")

    def test_verify_live_status_rollup_skipped_neutral_non_required_check_passes(self) -> None:
        rollup = [
            {"name": "Quality", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha},
            {"name": "dependabot-auto-merge", "status": "COMPLETED", "conclusion": "SKIPPED", "head_sha": self.c_sha},
            {"name": "optional-linter", "status": "COMPLETED", "conclusion": "NEUTRAL", "head_sha": self.c_sha},
        ]
        ok, code = verify_live_status_rollup(rollup, self.c_sha, required_checks=[{"context": "Quality"}])
        self.assertTrue(ok)
        self.assertEqual(code, "")

        # Negative control: when required check itself is SKIPPED
        req_skipped = [
            {"name": "Quality", "status": "COMPLETED", "conclusion": "SKIPPED", "head_sha": self.c_sha},
        ]
        ok_sk, code_sk = verify_live_status_rollup(req_skipped, self.c_sha, required_checks=[{"context": "Quality"}])
        self.assertFalse(ok_sk)
        self.assertEqual(code_sk, "CI_CHECK_FAILED")

        # Negative control: when optional check is FAILURE
        rollup_failed = [
            {"name": "Quality", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha},
            {"name": "optional-check", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha},
        ]
        ok_f, code_f = verify_live_status_rollup(rollup_failed, self.c_sha, required_checks=[{"context": "Quality"}])
        self.assertFalse(ok_f)
        self.assertEqual(code_f, "CI_CHECK_FAILED")

    def test_is_named_check_passing_duplicate_check_names_all_must_pass(self) -> None:
        # First passing, second failing
        rollup_one_failed = [
            {"name": "test-gate", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha},
            {"name": "test-gate", "status": "COMPLETED", "conclusion": "FAILURE", "head_sha": self.c_sha},
        ]
        ok1, code1 = _is_named_check_passing("test-gate", rollup_one_failed, self.c_sha)
        self.assertFalse(ok1)
        self.assertEqual(code1, "CI_CHECK_FAILED")

        # Both passing
        rollup_both_passed = [
            {"name": "test-gate", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha},
            {"name": "test-gate", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha},
        ]
        ok2, code2 = _is_named_check_passing("test-gate", rollup_both_passed, self.c_sha)
        self.assertTrue(ok2)
        self.assertEqual(code2, "")

    def test_fetch_commit_check_runs_from_provider(self) -> None:
        def ok_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps(
                {
                    "total_count": 1,
                    "check_runs": [
                        {
                            "id": 123456,
                            "name": "Quality",
                            "head_sha": self.c_sha,
                            "status": "completed",
                            "conclusion": "success",
                            "app": {"id": 15368, "slug": "github-actions"},
                        }
                    ],
                }
            )

        runs = fetch_commit_check_runs_from_provider("selamy-labs/agent-skills", self.c_sha, runner=ok_runner)
        self.assertIsNotNone(runs)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["name"], "Quality")
        self.assertEqual(runs[0]["app"]["id"], 15368)

        self.assertIsNone(
            fetch_commit_check_runs_from_provider(
                "selamy-labs/agent-skills", self.c_sha, runner=lambda cmd, cwd=None: (1, "fail")
            )
        )
        self.assertIsNone(
            fetch_commit_check_runs_from_provider(
                "selamy-labs/agent-skills", self.c_sha, runner=lambda cmd, cwd=None: (0, "not json")
            )
        )
        self.assertIsNone(
            fetch_commit_check_runs_from_provider(
                "selamy-labs/agent-skills", self.c_sha, runner=lambda cmd, cwd=None: (0, "[]")
            )
        )
        self.assertIsNone(
            fetch_commit_check_runs_from_provider(
                "selamy-labs/agent-skills", self.c_sha, runner=lambda cmd, cwd=None: (0, "{}")
            )
        )
        runs_empty = fetch_commit_check_runs_from_provider("", self.c_sha, runner=ok_runner)
        self.assertIsNotNone(runs_empty)

    def test_has_app_pinned_checks(self) -> None:
        self.assertTrue(has_app_pinned_checks([{"context": "Quality", "app_id": 15368}]))
        self.assertTrue(has_app_pinned_checks([{"context": "Quality", "integration_id": 15368}]))
        self.assertFalse(has_app_pinned_checks([{"context": "Quality"}]))
        self.assertFalse(has_app_pinned_checks([]))
        self.assertFalse(has_app_pinned_checks(None))

    def test_is_policy_authoritative(self) -> None:
        p_dir = Path(self.temp_dir.name) / "policy_test"
        p_dir.mkdir(parents=True, exist_ok=True)
        self.assertFalse(is_policy_authoritative("selamy-labs/agent-skills", None))
        self.assertFalse(is_policy_authoritative("selamy-labs/agent-skills", p_dir))

        p_file = p_dir / "delivery-policy.json"
        p_file.write_text(json.dumps({"authoritative": True}), encoding="utf-8")
        self.assertTrue(is_policy_authoritative("selamy-labs/agent-skills", p_dir))

        p_file.write_text(json.dumps({"authoritative_policy": True}), encoding="utf-8")
        self.assertTrue(is_policy_authoritative("selamy-labs/agent-skills", p_dir))

        p_file.write_text(json.dumps({"authoritative_repositories": ["agent-skills"]}), encoding="utf-8")
        self.assertTrue(is_policy_authoritative("selamy-labs/agent-skills", p_dir))
        self.assertFalse(is_policy_authoritative("other-org/other-repo", p_dir))

        p_file.write_text(json.dumps({"authoritative": {"agent_skills": True}}), encoding="utf-8")
        self.assertTrue(is_policy_authoritative("selamy-labs/agent-skills", p_dir))
        p_file.write_text(json.dumps({"authoritative": {"agent_skills": False}}), encoding="utf-8")
        self.assertFalse(is_policy_authoritative("selamy-labs/agent-skills", p_dir))

    def test_resolve_required_checks_policy_authoritative(self) -> None:
        p_dir = Path(self.temp_dir.name) / "policy_auth_test"
        p_dir.mkdir(parents=True, exist_ok=True)
        p_file = p_dir / "delivery-policy.json"
        p_file.write_text(
            json.dumps({"authoritative": False, "required_checks": {"agent-skills": ["gate"]}}),
            encoding="utf-8",
        )

        res1 = resolve_required_checks(
            "selamy-labs/agent-skills", runner=lambda cmd, cwd=None: (1, "fail"), control_dir=p_dir
        )
        self.assertIsNone(res1)

        p_file.write_text(
            json.dumps({"authoritative": True, "required_checks": {"agent-skills": ["gate"]}}),
            encoding="utf-8",
        )
        res2 = resolve_required_checks(
            "selamy-labs/agent-skills", runner=lambda cmd, cwd=None: (1, "fail"), control_dir=p_dir
        )
        self.assertEqual(res2, [{"context": "gate", "app_id": None}])

    def test_verify_live_status_rollup_app_pinned_checks(self) -> None:
        req = [{"context": "Quality", "app_id": 15368}]
        rollup_real_gh = [{"name": "Quality", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": self.c_sha}]
        check_runs_ok = [
            {
                "name": "Quality",
                "status": "completed",
                "conclusion": "success",
                "head_sha": self.c_sha,
                "app": {"id": 15368},
            }
        ]

        ok, code = verify_live_status_rollup(rollup_real_gh, self.c_sha, required_checks=req, check_runs=check_runs_ok)
        self.assertTrue(ok)
        self.assertEqual(code, "")

        check_runs_wrong_app = [
            {
                "name": "Quality",
                "status": "completed",
                "conclusion": "success",
                "head_sha": self.c_sha,
                "app": {"id": 99999},
            }
        ]
        ok_w, code_w = verify_live_status_rollup(
            rollup_real_gh, self.c_sha, required_checks=req, check_runs=check_runs_wrong_app
        )
        self.assertFalse(ok_w)
        self.assertEqual(code_w, "CI_CHECK_MISSING")

        ok_m, code_m = verify_live_status_rollup(rollup_real_gh, self.c_sha, required_checks=req, check_runs=[])
        self.assertFalse(ok_m)
        self.assertEqual(code_m, "CI_CHECK_MISSING")

        ok_n, code_n = verify_live_status_rollup(rollup_real_gh, self.c_sha, required_checks=req, check_runs=None)
        self.assertFalse(ok_n)
        self.assertEqual(code_n, "CI_CHECK_MISSING")

        check_runs_failed = [
            {
                "name": "Quality",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": self.c_sha,
                "app": {"id": 15368},
            }
        ]
        ok_fl, code_fl = verify_live_status_rollup(
            rollup_real_gh, self.c_sha, required_checks=req, check_runs=check_runs_failed
        )
        self.assertFalse(ok_fl)
        self.assertEqual(code_fl, "CI_CHECK_FAILED")

    def test_reconcile_and_merge_pr_with_app_pinned_checks_success(self) -> None:
        merged = [False]
        runner = _make_app_pinned_merge_runner(self.c_sha, merged)
        ok, res = reconcile_and_merge_pr(
            self.worktree,
            "152",
            self.c_sha,
            runner=runner,
            required_checks=[{"context": "Quality", "app_id": 15368}],
        )
        self.assertTrue(ok)

    def test_reconcile_and_merge_pr_with_app_pinned_checks_fetch_fails(self) -> None:
        merged = [False]
        runner = _make_app_pinned_merge_runner(self.c_sha, merged, fail_check_runs=True)
        ok_bad, res_bad = reconcile_and_merge_pr(
            self.worktree,
            "152",
            self.c_sha,
            runner=runner,
            required_checks=[{"context": "Quality", "app_id": 15368}],
        )
        self.assertFalse(ok_bad)
        self.assertEqual(res_bad.get("reason_code"), "MISSING_EXACT_HEAD_CI")


if __name__ == "__main__":
    unittest.main()
