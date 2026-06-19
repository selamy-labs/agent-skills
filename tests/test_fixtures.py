"""Fixture tests — deliberately bad skills that gates MUST reject."""

from __future__ import annotations

from pathlib import Path

from tools.naming import validate_skill
from tools.privacy import findings_for_text
from tools.security import findings_for_text as security_findings_for_text

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

    def test_malicious_skill_rejected(self) -> None:
        # The known-bad fixture's real content MUST trip the security gate when
        # it appears in a contributed skill path. The fixture's own path is
        # self-scan-excluded (it is an intentional sample), so assert against a
        # representative contributed path to prove the patterns catch it.
        skill_path = FIXTURES_DIR / "malicious-skill" / "SKILL.md"
        text = skill_path.read_text()
        findings = security_findings_for_text(text, "skills/contributed/SKILL.md")
        categories = {f.split(": ", 1)[1].split(" —", 1)[0] for f in findings}
        assert {
            "instruction-override",
            "jailbreak-persona",
            "covert-action",
            "exfiltration",
            "obfuscated-exec",
        } <= categories

    def test_malicious_fixture_excluded_from_live_gate(self) -> None:
        # The intentional sample must NOT fail the real scan: its own path is in
        # the scanner's self-scan exclusion set, so it yields zero findings.
        skill_path = FIXTURES_DIR / "malicious-skill" / "SKILL.md"
        text = skill_path.read_text()
        rel_path = "tests/fixtures/malicious-skill/SKILL.md"
        assert security_findings_for_text(text, rel_path) == []

    def test_real_skills_pass_security_gate(self) -> None:
        # Every shipped skill must be clean under the security scanner.
        skills_dir = FIXTURES_DIR.parents[1] / "skills"
        offenders: list[str] = []
        for skill in sorted(skills_dir.rglob("SKILL.md")):
            rel = skill.relative_to(skills_dir.parents[0]).as_posix()
            offenders.extend(security_findings_for_text(skill.read_text(), rel))
        assert offenders == []
