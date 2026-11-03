from __future__ import annotations

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
    marker.write_text(str(time.time()), encoding="utf-8")
