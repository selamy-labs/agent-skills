#!/usr/bin/env python3
"""Behavioral tests for git and GitHub CLI provider adapters."""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from providers import (  # noqa: E402
    _check_merge_already_merged,
    _clean_string_list,
    _evaluate_additive_policy_checks,
    _evaluate_provider_required_checks,
    _extract_repo_required_checks,
    _handle_runner_result,
    _is_provider_bot_login_allowed,
    _lookup_repo_key,
    _normalize_req_check,
    _parse_pr_checks_payload,
    _parse_pr_view_payload,
    _post_merge_verify,
    _read_policy_dict,
    _run_cmd,
    _split_owner_repo,
    _validate_pr_base,
    _verify_created_pr,
    _verify_policy_checks_passed,
    _verify_pr_view_head_and_state,
    check_existing_pr,
    evaluate_existing_pr_data,
    evaluate_pr_ci,
    fetch_pr_base_branch,
    get_commit_modified_paths,
    get_remote_branch_sha,
    inspect_git_commit,
    is_approved_identity,
    is_hex_40,
    is_hex_64,
    iso_timestamp,
    load_policy_required_checks,
    reconcile_and_complete_spec_task,
    reconcile_and_merge_pr,
    reconcile_and_publish_pr,
    reconcile_and_push,
    sha256_file,
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


def _handle_pr_view_response(
    c_sha: str,
    merged_state: list[bool],
    post_verify_fail: bool,
    post_head_mismatch: bool,
) -> tuple[int, str]:
    if not merged_state[0]:
        return 0, json.dumps({"state": "OPEN", "headRefOid": c_sha, "baseRefName": "main"})
    if post_verify_fail:
        return 0, json.dumps({"state": "OPEN", "headRefOid": c_sha})
    h = "9" * 40 if post_head_mismatch else c_sha
    return 0, json.dumps(
        {
            "state": "MERGED",
            "mergedAt": "2026-11-03T10:00:00Z",
            "mergeCommit": {"oid": "c" * 40},
            "headRefOid": h,
        }
    )


def _merge_flow_cmd_dispatch(
    cmd_str: str,
    c_sha: str,
    merged_state: list[bool],
    ci_pass: bool,
    merge_fail: bool,
    post_verify_fail: bool,
    post_head_mismatch: bool,
) -> tuple[int, str]:
    if "api user" in cmd_str:
        return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
    if "pr checks" in cmd_str:
        bucket = "pass" if ci_pass else "fail"
        return 0, json.dumps([{"name": "test", "state": "COMPLETED", "bucket": bucket, "workflow": "CI"}])
    if "pr merge" in cmd_str:
        if merge_fail:
            return 1, "Merge conflict on server"
        merged_state[0] = True
        return 0, "Merged PR #152"
    if "pr view" in cmd_str:
        return _handle_pr_view_response(c_sha, merged_state, post_verify_fail, post_head_mismatch)
    return 0, ""


def _make_merge_flow_runner(
    c_sha: str,
    merged_state: list[bool],
    *,
    ci_pass: bool = True,
    merge_fail: bool = False,
    post_verify_fail: bool = False,
    post_head_mismatch: bool = False,
) -> Callable[..., tuple[int, str]]:
    return lambda cmd, cwd=None: _merge_flow_cmd_dispatch(
        " ".join(cmd),
        c_sha,
        merged_state,
        ci_pass,
        merge_fail,
        post_verify_fail,
        post_head_mismatch,
    )


def _resolve_post_view_failure(
    fail_view: bool,
    bad_json: bool,
    mismatch_head: bool,
) -> tuple[int, str]:
    if fail_view:
        return 1, "view fail post"
    if bad_json:
        return 0, "not json"
    if mismatch_head:
        return 0, json.dumps({"state": "OPEN", "headRefOid": "9" * 40, "baseRefName": "main"})
    return 0, ""


def _post_check_runner_dispatch(
    cmd_str: str,
    c_sha: str,
    view_calls: list[int],
    *,
    fail_view: bool = False,
    mismatch_head: bool = False,
    bad_json: bool = False,
) -> tuple[int, str]:
    if "pr checks" in cmd_str:
        return 0, json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}])
    if "pr view" not in cmd_str:
        return 0, ""
    view_calls[0] += 1
    if view_calls[0] >= 2:
        return _resolve_post_view_failure(fail_view, bad_json, mismatch_head)
    return 0, json.dumps({"state": "OPEN", "headRefOid": c_sha, "baseRefName": "main"})


def _fwd_runner_dispatch(cmd_str: str, c_sha: str, merged_state: list[bool]) -> tuple[int, str]:
    if "api user" in cmd_str:
        return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
    if "pr checks" in cmd_str:
        return 0, json.dumps([{"name": "PolicyCheck", "state": "COMPLETED", "bucket": "pass", "workflow": "CI"}])
    if "pr merge" in cmd_str:
        merged_state[0] = True
        return 0, "Merged PR #152"
    if "pr view" in cmd_str:
        if merged_state[0]:
            return 0, json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-11-03T10:00:00Z",
                    "mergeCommit": {"oid": "c" * 40},
                    "headRefOid": c_sha,
                }
            )
        return 0, json.dumps({"state": "OPEN", "headRefOid": c_sha, "baseRefName": "main"})
    return 0, ""


class TestProviders(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.worktree = Path(self.temp_dir.name)
        self.c_sha = _init_git_repo(self.worktree)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # --------------------------------------------------------------------------
    # Git & Identity Helpers
    # --------------------------------------------------------------------------

    def test_inspect_git_commit(self) -> None:
        ok, author, committer, tree, _ = inspect_git_commit(self.worktree, self.c_sha)
        self.assertTrue(ok)
        self.assertIn("selamy-labs-bot", author)
        self.assertIn("selamy-labs-bot", committer)
        self.assertEqual(len(tree), 40)

    def test_inspect_git_commit_failures(self) -> None:
        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "fatal: bad object"

        ok, author, committer, tree, _ = inspect_git_commit(self.worktree, self.c_sha, runner=fail_runner)
        self.assertFalse(ok)

        def short_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "only-one-line\n"

        ok2, _, _, _, _ = inspect_git_commit(self.worktree, self.c_sha, runner=short_runner)
        self.assertFalse(ok2)

    def test_is_approved_identity(self) -> None:
        self.assertTrue(is_approved_identity("selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>"))
        self.assertTrue(is_approved_identity("claude-cir <claude-cir@selamy.dev>"))
        self.assertTrue(is_approved_identity("<346154065+selamy-labs-bot@users.noreply.github.com>"))
        self.assertFalse(is_approved_identity("unknown-dev <dev@example.com>"))
        self.assertFalse(is_approved_identity("selamy-labs-bot <wrong-email@example.com>"))
        self.assertFalse(is_approved_identity("<wrong-email@example.com>"))

    def test_get_remote_branch_sha_exact_ref_matching(self) -> None:
        def mock_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            lines = [
                f"{'1' * 40}\trefs/heads/a/feat/W-100",
                f"{'2' * 40}\trefs/heads/feat/W-100",
                f"{'3' * 40}\trefs/heads/feat/W-100/extra",
            ]
            return 0, "\n".join(lines)

        sha = get_remote_branch_sha(self.worktree, "origin", "feat/W-100", runner=mock_runner)
        self.assertEqual(sha, "2" * 40)

    def test_get_remote_branch_sha_failures(self) -> None:
        def error_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "network error"

        self.assertIsNone(get_remote_branch_sha(self.worktree, "origin", "feat/branch", runner=error_runner))

        def empty_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, ""

        self.assertIsNone(get_remote_branch_sha(self.worktree, "origin", "feat/branch", runner=empty_runner))

    def test_get_commit_modified_paths(self) -> None:
        def diff_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "file1.py\nfile2.py\n"

        paths = get_commit_modified_paths(self.worktree, self.c_sha, runner=diff_runner)
        self.assertEqual(paths, ["file1.py", "file2.py"])

        def fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "error"

        self.assertEqual(get_commit_modified_paths(self.worktree, self.c_sha, runner=fail_runner), [])

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

    def test_sha256_and_timestamp(self) -> None:
        p = self.worktree / "test_file.txt"
        p.write_bytes(b"hello world")
        self.assertEqual(len(sha256_file(p)), 64)

        t1 = iso_timestamp()
        self.assertTrue(t1.endswith("Z"))
        dt = datetime.datetime(2026, 11, 3, 12, 0, 0, tzinfo=datetime.UTC)
        self.assertEqual(iso_timestamp(dt), "2026-11-03T12:00:00Z")

        naive_dt = datetime.datetime(2026, 11, 3, 12, 0, 0)
        self.assertEqual(iso_timestamp(naive_dt), "2026-11-03T12:00:00Z")

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

    def test_handle_runner_result(self) -> None:
        code1, out1 = _handle_runner_result((0, "success"))
        self.assertEqual(code1, 0)
        self.assertEqual(out1, "success")

        class FakeProc:
            returncode = 2
            stdout = "standard out"
            stderr = " standard err"

        code2, out2 = _handle_runner_result(FakeProc())
        self.assertEqual(code2, 2)
        self.assertEqual(out2, "standard out standard err")

    # --------------------------------------------------------------------------
    # Spec Task & Bot Actor
    # --------------------------------------------------------------------------

    def test_reconcile_and_complete_spec_task(self) -> None:
        spec_file = self.worktree / "spec.md"
        spec_file.write_text("# Spec\n- [ ] TASK-100 First task\n- [ ] TASK-101 Second task\n", encoding="utf-8")
        ok, res = reconcile_and_complete_spec_task(spec_file, "TASK-100")
        self.assertTrue(ok)
        self.assertFalse(res.get("reconciled_existing_effect"))
        self.assertIn("- [x] TASK-100", spec_file.read_text(encoding="utf-8"))

        ok2, res2 = reconcile_and_complete_spec_task(spec_file, "TASK-100")
        self.assertTrue(ok2)
        self.assertTrue(res2.get("reconciled_existing_effect"))

    def test_reconcile_and_complete_spec_task_variations(self) -> None:
        ok1, res1 = reconcile_and_complete_spec_task(None, "TASK_01")
        self.assertFalse(ok1)
        self.assertIn("Missing spec_path", res1.get("error", ""))

        ok2, res2 = reconcile_and_complete_spec_task("/tmp/nonexistent-spec-file.md", "TASK_01")
        self.assertFalse(ok2)
        self.assertIn("does not exist", res2.get("error", ""))

        spec = self.worktree / "SPEC.md"
        spec.write_text("- [ ] OTHER_TASK: Desc\n", encoding="utf-8")
        ok3, res3 = reconcile_and_complete_spec_task(spec, "TASK_01")
        self.assertFalse(ok3)
        self.assertIn("not found", res3.get("error", ""))

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

    def test_verify_provider_bot_actor_variations(self) -> None:
        def auth_fallback_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "api user" in " ".join(cmd):
                return 1, "Forbidden"
            if "auth status" in " ".join(cmd):
                return 0, "Logged in to github.com account selamy-labs-bot"
            return 0, ""

        ok, err = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=auth_fallback_runner)
        self.assertTrue(ok)
        self.assertEqual(err, "")

        def auth_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "Not logged in"

        ok2, err2 = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=auth_fail_runner)
        self.assertFalse(ok2)
        self.assertEqual(err2, "PROVIDER_AUTH_FAILED")

        def prefix_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"login": "selamy-labs-bot[bot]", "type": "Bot"})

        ok3, err3 = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=prefix_runner)
        self.assertTrue(ok3)

        def bad_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "corrupted json"

        ok4, err4 = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=bad_json_runner)
        self.assertFalse(ok4)
        self.assertEqual(err4, "PROVIDER_AUTH_FAILED")

        def list_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps(["not-a-dict"])

        ok5, err5 = verify_provider_bot_actor(self.worktree, "selamy-labs-bot", runner=list_json_runner)
        self.assertFalse(ok5)
        self.assertEqual(err5, "DISALLOWED_MERGE_IDENTITY")

    def test_is_provider_bot_login_allowed(self) -> None:
        self.assertTrue(_is_provider_bot_login_allowed("selamy-labs-bot", "Bot", "selamy-labs-bot"))
        self.assertTrue(_is_provider_bot_login_allowed("claude-cir", "Bot", "claude-cir"))
        self.assertTrue(_is_provider_bot_login_allowed("selamy-labs-bot[bot]", "Bot", "selamy-labs-bot"))
        self.assertFalse(_is_provider_bot_login_allowed("random-user", "User", "selamy-labs-bot"))
        self.assertFalse(_is_provider_bot_login_allowed("attacker[bot]", "Bot", "selamy-labs-bot"))

    # --------------------------------------------------------------------------
    # Push & PR Publishing
    # --------------------------------------------------------------------------

    def test_reconcile_and_push_reconciles_when_matching(self) -> None:
        def matching_remote(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "ls-remote" in cmd:
                return 0, f"{self.c_sha}\trefs/heads/feat/branch\n"
            return 1, "push should not be called"

        ok, res = reconcile_and_push(self.worktree, "origin", "feat/branch", self.c_sha, runner=matching_remote)
        self.assertTrue(ok)
        self.assertTrue(res.get("reconciled_existing_effect"))

    def test_reconcile_and_push_execution(self) -> None:
        def push_ok_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "ls-remote" in cmd:
                return 0, ""
            if "push" in cmd:
                return 0, "To origin feat/branch"
            return 0, ""

        ok, res = reconcile_and_push(self.worktree, "origin", "feat/branch", self.c_sha, runner=push_ok_runner)
        self.assertTrue(ok)
        self.assertFalse(res.get("reconciled_existing_effect"))

        def push_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "ls-remote" in cmd:
                return 0, ""
            if "push" in cmd:
                return 1, "remote rejected"
            return 0, ""

        ok_f, res_f = reconcile_and_push(self.worktree, "origin", "feat/branch", self.c_sha, runner=push_fail_runner)
        self.assertFalse(ok_f)
        self.assertIn("error", res_f)

    def test_check_existing_pr_strict_json_projection_merged_pr(self) -> None:
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
        pr_incomplete = {
            "number": 152,
            "url": "https://github.com/selamy-labs/agent-skills/pull/152",
            "state": "MERGED",
            "headRefOid": self.c_sha,
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

    def test_check_existing_pr_failures(self) -> None:
        def bad_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not-json"

        ok, err = check_existing_pr(self.worktree, "feat/branch", self.c_sha, runner=bad_json_runner)
        self.assertFalse(ok)
        self.assertIsNone(err)

    def test_evaluate_existing_pr_data(self) -> None:
        self.assertEqual(evaluate_existing_pr_data({"state": "CLOSED"}, self.c_sha), (False, "PR_NOT_OPEN"))
        self.assertEqual(
            evaluate_existing_pr_data({"state": "OPEN", "headRefOid": "9" * 40}, self.c_sha), (False, None)
        )
        ok_open, res_open = evaluate_existing_pr_data(
            {"state": "OPEN", "headRefOid": self.c_sha, "number": 1, "url": "u"}, self.c_sha
        )
        self.assertTrue(ok_open)
        self.assertEqual(res_open["state"], "OPEN")

        ok_merged, res_merged = evaluate_existing_pr_data(
            {
                "state": "MERGED",
                "headRefOid": self.c_sha,
                "mergedAt": "2026-11-03T10:00:00Z",
                "mergeCommit": {"oid": "a" * 40},
                "number": 2,
                "url": "u2",
            },
            self.c_sha,
        )
        self.assertTrue(ok_merged)
        self.assertEqual(res_merged["merge_commit_sha"], "a" * 40)

        self.assertEqual(
            evaluate_existing_pr_data({"state": "UNKNOWN", "headRefOid": self.c_sha}, self.c_sha),
            (False, "PR_NOT_OPEN"),
        )

    def test_reconcile_and_publish_pr_existing_pr(self) -> None:
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

    def test_verify_created_pr_variations(self) -> None:
        ok1, res1 = _verify_created_pr(1, "Command failed", self.c_sha)
        self.assertFalse(ok1)
        self.assertEqual(res1.get("reason_code"), "CREATED_PR_VIEW_FAILED")

        ok2, res2 = _verify_created_pr(0, "not-json", self.c_sha)
        self.assertFalse(ok2)
        self.assertEqual(res2.get("reason_code"), "PR_HEAD_MISMATCH")

        ok3, res3 = _verify_created_pr(0, json.dumps(["list", "not", "dict"]), self.c_sha)
        self.assertFalse(ok3)
        self.assertEqual(res2.get("reason_code"), "PR_HEAD_MISMATCH")

        ok4, res4 = _verify_created_pr(0, json.dumps({"state": "CLOSED", "headRefOid": self.c_sha}), self.c_sha)
        self.assertFalse(ok4)
        self.assertEqual(res4.get("reason_code"), "PR_HEAD_MISMATCH")

        ok5, res5 = _verify_created_pr(0, json.dumps({"state": "OPEN", "headRefOid": "9" * 40}), self.c_sha)
        self.assertFalse(ok5)
        self.assertEqual(res5.get("reason_code"), "PR_HEAD_MISMATCH")

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

    # --------------------------------------------------------------------------
    # Policy Checks
    # --------------------------------------------------------------------------

    def test_clean_string_list_and_split_owner_repo(self) -> None:
        self.assertEqual(_clean_string_list(["a", " ", "", 123]), ["a", "123"])
        self.assertEqual(_split_owner_repo("org/repo"), ("org", "repo"))
        self.assertEqual(_split_owner_repo("repo"), ("", "repo"))
        self.assertEqual(_lookup_repo_key({"repo": ["c1"]}, "org/repo"), (True, ["c1"]))
        self.assertEqual(_lookup_repo_key({"other": ["c1"]}, "org/repo"), (False, None))
        self.assertEqual(_normalize_req_check({"name": "lint"}), {"context": "lint", "app_id": None})
        self.assertEqual(_normalize_req_check("build"), {"context": "build", "app_id": None})

    def test_policy_required_checks_extract_and_read(self) -> None:
        self.assertEqual(
            _extract_repo_required_checks(["check1"], "org/repo"),
            [{"context": "check1", "app_id": None}],
        )
        d = {
            "selamy-labs/agent-skills": ["check-exact"],
            "my-repo": ["check-base"],
            "repo_dash": ["check-dash"],
        }
        self.assertEqual(
            _extract_repo_required_checks(d, "selamy-labs/agent-skills"),
            [{"context": "check-exact", "app_id": None}],
        )
        self.assertEqual(
            _extract_repo_required_checks(d, "org/my-repo"),
            [{"context": "check-base", "app_id": None}],
        )
        self.assertEqual(
            _extract_repo_required_checks(d, "org/repo-dash"),
            [{"context": "check-dash", "app_id": None}],
        )
        self.assertEqual(_extract_repo_required_checks(d, "unknown/repo"), [])
        self.assertIsNone(_extract_repo_required_checks(12345, "org/repo"))
        self.assertIsNone(_extract_repo_required_checks({"agent-skills": "not-a-list"}, "agent-skills"))
        self.assertIsNone(_extract_repo_required_checks({"agent-skills": [""]}, "agent-skills"))

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

        self.assertEqual(load_policy_required_checks("selamy-labs/agent-skills", control_dir=None), [])
        ctrl = self.worktree / "ctrl"
        ctrl.mkdir(parents=True, exist_ok=True)
        self.assertEqual(load_policy_required_checks("selamy-labs/agent-skills", control_dir=ctrl), [])
        (ctrl / "delivery-policy.json").write_text(
            json.dumps({"required_checks": {"agent-skills": ["custom-gate"]}}), encoding="utf-8"
        )
        loaded = load_policy_required_checks("selamy-labs/agent-skills", control_dir=ctrl)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0]["context"], "custom-gate")  # type: ignore[index]

    # --------------------------------------------------------------------------
    # Provider PR CI Evaluation (Adopt-and-Delete: gh pr checks --required)
    # --------------------------------------------------------------------------

    def test_parse_pr_view_and_checks_payload(self) -> None:
        self.assertIsNone(_parse_pr_view_payload("not-json"))
        self.assertIsNone(_parse_pr_view_payload("[1, 2]"))
        self.assertEqual(_parse_pr_view_payload('{"key": "val"}'), {"key": "val"})

        self.assertIsNone(_parse_pr_checks_payload("not-json"))
        self.assertIsNone(_parse_pr_checks_payload('{"key": "val"}'))
        self.assertIsNone(_parse_pr_checks_payload('[1, "not-dict"]'))
        self.assertIsNone(_parse_pr_checks_payload('[{"name": "CI"}]'))
        self.assertEqual(
            _parse_pr_checks_payload('[{"name": "CI", "bucket": "pass"}]'),
            [{"name": "CI", "bucket": "pass"}],
        )

    def test_parse_pr_checks_payload_strict_rows(self) -> None:
        # Missing state/workflow when required
        mal_no_state = '[{"name": "CI", "bucket": "pass"}]'
        self.assertIsNone(_parse_pr_checks_payload(mal_no_state, require_state_workflow=True))

        # Empty name
        mal_empty_name = '[{"name": " ", "bucket": "pass", "state": "SUCCESS", "workflow": "CI"}]'
        self.assertIsNone(_parse_pr_checks_payload(mal_empty_name, require_state_workflow=True))

        # Uppercase non-canonical bucket
        mal_upper_bucket = '[{"name": "CI", "bucket": "PASS", "state": "SUCCESS", "workflow": "CI"}]'
        self.assertIsNone(_parse_pr_checks_payload(mal_upper_bucket, require_state_workflow=True))

        # Unknown bucket
        mal_unk_bucket = '[{"name": "CI", "bucket": "unknown", "state": "SUCCESS", "workflow": "CI"}]'
        self.assertIsNone(_parse_pr_checks_payload(mal_unk_bucket, require_state_workflow=True))

        # Non-string workflow
        mal_num_wf = '[{"name": "CI", "bucket": "pass", "state": "SUCCESS", "workflow": 123}]'
        self.assertIsNone(_parse_pr_checks_payload(mal_num_wf, require_state_workflow=True))

        # Empty string workflow is allowed (e.g. non-Actions status checks)
        valid_empty_wf = '[{"name": "CI", "bucket": "pass", "state": "SUCCESS", "workflow": ""}]'
        self.assertIsNotNone(_parse_pr_checks_payload(valid_empty_wf, require_state_workflow=True))

    def test_validate_pr_base(self) -> None:
        self.assertEqual(_validate_pr_base("main", "main"), (True, ""))
        self.assertEqual(_validate_pr_base("main", ""), (True, ""))
        self.assertEqual(_validate_pr_base("", "main"), (False, "BASE_BRANCH_MISMATCH"))
        self.assertEqual(_validate_pr_base("", ""), (False, "BASE_BRANCH_MISMATCH"))
        self.assertEqual(_validate_pr_base("main", "release"), (False, "BASE_BRANCH_MISMATCH"))

    def test_verify_pr_view_head_and_state(self) -> None:
        info_open = {"state": "OPEN", "headRefOid": self.c_sha, "baseRefName": "main"}
        self.assertEqual(_verify_pr_view_head_and_state(info_open, self.c_sha, "main"), (True, ""))

        info_merged = {"state": "MERGED", "headRefOid": self.c_sha, "baseRefName": "main"}
        self.assertEqual(_verify_pr_view_head_and_state(info_merged, self.c_sha, "main", allow_merged=True), (True, ""))
        self.assertEqual(
            _verify_pr_view_head_and_state(info_merged, self.c_sha, "main", allow_merged=False),
            (False, "PR_NOT_OPEN"),
        )

        info_closed = {"state": "CLOSED", "headRefOid": self.c_sha, "baseRefName": "main"}
        self.assertEqual(_verify_pr_view_head_and_state(info_closed, self.c_sha), (False, "PR_NOT_OPEN"))

        info_head_mis = {"state": "OPEN", "headRefOid": "0" * 40, "baseRefName": "main"}
        self.assertEqual(_verify_pr_view_head_and_state(info_head_mis, self.c_sha), (False, "PR_HEAD_MISMATCH"))

        info_base_mis = {"state": "OPEN", "headRefOid": self.c_sha, "baseRefName": "other"}
        self.assertEqual(
            _verify_pr_view_head_and_state(info_base_mis, self.c_sha, "main"), (False, "BASE_BRANCH_MISMATCH")
        )

        info_no_base = {"state": "OPEN", "headRefOid": self.c_sha}
        self.assertEqual(_verify_pr_view_head_and_state(info_no_base, self.c_sha), (False, "BASE_BRANCH_MISMATCH"))

    def test_evaluate_provider_required_checks(self) -> None:
        def fail_cmd(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "error"

        ok1, err1, _ = _evaluate_provider_required_checks(self.worktree, "152", fail_cmd)
        self.assertFalse(ok1)
        self.assertEqual(err1, "CI_CHECK_MISSING")

        def empty_checks(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "[]"

        ok2, err2, _ = _evaluate_provider_required_checks(self.worktree, "152", empty_checks)
        self.assertFalse(ok2)
        self.assertEqual(err2, "CI_CHECK_MISSING")

        def failing_check(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps([{"name": "test", "state": "FAILURE", "bucket": "fail", "workflow": "CI"}])

        ok3, err3, _ = _evaluate_provider_required_checks(self.worktree, "152", failing_check)
        self.assertFalse(ok3)
        self.assertEqual(err3, "CI_CHECK_FAILED")

        def passing_checks(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}])

        ok4, err4, data4 = _evaluate_provider_required_checks(self.worktree, "152", passing_checks, repo="org/repo")
        self.assertTrue(ok4)
        self.assertEqual(err4, "")
        self.assertEqual(len(data4), 1)

    def test_evaluate_additive_policy_checks(self) -> None:
        ok_no_ctrl, _ = _evaluate_additive_policy_checks(self.worktree, "152", "repo", None, None)
        self.assertTrue(ok_no_ctrl)

        ctrl = self.worktree / "ctrl_add"
        ctrl.mkdir(parents=True, exist_ok=True)
        (ctrl / "delivery-policy.json").write_text(json.dumps({"required_checks": {}}), encoding="utf-8")
        ok_empty, _ = _evaluate_additive_policy_checks(self.worktree, "152", "repo", ctrl, None)
        self.assertTrue(ok_empty)

        (ctrl / "delivery-policy.json").write_text(
            json.dumps({"required_checks": {"repo": ["policy-gate"]}}), encoding="utf-8"
        )

        def fail_cmd(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "error"

        ok_f, err_f = _evaluate_additive_policy_checks(self.worktree, "152", "repo", ctrl, fail_cmd)
        self.assertFalse(ok_f)
        self.assertEqual(err_f, "CI_CHECK_MISSING")

        def bad_json(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not json"

        ok_bj, err_bj = _evaluate_additive_policy_checks(self.worktree, "152", "repo", ctrl, bad_json)
        self.assertFalse(ok_bj)
        self.assertEqual(err_bj, "CI_CHECK_MISSING")

        def missing_policy_check(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps([{"name": "other-gate", "bucket": "pass"}])

        ok_m, err_m = _evaluate_additive_policy_checks(self.worktree, "152", "repo", ctrl, missing_policy_check)
        self.assertFalse(ok_m)
        self.assertEqual(err_m, "CI_CHECK_MISSING")

        def failing_policy_check(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps([{"name": "policy-gate", "bucket": "fail"}])

        ok_fl, err_fl = _evaluate_additive_policy_checks(self.worktree, "152", "repo", ctrl, failing_policy_check)
        self.assertFalse(ok_fl)
        self.assertEqual(err_fl, "CI_CHECK_FAILED")

        def passing_policy_check(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps([{"name": "policy-gate", "bucket": "pass"}])

        ok_p, err_p = _evaluate_additive_policy_checks(self.worktree, "152", "repo", ctrl, passing_policy_check)
        self.assertTrue(ok_p)
        self.assertEqual(err_p, "")

        self.assertEqual(
            _verify_policy_checks_passed([{"context": ""}], {"": "pass"}),
            (False, "CI_CHECK_MISSING"),
        )

    def test_evaluate_pr_ci_pre_check_failures(self) -> None:
        def fail_view(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "view fail"

        ok1, err1, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=fail_view)
        self.assertFalse(ok1)
        self.assertEqual(err1, "PR_VIEW_FAILED")

        def bad_json_view(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not-json"

        ok2, err2, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=bad_json_view)
        self.assertFalse(ok2)
        self.assertEqual(err2, "PR_VIEW_FAILED")

        def closed_view(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"state": "CLOSED", "headRefOid": self.c_sha, "baseRefName": "main"})

        ok3, err3, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=closed_view)
        self.assertFalse(ok3)
        self.assertEqual(err3, "PR_NOT_OPEN")

    def test_evaluate_pr_ci_provider_checks_failures(self) -> None:
        def checks_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "pr checks" in " ".join(cmd):
                return 0, json.dumps([{"name": "test", "state": "FAILURE", "bucket": "fail", "workflow": "CI"}])
            return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha, "baseRefName": "main"})

        ok4, err4, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=checks_fail_runner)
        self.assertFalse(ok4)
        self.assertEqual(err4, "CI_CHECK_FAILED")

    def test_evaluate_pr_ci_post_view_fail(self) -> None:
        view_calls = [0]

        def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return _post_check_runner_dispatch(" ".join(cmd), self.c_sha, view_calls, fail_view=True)

        ok, err, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(err, "PR_VIEW_FAILED")

    def test_evaluate_pr_ci_post_head_mismatch(self) -> None:
        view_calls = [0]

        def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return _post_check_runner_dispatch(" ".join(cmd), self.c_sha, view_calls, mismatch_head=True)

        ok, err, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(err, "MISSING_EXACT_HEAD_CI")

    def test_evaluate_pr_ci_post_bad_json(self) -> None:
        view_calls = [0]

        def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return _post_check_runner_dispatch(" ".join(cmd), self.c_sha, view_calls, bad_json=True)

        ok, err, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(err, "PR_VIEW_FAILED")

    def test_evaluate_pr_ci_post_base_retarget(self) -> None:
        view_calls = [0]

        def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "pr checks" in cmd_str:
                return 0, json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}])
            if "pr view" in cmd_str:
                view_calls[0] += 1
                base = "release" if view_calls[0] >= 2 else "main"
                return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha, "baseRefName": base})
            return 0, ""

        ok, err, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(err, "BASE_BRANCH_MISMATCH")

    def test_evaluate_pr_ci_post_base_missing(self) -> None:
        view_calls = [0]

        def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "pr checks" in cmd_str:
                return 0, json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}])
            if "pr view" in cmd_str:
                view_calls[0] += 1
                base = "" if view_calls[0] >= 2 else "main"
                return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha, "baseRefName": base})
            return 0, ""

        ok, err, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(err, "BASE_BRANCH_MISMATCH")

    def test_evaluate_pr_ci_post_state_closed(self) -> None:
        view_calls = [0]

        def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "pr checks" in cmd_str:
                return 0, json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}])
            if "pr view" in cmd_str:
                view_calls[0] += 1
                state = "CLOSED" if view_calls[0] >= 2 else "OPEN"
                return 0, json.dumps({"state": state, "headRefOid": self.c_sha, "baseRefName": "main"})
            return 0, ""

        ok, err, _ = evaluate_pr_ci(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(err, "PR_NOT_OPEN")

    def test_evaluate_pr_ci_policy_malformed_fails_closed(self) -> None:
        ctrl = self.worktree / "ctrl_malformed"
        ctrl.mkdir(parents=True, exist_ok=True)
        pol_file = ctrl / "delivery-policy.json"

        def all_pass_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "pr checks" in cmd_str:
                return 0, json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}])
            if "pr view" in cmd_str:
                return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha, "baseRefName": "main"})
            return 0, ""

        bad_payloads = [
            "invalid json",
            "null",
            json.dumps({"required_checks": "string"}),
            json.dumps({"required_checks": {"agent-skills": "string"}}),
            json.dumps({"required_checks": {"agent-skills": [""]}}),
            json.dumps({"required_checks": {"agent-skills": [123]}}),
        ]
        for bad_p in bad_payloads:
            pol_file.write_text(bad_p, encoding="utf-8")
            ok, err, _ = evaluate_pr_ci(
                self.worktree,
                "152",
                self.c_sha,
                runner=all_pass_runner,
                control_dir=ctrl,
                repo="selamy-labs/agent-skills",
            )
            self.assertFalse(ok)
            self.assertEqual(err, "CI_CHECK_MISSING")

    def test_evaluate_pr_ci_success(self) -> None:
        def all_pass_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "pr checks" in cmd_str:
                return 0, json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}])
            if "pr view" in cmd_str:
                return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha, "baseRefName": "main"})
            return 0, ""

        ok8, err8, info8 = evaluate_pr_ci(
            self.worktree,
            "152",
            self.c_sha,
            runner=all_pass_runner,
            expected_base_branch="main",
            repo="selamy-labs/agent-skills",
        )
        self.assertTrue(ok8)
        self.assertEqual(err8, "")
        self.assertEqual(info8.get("headRefOid"), self.c_sha)

    # --------------------------------------------------------------------------
    # PR Merge Operations
    # --------------------------------------------------------------------------

    def test_post_merge_verify_variations(self) -> None:
        def view_fail(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 1, "view failed"

        ok1, err1, _ = _post_merge_verify("152", self.c_sha, runner=view_fail, cwd=self.worktree)
        self.assertFalse(ok1)
        self.assertEqual(err1, "PR_VIEW_FAILED")

        def bad_json(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "not-json"

        ok_bj, err_bj, _ = _post_merge_verify("152", self.c_sha, runner=bad_json, cwd=self.worktree)
        self.assertFalse(ok_bj)
        self.assertEqual(err_bj, "PR_VIEW_FAILED")

        def non_dict(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, "[]"

        ok_nd, err_nd, _ = _post_merge_verify("152", self.c_sha, runner=non_dict, cwd=self.worktree)
        self.assertFalse(ok_nd)
        self.assertEqual(err_nd, "PR_VIEW_FAILED")

        def queued_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha})

        ok2, err2, _ = _post_merge_verify("152", self.c_sha, runner=queued_runner, cwd=self.worktree)
        self.assertFalse(ok2)
        self.assertEqual(err2, "MERGE_NOT_COMPLETED")

        def head_mismatch_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-11-03T10:00:00Z",
                    "mergeCommit": {"oid": "c" * 40},
                    "headRefOid": "9" * 40,
                }
            )

        ok3, err3, _ = _post_merge_verify("152", self.c_sha, runner=head_mismatch_runner, cwd=self.worktree)
        self.assertFalse(ok3)
        self.assertEqual(err3, "CANDIDATE_REVISION_MISMATCH")

        def ok_merge_view(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return 0, json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-11-03T10:00:00Z",
                    "mergeCommit": {"oid": "c" * 40},
                    "headRefOid": self.c_sha,
                }
            )

        ok4, oid4, data4 = _post_merge_verify(
            "152", self.c_sha, runner=ok_merge_view, cwd=self.worktree, repo="org/repo"
        )
        self.assertTrue(ok4)
        self.assertEqual(oid4, "c" * 40)
        self.assertEqual(data4.get("state"), "MERGED")

    def test_check_merge_already_merged(self) -> None:
        info_open = {"state": "OPEN"}
        self.assertEqual(_check_merge_already_merged(info_open, self.c_sha), (True, None))

        info_merged_base_mis = {
            "state": "MERGED",
            "mergedAt": "2026-11-03T10:00:00Z",
            "mergeCommit": "c" * 40,
            "baseRefName": "other",
        }
        ok1, res1 = _check_merge_already_merged(info_merged_base_mis, self.c_sha, expected_base_branch="main")
        self.assertFalse(ok1)
        self.assertEqual(res1.get("reason_code"), "BASE_BRANCH_MISMATCH")  # type: ignore[union-attr]

        info_merged_head_mis = {
            "state": "MERGED",
            "mergedAt": "2026-11-03T10:00:00Z",
            "mergeCommit": "c" * 40,
            "headRefOid": "9" * 40,
            "baseRefName": "main",
        }
        ok2, res2 = _check_merge_already_merged(info_merged_head_mis, self.c_sha)
        self.assertFalse(ok2)
        self.assertEqual(res2.get("reason_code"), "PR_HEAD_MISMATCH")  # type: ignore[union-attr]

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

        def bad_json_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            if "api user" in " ".join(cmd):
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr view" in " ".join(cmd):
                return 0, "not json"
            return 0, ""

        ok_bj, res_bj = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=bad_json_runner)
        self.assertFalse(ok_bj)
        self.assertEqual(res_bj.get("reason_code"), "PR_VIEW_FAILED")

    def test_reconcile_and_merge_pr_already_merged(self) -> None:
        def already_merged_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr checks" in cmd_str:
                return 0, json.dumps([{"name": "CI", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}])
            if "pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": self.c_sha,
                        "baseRefName": "main",
                        "mergedAt": "2026-11-03T10:00:00Z",
                        "mergeCommit": {"oid": "d" * 40},
                    }
                )
            return 0, ""

        ok4, res4 = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=already_merged_runner)
        self.assertTrue(ok4)
        self.assertTrue(res4.get("reconciled_existing_effect"))
        self.assertEqual(res4.get("merge_commit_sha"), "d" * 40)

    def test_reconcile_and_merge_pr_missing_base_refuses(self) -> None:
        def no_base_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr view" in cmd_str:
                return 0, json.dumps({"state": "OPEN", "headRefOid": self.c_sha})
            return 0, ""

        ok, res = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=no_base_runner)
        self.assertFalse(ok)
        self.assertEqual(res.get("reason_code"), "BASE_BRANCH_MISMATCH")

    def test_reconcile_and_merge_pr_already_merged_ci_fails(self) -> None:
        def ci_fail_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr checks" in cmd_str:
                return 0, json.dumps([{"name": "CI", "state": "FAILURE", "bucket": "fail", "workflow": "CI"}])
            if "pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": self.c_sha,
                        "baseRefName": "main",
                        "mergedAt": "2026-11-03T10:00:00Z",
                        "mergeCommit": {"oid": "d" * 40},
                    }
                )
            return 0, ""

        ok, res = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=ci_fail_runner)
        self.assertFalse(ok)
        self.assertEqual(res.get("reason_code"), "CI_CHECK_FAILED")

    def test_reconcile_and_merge_pr_already_merged_ci_missing(self) -> None:
        def ci_missing_runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "api user" in cmd_str:
                return 0, json.dumps({"login": "selamy-labs-bot", "type": "Bot"})
            if "pr checks" in cmd_str:
                return 0, "[]"
            if "pr view" in cmd_str:
                return 0, json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": self.c_sha,
                        "baseRefName": "main",
                        "mergedAt": "2026-11-03T10:00:00Z",
                        "mergeCommit": {"oid": "d" * 40},
                    }
                )
            return 0, ""

        ok, res = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=ci_missing_runner)
        self.assertFalse(ok)
        self.assertEqual(res.get("reason_code"), "CI_CHECK_MISSING")

    def test_reconcile_and_merge_pr_ci_failed(self) -> None:
        merged_state = [False]
        runner = _make_merge_flow_runner(self.c_sha, merged_state, ci_pass=False)
        ok, res = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(res.get("reason_code"), "CI_CHECK_FAILED")

    def test_reconcile_and_merge_pr_merge_command_fails(self) -> None:
        merged_state = [False]
        runner = _make_merge_flow_runner(self.c_sha, merged_state, merge_fail=True)
        ok5, res5 = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok5)
        self.assertEqual(res5.get("reason_code"), "MERGE_FAILED")

    def test_reconcile_and_merge_pr_post_merge_verify_fails(self) -> None:
        merged_state = [False]
        runner = _make_merge_flow_runner(self.c_sha, merged_state, post_verify_fail=True)
        ok6, res6 = reconcile_and_merge_pr(self.worktree, "152", self.c_sha, runner=runner)
        self.assertFalse(ok6)
        self.assertEqual(res6.get("reason_code"), "MERGE_NOT_COMPLETED")

    def test_reconcile_and_merge_pr_execution_and_post_verify(self) -> None:
        merged_state = [False]
        runner = _make_merge_flow_runner(self.c_sha, merged_state)
        ok, res = reconcile_and_merge_pr(
            self.worktree, "152", self.c_sha, runner=runner, repo="selamy-labs/agent-skills"
        )
        self.assertTrue(ok)
        self.assertEqual(res.get("state"), "MERGED")
        self.assertEqual(res.get("merge_commit_sha"), "c" * 40)

    def test_reconcile_and_merge_pr_forwards_control_dir(self) -> None:
        ctrl = self.worktree / "ctrl_fwd"
        ctrl.mkdir(parents=True, exist_ok=True)
        (ctrl / "delivery-policy.json").write_text(
            json.dumps({"authoritative": True, "required_checks": {"agent-skills": ["PolicyCheck"]}}),
            encoding="utf-8",
        )
        merged_state = [False]

        def runner(cmd: list[str], cwd: Any = None) -> tuple[int, str]:
            return _fwd_runner_dispatch(" ".join(cmd), self.c_sha, merged_state)

        ok, res = reconcile_and_merge_pr(
            self.worktree,
            "152",
            self.c_sha,
            runner=runner,
            control_dir=ctrl,
            repo="selamy-labs/agent-skills",
        )
        self.assertTrue(ok)
        self.assertEqual(res.get("state"), "MERGED")


if __name__ == "__main__":
    unittest.main()
