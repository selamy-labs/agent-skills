"""Skill naming validation logic."""

from __future__ import annotations

import re
from pathlib import Path

SKILL_RE = re.compile(r"^---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)$", re.DOTALL)
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
RESERVED_PREFIXES = ("selamy-", "claude-", "ai-")
RESERVED_TERMS = {
    "agent",
    "agents",
    "llm",
    "workflow",
    "skill",
}


def parse_frontmatter(text: str, path: Path) -> dict[str, str]:
    match = SKILL_RE.match(text)
    if not match:
        raise ValueError(f"{path}: missing YAML frontmatter")

    data: dict[str, str] = {}
    for line in match.group("frontmatter").splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"{path}: malformed frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def _validate_name(name: str, skill_path: Path, known_names: set[str]) -> list[str]:
    errors: list[str] = []
    if not name:
        return errors
    if name in known_names:
        errors.append(f"{skill_path}: duplicate skill name {name}")
    if name != skill_path.parent.name:
        errors.append(f"{skill_path}: name must match directory ({skill_path.parent.name})")
    if not NAME_RE.fullmatch(name):
        errors.append(f"{skill_path}: name must be lowercase descriptive kebab-case")
    if name.startswith(RESERVED_PREFIXES):
        errors.append(f"{skill_path}: name must not use a vendor/tool/noise prefix")
    if name in RESERVED_TERMS:
        errors.append(f"{skill_path}: name is too generic to be stable")
    return errors


def validate_skill(text: str, skill_path: Path, known_names: set[str]) -> list[str]:
    """Validate a single skill file. Returns list of error strings."""
    errors: list[str] = []
    try:
        data = parse_frontmatter(text, skill_path)
        name = data.get("name", "")
        description = data.get("description", "")
        if not name:
            errors.append(f"{skill_path}: missing name")
        if not description:
            errors.append(f"{skill_path}: missing description")
        errors.extend(_validate_name(name, skill_path, known_names))
        if len(description.split()) > 45:
            errors.append(f"{skill_path}: description should stay concise (<=45 words)")
        if "<" in description and ">" in description:
            errors.append(f"{skill_path}: description must not contain XML/HTML tags")
    except Exception as exc:
        errors.append(str(exc))
    return errors


def validate_all_skills(root: Path) -> tuple[list[str], int]:
    """Validate all skills under root/skills/. Returns (errors, count)."""
    errors: list[str] = []
    skills = sorted((root / "skills").glob("*/SKILL.md"))
    if not skills:
        errors.append("no skills found under skills/*/SKILL.md")
        return errors, 0

    names: set[str] = set()
    for skill in skills:
        text = skill.read_text()
        skill_errors = validate_skill(text, skill, names)
        name_data = parse_frontmatter(text, skill) if not any("missing YAML" in e for e in skill_errors) else {}
        name = name_data.get("name", "")
        if name:
            names.add(name)
        errors.extend(skill_errors)

    return errors, len(skills)
