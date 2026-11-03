from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Ensure all subprocesses inherit branch coverage and configuration
os.environ["COV_CORE_BRANCH"] = "enabled"
os.environ["COVERAGE_RCFILE"] = str(REPO_ROOT / "pyproject.toml")


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES_DIR


def pytest_sessionstart(session: pytest.Session) -> None:
    """Prepare a fresh coverage state for this test session."""
    report_file = REPO_ROOT / "coverage.json"
    if report_file.is_file():
        try:
            report_file.unlink()
        except OSError:
            pass
    marker = REPO_ROOT / ".coverage_session_start"
    session_id = getattr(session, "session_id", None) or (
        f"pytest-{os.getpid()}-{int(time.time() * 1000)}-{os.urandom(8).hex()}"
    )
    setattr(session, "session_id", session_id)
    marker_data = {
        "session_id": session_id,
        "timestamp": time.time(),
        "pytest_version": pytest.__version__,
    }
    marker.write_text(json.dumps(marker_data), encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int):  # type: ignore[no-untyped-def]
    """Inject the verified pytest session_id into coverage.json after pytest-cov writes it."""
    _ = exitstatus
    yield
    report_file = REPO_ROOT / "coverage.json"
    if report_file.is_file():
        try:
            cov_data = json.loads(report_file.read_text(encoding="utf-8"))
            session_id = getattr(session, "session_id", None)
            if session_id and isinstance(cov_data, dict):
                cov_data["session_id"] = session_id
                report_file.write_text(json.dumps(cov_data, indent=2), encoding="utf-8")
        except Exception:
            pass
