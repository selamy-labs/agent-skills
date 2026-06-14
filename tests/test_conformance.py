"""Conformance test — every shipped skill must pass schema, naming, and privacy scan."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.naming import validate_skill
from tools.privacy import findings_for_text

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"


def _shipped_skills() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def test_shipped_skills_exist() -> None:
    skills = _shipped_skills()
    assert skills, "no skills found under skills/*/SKILL.md"


@pytest.mark.parametrize(
    "skill_path",
    _shipped_skills(),
    ids=[p.parent.name for p in _shipped_skills()],
)
def test_skill_passes_naming(skill_path: Path) -> None:
    text = skill_path.read_text()
    errors = validate_skill(text, skill_path, set())
    assert errors == [], f"naming errors: {errors}"


@pytest.mark.parametrize(
    "skill_path",
    _shipped_skills(),
    ids=[p.parent.name for p in _shipped_skills()],
)
def test_skill_passes_privacy_scan(skill_path: Path) -> None:
    text = skill_path.read_text()
    rel_path = skill_path.relative_to(REPO_ROOT).as_posix()
    findings = findings_for_text(text, rel_path)
    assert findings == [], f"privacy findings: {findings}"
