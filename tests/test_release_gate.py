"""Unit tests for tools.release_gate."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tools.release_gate import (
    evaluate_check_runs,
    fetch_github_check_runs,
    main,
    verify_exact_commit_gate,
)


class TestEvaluateCheckRuns:
    """Verify that evaluate_check_runs fails closed on missing, failed, or cancelled runs."""

    def test_all_required_checks_successful_passes(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is True
        assert reasons == []

    def test_dict_wrapper_format_supported(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = {
            "check_runs": [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
                {
                    "name": "Tests",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
            ]
        }
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is True
        assert reasons == []

    def test_missing_check_fails_closed(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            }
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("missing required check: 'Tests'" in r for r in reasons)

    def test_in_progress_check_fails_closed(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "in_progress",
                "conclusion": None,
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("is not completed (status='in_progress')" in r for r in reasons)

    def test_queued_check_fails_closed(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "queued",
                "conclusion": None,
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("is not completed (status='queued')" in r for r in reasons)

    def test_failed_check_fails_closed(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("did not succeed (conclusion='failure')" in r for r in reasons)

    def test_cancelled_check_fails_closed(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "cancelled",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("did not succeed (conclusion='cancelled')" in r for r in reasons)

    def test_timed_out_check_fails_closed(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "timed_out",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("did not succeed (conclusion='timed_out')" in r for r in reasons)

    def test_mismatched_sha_fails_closed(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        wrong_sha = "0000000000000000000000000000000000000000"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": wrong_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": wrong_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert len(reasons) == 2

    def test_missing_or_none_head_sha_fails_closed_when_sha_expected(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": None,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("missing required check: 'Skill Quality And Privacy'" in r for r in reasons)
        assert any("missing required check: 'Tests'" in r for r in reasons)

    def test_failed_run_with_subsequent_successful_rerun_passes(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "id": 1,
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": 2,
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": 3,
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is True
        assert reasons == []

    def test_successful_run_with_subsequent_failed_rerun_fails(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "id": 1,
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": 2,
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": 3,
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("required check 'Tests' did not succeed" in r for r in reasons)

    def test_incomplete_run_with_successful_run_fails_closed(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "id": 1,
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": 2,
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": 3,
                "name": "Tests",
                "status": "in_progress",
                "conclusion": None,
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha)
        assert passed is False
        assert any("required check 'Tests' is not completed" in r for r in reasons)

    def test_missing_or_null_or_invalid_app_rejected(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        cases = [
            [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                },
                {"name": "Tests", "status": "completed", "conclusion": "success", "head_sha": target_sha},
            ],
            [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": None,
                },
                {"name": "Tests", "status": "completed", "conclusion": "success", "head_sha": target_sha, "app": None},
            ],
            [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": 42,
                },
                {"name": "Tests", "status": "completed", "conclusion": "success", "head_sha": target_sha, "app": 42},
            ],
            [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {},
                },
                {"name": "Tests", "status": "completed", "conclusion": "success", "head_sha": target_sha, "app": {}},
            ],
        ]
        for data in cases:
            passed, reasons = evaluate_check_runs(data, expected_sha=target_sha, expected_app_slug="github-actions")
            assert passed is False
            assert any("missing required check" in r for r in reasons)

    def test_unrelated_app_check_runs_rejected(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "unrelated-app", "id": 99999},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "unrelated-app", "id": 99999},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha, expected_app_slug="github-actions")
        assert passed is False
        assert any("missing required check: 'Skill Quality And Privacy'" in r for r in reasons)
        assert any("missing required check: 'Tests'" in r for r in reasons)

    def test_github_actions_app_check_runs_accepted(self) -> None:
        target_sha = "8d57f38c4e7b4b32f69df0fc8dea5d9793e76bec"
        data = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha, expected_app_slug="github-actions")
        assert passed is True
        assert reasons == []


class TestVerifyExactCommitGate:
    """Verify high-level gate verification and error handling."""

    def test_missing_credentials_fails_closed(self) -> None:
        passed, reasons = verify_exact_commit_gate("abc1234")
        assert passed is False
        assert any("missing GH_TOKEN" in r for r in reasons)

    def test_api_network_failure_fails_closed(self) -> None:
        with patch("tools.release_gate.fetch_github_check_runs", side_effect=RuntimeError("connection refused")):
            passed, reasons = verify_exact_commit_gate("abc1234", token="fake", repo="owner/repo")
            assert passed is False
            assert any("failed to fetch check runs" in r for r in reasons)

    def test_file_backed_data_verification(self, tmp_path: Path) -> None:
        target_sha = "commit123456"
        check_file = tmp_path / "checks.json"
        check_file.write_text(
            json.dumps(
                {
                    "check_runs": [
                        {
                            "name": "Skill Quality And Privacy",
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": target_sha,
                            "app": {"slug": "github-actions"},
                        },
                        {
                            "name": "Tests",
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": target_sha,
                            "app": {"slug": "github-actions"},
                        },
                    ]
                }
            )
        )
        with open(check_file, encoding="utf-8") as f:
            data = json.load(f)
        passed, reasons = verify_exact_commit_gate(target_sha, check_runs_data=data)
        assert passed is True
        assert reasons == []

    def test_fetch_github_check_runs(self) -> None:
        fake_response = io.BytesIO(json.dumps({"check_runs": [], "total_count": 0}).encode("utf-8"))
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = fake_response
            res = fetch_github_check_runs("sha123", token="tok", repo="owner/repo")
            assert res == {"check_runs": [], "total_count": 0}
            req = mock_urlopen.call_args[0][0]
            assert "commits/sha123/check-runs?filter=all&per_page=100&page=1" in req.full_url

    def test_fetch_github_check_runs_pagination_and_link_header(self) -> None:
        page1 = {"total_count": 2, "check_runs": [{"name": "run1", "status": "completed", "conclusion": "success"}]}
        page2 = {"total_count": 2, "check_runs": [{"name": "run2", "status": "completed", "conclusion": "success"}]}
        pages = [(page1, '<https://api.github.com/...page=2>; rel="next"'), (page2, "")]

        with patch("tools.release_gate._fetch_single_check_runs_page", side_effect=pages) as mock_fetch:
            res = fetch_github_check_runs("sha123", token="tok", repo="owner/repo")
            assert res["total_count"] == 2
            assert len(res["check_runs"]) == 2
            assert [r["name"] for r in res["check_runs"]] == ["run1", "run2"]
            assert mock_fetch.call_count == 2

    def test_fetch_github_check_runs_incomplete_fails_closed(self) -> None:
        page1 = {"total_count": 5, "check_runs": [{"name": "run1"}]}
        page2 = {"total_count": 5, "check_runs": []}
        with patch("tools.release_gate._fetch_single_check_runs_page", side_effect=[(page1, ""), (page2, "")]):
            try:
                fetch_github_check_runs("sha123", token="tok", repo="owner/repo")
                raise AssertionError("Expected incomplete check runs to raise RuntimeError")
            except RuntimeError as exc:
                assert "incomplete check runs retrieved" in str(exc)

    def test_fetch_github_check_runs_missing_total_count_fails_closed(self) -> None:
        page1 = {"check_runs": [{"name": "run1"}]}
        with patch("tools.release_gate._fetch_single_check_runs_page", return_value=(page1, "")):
            try:
                fetch_github_check_runs("sha123", token="tok", repo="owner/repo")
                raise AssertionError("Expected missing total_count to raise RuntimeError")
            except RuntimeError as exc:
                assert "missing or invalid total_count" in str(exc)

    def test_fetch_github_check_runs_invalid_total_count_type_fails_closed(self) -> None:
        for bad_tc in (True, False, "100", None, -1):
            page1 = {"total_count": bad_tc, "check_runs": []}
            with patch("tools.release_gate._fetch_single_check_runs_page", return_value=(page1, "")):
                try:
                    fetch_github_check_runs("sha123", token="tok", repo="owner/repo")
                    raise AssertionError(f"Expected bad total_count {bad_tc!r} to raise RuntimeError")
                except RuntimeError as exc:
                    assert "missing or invalid total_count" in str(exc)

    def test_verify_exact_commit_gate_polling_pending_then_success(self) -> None:
        target_sha = "sha123"
        pending_data = {
            "check_runs": [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "in_progress",
                    "conclusion": None,
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
                {
                    "name": "Tests",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
            ]
        }
        success_data = {
            "check_runs": [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
                {
                    "name": "Tests",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
            ]
        }

        call_count = 0

        def fake_fetcher(sha: str, *, token: str, repo: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return pending_data
            return success_data

        sleep_calls: list[float] = []
        clock = [0.0]

        def fake_time() -> float:
            return clock[0]

        def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            clock[0] += duration

        passed, reasons = verify_exact_commit_gate(
            target_sha,
            token="tok",
            repo="owner/repo",
            wait_seconds=10.0,
            poll_interval_seconds=2.0,
            fetcher=fake_fetcher,
            sleeper=fake_sleep,
            time_provider=fake_time,
        )
        assert passed is True
        assert reasons == []
        assert call_count == 2
        assert sleep_calls == [2.0]

    def test_verify_exact_commit_gate_polling_definitive_failure_aborts_immediately(self) -> None:
        target_sha = "sha123"
        failed_data = {
            "check_runs": [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
                {
                    "name": "Tests",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
            ]
        }

        sleep_calls: list[float] = []
        passed, reasons = verify_exact_commit_gate(
            target_sha,
            token="tok",
            repo="owner/repo",
            wait_seconds=600.0,
            poll_interval_seconds=10.0,
            fetcher=lambda sha, token, repo: failed_data,
            sleeper=lambda d: sleep_calls.append(d),
            time_provider=lambda: 0.0,
        )
        assert passed is False
        assert any("did not succeed (conclusion='failure')" in r for r in reasons)
        assert sleep_calls == []

    def test_verify_exact_commit_gate_polling_timeout_fails_closed(self) -> None:
        target_sha = "sha123"
        pending_data = {
            "check_runs": [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "in_progress",
                    "conclusion": None,
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
                {
                    "name": "Tests",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
            ]
        }

        clock = [0.0]

        def fake_time() -> float:
            return clock[0]

        def fake_sleep(duration: float) -> None:
            clock[0] += duration

        passed, reasons = verify_exact_commit_gate(
            target_sha,
            token="tok",
            repo="owner/repo",
            wait_seconds=6.0,
            poll_interval_seconds=2.0,
            fetcher=lambda sha, token, repo: pending_data,
            sleeper=fake_sleep,
            time_provider=fake_time,
        )
        assert passed is False
        assert any("is not completed" in r for r in reasons)
        assert clock[0] >= 6.0

    def test_main_cli_missing_sha(self) -> None:
        with patch("sys.argv", ["release_gate", "--sha", ""]):
            assert main() == 1

    def test_main_cli_success(self, tmp_path: Path) -> None:
        target_sha = "sha123"
        check_file = tmp_path / "checks.json"
        check_file.write_text(
            json.dumps(
                {
                    "check_runs": [
                        {
                            "name": "Skill Quality And Privacy",
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": target_sha,
                            "app": {"slug": "github-actions"},
                        },
                        {
                            "name": "Tests",
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": target_sha,
                            "app": {"slug": "github-actions"},
                        },
                    ]
                }
            )
        )
        with patch("sys.argv", ["release_gate", "--sha", target_sha, "--check-runs-file", str(check_file)]):
            assert main() == 0

    def test_main_cli_with_polling_and_app_args(self) -> None:
        target_sha = "sha123"
        fake_data = {
            "total_count": 2,
            "check_runs": [
                {
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
                {
                    "name": "Tests",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                },
            ],
        }
        with patch("tools.release_gate.fetch_github_check_runs", return_value=fake_data):
            with patch(
                "sys.argv",
                [
                    "release_gate",
                    "--sha",
                    target_sha,
                    "--token",
                    "fake-token",
                    "--repo",
                    "owner/repo",
                    "--wait-seconds",
                    "10",
                    "--poll-interval-seconds",
                    "2",
                    "--expected-app-slug",
                    "github-actions",
                ],
            ):
                assert main() == 0

    def test_main_cli_failure(self, tmp_path: Path) -> None:
        target_sha = "sha123"
        check_file = tmp_path / "checks.json"
        check_file.write_text(json.dumps({"check_runs": []}))
        with patch("sys.argv", ["release_gate", "--sha", target_sha, "--check-runs-file", str(check_file)]):
            assert main() == 1

    def test_string_ids_sorted_numerically(self) -> None:
        target_sha = "sha123"
        # Run 1: id="9", success. Run 2: id="10", failure.
        # String sorting would order "10" < "9", wrongly choosing "9" (success).
        # Numerical sorting orders 10 > 9, correctly choosing "10" (failure).
        data_failing_later = [
            {
                "id": "9",
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": "10",
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": "1",
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data_failing_later, expected_sha=target_sha)
        assert passed is False
        assert any("did not succeed" in r for r in reasons)

        # Conversely, Run 1: id="9" failed, Run 2: id="10" succeeded.
        data_passing_later = [
            {
                "id": "9",
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": "10",
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": "1",
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = evaluate_check_runs(data_passing_later, expected_sha=target_sha)
        assert passed is True
        assert reasons == []

    def test_check_suite_id_filtering(self) -> None:
        target_sha = "sha123"
        data = [
            {
                "id": 1,
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
                "check_suite": {"id": 100},
            },
            {
                "id": 2,
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
                "check_suite": {"id": 100},
            },
            {
                "id": 3,
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
                "check_suite": {"id": 200},
            },
        ]
        # Filtered to check suite 100: passes
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha, expected_check_suite_id=100)
        assert passed is True
        assert reasons == []

        # Filtered to check suite 200: fails (Tests failed, Quality missing)
        passed, reasons = evaluate_check_runs(data, expected_sha=target_sha, expected_check_suite_id=200)
        assert passed is False

    def test_blank_expected_app_slug_raises_value_error(self) -> None:
        target_sha = "sha123"
        data = [
            {
                "id": 1,
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": 2,
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        with pytest.raises(ValueError, match="expected_app_slug must not be blank"):
            evaluate_check_runs(data, expected_sha=target_sha, expected_app_slug="")

        with pytest.raises(ValueError, match="expected_app_slug must not be blank"):
            evaluate_check_runs(data, expected_sha=target_sha, expected_app_slug="   ")

        with pytest.raises(ValueError, match="expected_app_slug must not be blank"):
            verify_exact_commit_gate(
                target_sha,
                check_runs_data=data,
                expected_app_slug="",
            )

    def test_main_rejects_empty_expected_app_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tools.release_gate import main

        monkeypatch.setattr(
            "sys.argv",
            ["release_gate.py", "--sha", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "--expected-app-slug", ""],
        )
        assert main() == 1

    def test_main_parses_optional_check_suite_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from tools.release_gate import main

        target_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        data = {
            "check_runs": [
                {
                    "id": 1,
                    "name": "Tests",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                    "check_suite": {"id": 100},
                },
                {
                    "id": 2,
                    "name": "Skill Quality And Privacy",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": target_sha,
                    "app": {"slug": "github-actions"},
                    "check_suite": {"id": 100},
                },
            ]
        }
        f = tmp_path / "runs.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv",
            [
                "release_gate.py",
                "--sha",
                target_sha,
                "--check-runs-file",
                str(f),
                "--check-suite-id",
                "",
            ],
        )
        assert main() == 0

        monkeypatch.setattr(
            "sys.argv",
            [
                "release_gate.py",
                "--sha",
                target_sha,
                "--check-runs-file",
                str(f),
                "--check-suite-id",
                "100",
            ],
        )
        assert main() == 0


class TestReleaseGateEdgeBranches:
    def test_edge_branches(self) -> None:
        import argparse

        from tools.release_gate import (
            _filter_matching_runs,
            _matches_run,
            _parse_optional_int,
            _parse_run_id,
            evaluate_check_runs,
            verify_exact_commit_gate,
        )

        assert _parse_run_id("not-an-int") == 0
        assert _parse_run_id([1, 2, 3]) == 0

        assert _parse_optional_int(None) is None
        assert _parse_optional_int("   ") is None
        assert _parse_optional_int(42) == 42
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_optional_int("abc")
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_optional_int(3.14)

        assert _matches_run({"head_sha": "a", "app": "not-a-dict"}, "a", "slug", None) is False
        assert _matches_run({"head_sha": "a", "app": {"slug": "other"}}, "a", "slug", None) is False
        assert _matches_run({"head_sha": "a"}, "a", None, None) is True

        with pytest.raises(ValueError, match="expected_app_slug must not be blank"):
            _filter_matching_runs([], "sha", "   ")

        passed, reasons = evaluate_check_runs([], required_checks=["Custom Gate"])
        assert passed is False
        assert "missing required check: 'Custom Gate'" in reasons

        times = [0.0, 1.0, 5.0]
        it = iter(times)

        def mock_time() -> float:
            return next(it, 10.0)

        passed, reasons = verify_exact_commit_gate(
            "sha123",
            check_runs_data=[],
            fetcher=lambda sha, **_kw: [],
            wait_seconds=5.0,
            poll_interval_seconds=0.0,
            time_provider=mock_time,
            sleeper=lambda s: None,
        )
        assert passed is False
