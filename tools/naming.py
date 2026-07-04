"""Skill naming validation logic."""

from __future__ import annotations

import re
from pathlib import Path

SKILL_RE = re.compile(r"^---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)$", re.DOTALL)
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
FRONTMATTER_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
ALLOWED_FRONTMATTER_KEYS = {"name", "description", "metadata"}
RESERVED_PREFIXES = ("selamy-", "claude-", "ai-")
RESERVED_TERMS = {
    "agent",
    "agents",
    "llm",
    "workflow",
    "skill",
}


def _clean_frontmatter_value(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _split_frontmatter_entry(line: str, path: Path, label: str) -> tuple[str, str]:
    if ":" not in line:
        raise ValueError(f"{path}: malformed {label} line: {line!r}")
    key, value = line.split(":", 1)
    key = key.strip()
    if not FRONTMATTER_KEY_RE.fullmatch(key):
        raise ValueError(f"{path}: invalid {label} key: {key!r}")
    return key, _clean_frontmatter_value(value)


def _add_metadata_entry(data: dict[str, object], line: str, path: Path) -> None:
    key, value = _split_frontmatter_entry(line.strip(), path, "metadata")
    if not value:
        raise ValueError(f"{path}: empty metadata value for {key!r}")
    metadata = data.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}: metadata must be a mapping")
    if key in metadata:
        raise ValueError(f"{path}: duplicate metadata key: {key!r}")
    metadata[key] = value


def _add_frontmatter_entry(data: dict[str, object], line: str, path: Path) -> str | None:
    key, value = _split_frontmatter_entry(line, path, "frontmatter")
    if key not in ALLOWED_FRONTMATTER_KEYS:
        raise ValueError(f"{path}: unsupported frontmatter key: {key!r}")
    if key in data:
        raise ValueError(f"{path}: duplicate frontmatter key: {key!r}")
    if key == "metadata":
        if value:
            raise ValueError(f"{path}: metadata must be a mapping")
        data[key] = {}
        return key
    if not value:
        raise ValueError(f"{path}: empty frontmatter value for {key!r}")
    data[key] = value
    return None


def parse_frontmatter(text: str, path: Path) -> dict[str, object]:
    match = SKILL_RE.match(text)
    if not match:
        raise ValueError(f"{path}: missing YAML frontmatter")
    if not match.group("body").strip():
        raise ValueError(f"{path}: missing skill body after frontmatter")

    data: dict[str, object] = {}
    active_block: str | None = None
    for line in match.group("frontmatter").splitlines():
        if not line.strip():
            continue
        if line.startswith("  "):
            if active_block != "metadata":
                raise ValueError(f"{path}: malformed frontmatter indentation: {line!r}")
            _add_metadata_entry(data, line, path)
            continue
        if line != line.strip():
            raise ValueError(f"{path}: malformed frontmatter indentation: {line!r}")
        active_block = _add_frontmatter_entry(data, line, path)
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
        name = str(data.get("name", ""))
        description = str(data.get("description", ""))
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
        try:
            name_data = parse_frontmatter(text, skill)
        except Exception:
            name_data = {}
        name = str(name_data.get("name", ""))
        if name:
            names.add(name)
        errors.extend(skill_errors)

    return errors, len(skills)
