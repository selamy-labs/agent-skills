#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_RE = re.compile(r"^---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)$", re.DOTALL)


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


def main() -> int:
    errors: list[str] = []
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    if not skills:
        errors.append("no skills found under skills/*/SKILL.md")

    names: set[str] = set()
    for skill in skills:
        try:
            text = skill.read_text()
            data = parse_frontmatter(text, skill)
            name = data.get("name", "")
            description = data.get("description", "")
            if not name:
                errors.append(f"{skill}: missing name")
            if not description:
                errors.append(f"{skill}: missing description")
            if name and name in names:
                errors.append(f"{skill}: duplicate skill name {name}")
            names.add(name)
            if name and name != skill.parent.name:
                errors.append(f"{skill}: name must match directory ({skill.parent.name})")
            if len(description.split()) > 45:
                errors.append(f"{skill}: description should stay concise (<=45 words)")
        except Exception as exc:
            errors.append(str(exc))

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"validated {len(skills)} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
