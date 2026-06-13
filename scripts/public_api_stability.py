#!/usr/bin/env python3
"""Guard public skill names against silent removal or rename."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
HEADER_RE = re.compile(r"^[a-z]+(?:\([^)]+\))?!:")


def run_git(args: list[str], *, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def parse_version(tag: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(tag)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def latest_semver_tag() -> str | None:
    tags = run_git(["tag", "--list", "v[0-9]*.[0-9]*.[0-9]*"]).splitlines()
    parsed = [(tag, parse_version(tag)) for tag in tags]
    semver_tags = [(tag, version) for tag, version in parsed if version is not None]
    if not semver_tags:
        return None
    return max(semver_tags, key=lambda item: item[1])[0]


def skill_names_at(ref: str) -> set[str]:
    raw = run_git(["ls-tree", "-r", "--name-only", ref, "skills"], check=False)
    names: set[str] = set()
    for path in raw.splitlines():
        parts = Path(path).parts
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            names.add(parts[1])
    return names


def has_breaking_marker(base: str) -> bool:
    raw = run_git(["log", "--format=%s%x1f%b%x1e", f"{base}..HEAD"])
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        subject, _, body = record.partition("\x1f")
        if HEADER_RE.match(subject) or "BREAKING CHANGE" in body:
            return True
    return False


def main() -> int:
    base = latest_semver_tag()
    if base is None:
        print("public API stability check skipped: no semver tag yet")
        return 0

    before = skill_names_at(base)
    after = skill_names_at("HEAD")
    removed = sorted(before - after)
    added = sorted(after - before)

    if not removed:
        print("public API stability check passed")
        return 0

    if has_breaking_marker(base):
        print(
            "public API stability check passed with explicit breaking marker; "
            f"removed={removed}, added={added}"
        )
        return 0

    print("Public API stability check failed:", file=sys.stderr)
    print(
        "Skill removals or renames require an explicit major-release marker "
        "(`!` in the conventional-commit header or `BREAKING CHANGE` in the body).",
        file=sys.stderr,
    )
    for name in removed:
        print(f"- removed skill: {name}", file=sys.stderr)
    if added:
        print(f"Added skills in this range: {', '.join(added)}", file=sys.stderr)
        print("If this is a rename, publish a breaking release.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
