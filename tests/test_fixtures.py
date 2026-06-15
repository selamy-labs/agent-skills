"""Fixture tests — deliberately bad skills that gates MUST reject."""

from __future__ import annotations

from pathlib import Path

from tools.naming import validate_skill
from tools.privacy import findings_for_text

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestFixtureRejections:
    def test_prefixed_name_rejected(self) -> None:
        skill_path = FIXTURES_DIR / "prefixed-name" / "SKILL.md"
        text = skill_path.read_text()
        errors = validate_skill(text, skill_path, set())
        assert any("vendor/tool/noise prefix" in e for e in errors)

    def test_leaking_org_name_rejected(self) -> None:
        skill_path = FIXTURES_DIR / "leaking-org-name" / "SKILL.md"
        text = skill_path.read_text()
        rel_path = "tests/fixtures/leaking-org-name/SKILL.md"
        findings = findings_for_text(text, rel_path)
        assert findings, "org name leak should be caught"

    def test_oversized_description_rejected(self) -> None:
        skill_path = FIXTURES_DIR / "oversized-description" / "SKILL.md"
        text = skill_path.read_text()
        errors = validate_skill(text, skill_path, set())
        assert any("concise" in e for e in errors)

    def test_missing_frontmatter_rejected(self) -> None:
        skill_path = FIXTURES_DIR / "missing-frontmatter" / "SKILL.md"
        text = skill_path.read_text()
        errors = validate_skill(text, skill_path, set())
        assert any("missing YAML frontmatter" in e for e in errors)

    def test_injection_attempt_rejected(self) -> None:
        skill_path = FIXTURES_DIR / "injection-attempt" / "SKILL.md"
        text = skill_path.read_text()
        errors = validate_skill(text, skill_path, set())
        assert any("XML/HTML" in e for e in errors)

    def test_silence_phrase_rejected(self) -> None:
        skill_path = FIXTURES_DIR / "silence-phrase" / "SKILL.md"
        text = skill_path.read_text()
        rel_path = "tests/fixtures/silence-phrase/SKILL.md"
        findings = findings_for_text(text, rel_path)
        assert any("silence phrase" in f for f in findings)
