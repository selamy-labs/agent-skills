#!/usr/bin/env python3
"""Validate that squash-merge PR titles are conventional commits."""

from __future__ import annotations

import argparse
import re
import sys


TITLE_RE = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]+\))?(?P<breaking>!)?: .+")
ALLOWED_TYPES = {
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "style",
    "test",
}


def validate(title: str, body: str = "") -> list[str]:
    errors: list[str] = []
    match = TITLE_RE.match(title.strip())
    if not match:
        errors.append("PR title must be a lowercase conventional commit, e.g. 'fix: correct release plan'")
    elif match.group("type") not in ALLOWED_TYPES:
        errors.append(f"unsupported conventional commit type: {match.group('type')}")

    if re.search(r"(?im)^breaking change:", body):
        errors.append("breaking-change footer must be uppercase 'BREAKING CHANGE:'")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    args = parser.parse_args()

    errors = validate(args.title, args.body)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
