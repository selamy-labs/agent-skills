#!/usr/bin/env python3
"""Behavioral tests for git and GitHub CLI provider adapters."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from providers import (
    check_existing_pr,
    fetch_ci_checks_from_provider,
    get_remote_branch_sha,
    inspect_git_commit,
    is_approved_identity,
    reconcile_and_complete_spec_task,
    reconcile_and_merge_pr,
    reconcile_and_publish_pr,
    reconcile_and_push,
    stamp_rollup_head_sha,
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

    def test_reconcile_and_merge_pr_paths(self) -> None:
        # 1. Already merged
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
            if "pr view" in cmd_str:
                return 0, json.dumps(merged_pr_view)
            return 0, ""

        ok, res = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=merged_runner)
        self.assertTrue(ok)
        self.assertTrue(res.get("reconciled_existing_effect"))

        # 2. PR head mismatch
        mismatch_pr_view = dict(merged_pr_view)
        mismatch_pr_view["headRefOid"] = "e" * 40

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

        # 3. Missing CI
        no_ci_pr_view = dict(merged_pr_view)
        no_ci_pr_view["state"] = "OPEN"
        no_ci_pr_view["statusCheckRollup"] = []

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

    def test_reconcile_and_publish_pr_creation_flow(self) -> None:
        # Remote branch head mismatch
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

        # Successful creation
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

        def merge_flow_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr view" in cmd_str:
                if merged_state[0]:
                    return 0, json.dumps(
                        {
                            "state": "MERGED",
                            "mergedAt": "2026-11-03T10:00:00Z",
                            "mergeCommit": {"oid": "c" * 40},
                            "headRefOid": self.c_sha,
                        }
                    )
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
                merged_state[0] = True
                return 0, "Merged PR #152"
            return 0, ""

        ok, res = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=merge_flow_runner)
        self.assertTrue(ok)
        self.assertEqual(res.get("state"), "MERGED")


if __name__ == "__main__":
    unittest.main()
