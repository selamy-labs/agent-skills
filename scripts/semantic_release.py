#!/usr/bin/env python3
"""Plan a conventional-commit semantic release for this repository."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
HEADER_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\([^)]+\))?(?P<breaking>!)?:")

INITIAL_VERSION = (0, 1, 0)


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str


def run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def parse_version(tag: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(tag)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def latest_semver_tag() -> tuple[str, tuple[int, int, int]] | None:
    tags = run_git(["tag", "--list", "v[0-9]*.[0-9]*.[0-9]*"]).splitlines()
    parsed = [(tag, parse_version(tag)) for tag in tags]
    semver_tags = [(tag, version) for tag, version in parsed if version is not None]
    if not semver_tags:
        return None
    return max(semver_tags, key=lambda item: item[1])


def commits_since(tag: str | None) -> list[Commit]:
    revision = f"{tag}..HEAD" if tag else "HEAD"
    raw = run_git(["log", "--format=%H%x1f%s%x1f%b%x1e", revision])
    commits: list[Commit] = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        sha, subject, body = (record.split("\x1f", 2) + [""])[:3]
        commits.append(Commit(sha=sha, subject=subject, body=body.strip()))
    return commits


def bump_for(commits: list[Commit], has_existing_tag: bool) -> str | None:
    if not commits:
        return None
    if not has_existing_tag:
        return "initial"

    bump = None
    for commit in commits:
        header = HEADER_RE.match(commit.subject)
        breaking = "BREAKING CHANGE" in commit.body or (header and header.group("breaking"))
        if breaking:
            return "major"
        commit_type = header.group("type").lower() if header else ""
        if commit_type == "feat":
            bump = "minor"
        elif commit_type in {"fix", "perf", "refactor"} and bump != "minor":
            bump = "patch"
    return bump


def next_version(current: tuple[int, int, int] | None, bump: str | None) -> tuple[int, int, int] | None:
    if bump is None:
        return None
    if bump == "initial" or current is None:
        return INITIAL_VERSION

    major, minor, patch = current
    if bump == "major":
        return (major + 1, 0, 0)
    if bump == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def release_notes(commits: list[Commit]) -> str:
    lines = ["## Changes", ""]
    for commit in commits:
        lines.append(f"- {commit.subject} ({commit.sha[:7]})")
    lines.append("")
    return "\n".join(lines)


def write_github_output(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        for key, value in values.items():
            print(f"{key}={value}")
        return

    with Path(output_path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value:
                delimiter = f"EOF_{key}"
                output.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-file", default="release-notes.md")
    args = parser.parse_args()

    latest = latest_semver_tag()
    latest_tag = latest[0] if latest else None
    latest_version = latest[1] if latest else None
    commits = commits_since(latest_tag)
    bump = bump_for(commits, has_existing_tag=latest is not None)
    planned_version = next_version(latest_version, bump)

    if planned_version is None:
        write_github_output(
            {
                "release_needed": "false",
                "latest_tag": latest_tag or "",
                "next_tag": "",
                "bump": "",
            }
        )
        return 0

    next_tag = f"v{planned_version[0]}.{planned_version[1]}.{planned_version[2]}"
    notes = release_notes(commits)
    Path(args.notes_file).write_text(notes, encoding="utf-8")
    write_github_output(
        {
            "release_needed": "true",
            "latest_tag": latest_tag or "",
            "next_tag": next_tag,
            "bump": bump or "",
            "notes_file": args.notes_file,
        }
    )
    print(f"planned {next_tag} ({bump}) from {len(commits)} commit(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
