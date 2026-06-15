from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES_DIR
