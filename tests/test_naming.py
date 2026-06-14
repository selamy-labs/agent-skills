"""Unit tests for tools.naming — skill name and frontmatter validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.naming import NAME_RE, parse_frontmatter, validate_all_skills, validate_skill


def _skill(text: str, dirname: str = "my-skill") -> tuple[str, Path]:
    return text, Path(f"skills/{dirname}/SKILL.md")


class TestNameRegex:
    def test_kebab_case_valid(self) -> None:
        assert NAME_RE.fullmatch("my-cool-skill")
        assert NAME_RE.fullmatch("a")
        assert NAME_RE.fullmatch("data-connector-building")

    def test_uppercase_rejected(self) -> None:
        assert NAME_RE.fullmatch("MySkill") is None

    def test_underscore_rejected(self) -> None:
        assert NAME_RE.fullmatch("my_skill") is None

    def test_leading_digit_rejected(self) -> None:
        assert NAME_RE.fullmatch("1skill") is None

    def test_empty_rejected(self) -> None:
        assert NAME_RE.fullmatch("") is None

    def test_trailing_hyphen_rejected(self) -> None:
        assert NAME_RE.fullmatch("skill-") is None


class TestParseFrontmatter:
    def test_valid_frontmatter(self) -> None:
        text = "---\nname: my-skill\ndescription: A test skill\n---\nBody."
        data = parse_frontmatter(text, Path("test"))
        assert data["name"] == "my-skill"
        assert data["description"] == "A test skill"

    def test_blank_lines_in_frontmatter(self) -> None:
        text = "---\nname: my-skill\n\ndescription: A test skill\n---\nBody."
        data = parse_frontmatter(text, Path("test"))
        assert data["name"] == "my-skill"
        assert data["description"] == "A test skill"

    def test_missing_frontmatter_raises(self) -> None:
        with pytest.raises(ValueError, match="missing YAML frontmatter"):
            parse_frontmatter("no frontmatter here", Path("test"))

    def test_malformed_line_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed frontmatter"):
            parse_frontmatter("---\nbadline\n---\nbody", Path("test"))


class TestValidateSkill:
    def test_valid_skill(self) -> None:
        text = "---\nname: my-skill\ndescription: Does a thing\n---\nBody."
        errors = validate_skill(text, Path("skills/my-skill/SKILL.md"), set())
        assert errors == []

    def test_missing_name(self) -> None:
        text = "---\ndescription: Does a thing\n---\nBody."
        errors = validate_skill(text, Path("skills/x/SKILL.md"), set())
        assert any("missing name" in e for e in errors)

    def test_missing_description(self) -> None:
        text = "---\nname: my-skill\n---\nBody."
        errors = validate_skill(text, Path("skills/my-skill/SKILL.md"), set())
        assert any("missing description" in e for e in errors)

    def test_selamy_prefix_rejected(self) -> None:
        text = "---\nname: selamy-tool\ndescription: Bad prefix\n---\nBody."
        errors = validate_skill(text, Path("skills/selamy-tool/SKILL.md"), set())
        assert any("vendor/tool/noise prefix" in e for e in errors)

    def test_claude_prefix_rejected(self) -> None:
        text = "---\nname: claude-helper\ndescription: Bad prefix\n---\nBody."
        errors = validate_skill(text, Path("skills/claude-helper/SKILL.md"), set())
        assert any("vendor/tool/noise prefix" in e for e in errors)

    def test_ai_prefix_rejected(self) -> None:
        text = "---\nname: ai-thing\ndescription: Bad prefix\n---\nBody."
        errors = validate_skill(text, Path("skills/ai-thing/SKILL.md"), set())
        assert any("vendor/tool/noise prefix" in e for e in errors)

    def test_reserved_term_rejected(self) -> None:
        text = "---\nname: agent\ndescription: Too generic\n---\nBody."
        errors = validate_skill(text, Path("skills/agent/SKILL.md"), set())
        assert any("too generic" in e for e in errors)

    def test_duplicate_name_rejected(self) -> None:
        text = "---\nname: my-skill\ndescription: Dup\n---\nBody."
        errors = validate_skill(text, Path("skills/my-skill/SKILL.md"), {"my-skill"})
        assert any("duplicate" in e for e in errors)

    def test_name_dir_mismatch_rejected(self) -> None:
        text = "---\nname: wrong-name\ndescription: Mismatch\n---\nBody."
        errors = validate_skill(text, Path("skills/my-skill/SKILL.md"), set())
        assert any("must match directory" in e for e in errors)

    def test_oversized_description_rejected(self) -> None:
        long_desc = " ".join(["word"] * 50)
        text = f"---\nname: my-skill\ndescription: {long_desc}\n---\nBody."
        errors = validate_skill(text, Path("skills/my-skill/SKILL.md"), set())
        assert any("concise" in e for e in errors)

    def test_xml_in_description_rejected(self) -> None:
        text = "---\nname: my-skill\ndescription: Use <inject>evil</inject> here\n---\nBody."
        errors = validate_skill(text, Path("skills/my-skill/SKILL.md"), set())
        assert any("XML/HTML" in e for e in errors)

    def test_missing_frontmatter(self) -> None:
        text = "No frontmatter at all."
        errors = validate_skill(text, Path("skills/bad/SKILL.md"), set())
        assert any("missing YAML frontmatter" in e for e in errors)


class TestValidateAllSkills:
    def test_validates_real_skills(self, repo_root: Path) -> None:
        errors, count = validate_all_skills(repo_root)
        assert count > 0
        assert errors == []

    def test_empty_dir_reports_no_skills(self, tmp_path: Path) -> None:
        (tmp_path / "skills").mkdir()
        errors, count = validate_all_skills(tmp_path)
        assert count == 0
        assert any("no skills found" in e for e in errors)
