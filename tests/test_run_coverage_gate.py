from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.run_coverage_gate import main, run_coverage_gate


def test_run_coverage_gate_generates_session_id_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COVERAGE_SESSION_ID", raising=False)
    captured_envs: list[dict[str, str]] = []

    def mock_run(cmd: list[str], cwd: str, env: dict[str, str]) -> MagicMock:
        captured_envs.append(dict(env))
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_run):
        rc = run_coverage_gate()

    assert rc == 0
    assert len(captured_envs) == 2
    sess1 = captured_envs[0].get("COVERAGE_SESSION_ID")
    sess2 = captured_envs[1].get("COVERAGE_SESSION_ID")
    assert sess1 is not None and sess1.startswith("local-session-")
    assert sess1 == sess2


def test_run_coverage_gate_forwards_existing_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COVERAGE_SESSION_ID", "custom-shared-session-99")
    captured_envs: list[dict[str, str]] = []

    def mock_run(cmd: list[str], cwd: str, env: dict[str, str]) -> MagicMock:
        captured_envs.append(dict(env))
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_run):
        rc = run_coverage_gate()

    assert rc == 0
    assert len(captured_envs) == 2
    assert captured_envs[0].get("COVERAGE_SESSION_ID") == "custom-shared-session-99"
    assert captured_envs[1].get("COVERAGE_SESSION_ID") == "custom-shared-session-99"


def test_run_coverage_gate_stops_on_pytest_failure() -> None:
    def mock_run(cmd: list[str], cwd: str, env: dict[str, str]) -> MagicMock:
        return MagicMock(returncode=2)

    with patch("subprocess.run", side_effect=mock_run):
        rc = run_coverage_gate()

    assert rc == 2


def test_run_coverage_gate_propagates_verify_failure() -> None:
    calls = []

    def mock_run(cmd: list[str], cwd: str, env: dict[str, str]) -> MagicMock:
        calls.append(cmd)
        if "tests.test_coverage_threshold" in cmd:
            return MagicMock(returncode=1)
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_run):
        rc = run_coverage_gate()

    assert rc == 1
    assert len(calls) == 2


def test_run_coverage_gate_main_wrapper() -> None:
    with patch("scripts.run_coverage_gate.run_coverage_gate", return_value=0):
        assert main() == 0


def test_readme_documents_shared_session_id_for_manual_commands() -> None:
    readme_path = Path(__file__).resolve().parents[1] / "README.md"
    content = readme_path.read_text(encoding="utf-8")
    assert 'export COVERAGE_SESSION_ID="local-run-$(date +%s)"' in content
    assert '$(date +%s)" pytest' not in content
    assert '$(date +%s)" python' not in content


def test_documented_manual_sequence_shares_single_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = "local-run-1700000000"
    monkeypatch.setenv("COVERAGE_SESSION_ID", session_id)
    captured_envs: list[dict[str, str]] = []

    def mock_run(cmd: list[str], cwd: str, env: dict[str, str]) -> MagicMock:
        captured_envs.append(dict(env))
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_run):
        rc = run_coverage_gate()

    assert rc == 0
    assert len(captured_envs) == 2
    assert captured_envs[0]["COVERAGE_SESSION_ID"] == session_id
    assert captured_envs[1]["COVERAGE_SESSION_ID"] == session_id
