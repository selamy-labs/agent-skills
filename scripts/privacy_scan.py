#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".txt", ".toml", ".json"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}

BLOCKED_TERMS = [
    "matchpoint",
    "match point",
    "tradestream",
    "nash",
    "speedforge",
    "veinroute",
    "virtual louie",
    "project louie",
    "laura",
    "jerry",
    "bryan",
    "tokenomics",
    "shared subscription",
    "flat-cap",
    "pselamy@gmail.com",
    "localhost",
    "/home/",
    "/tmp/tmux-session-state",
]

SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
]


def iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def main() -> int:
    findings: list[str] = []
    for path in iter_text_files():
        text = path.read_text(errors="ignore")
        lowered = text.lower()
        rel_path = path.relative_to(ROOT).as_posix()
        if rel_path != "scripts/privacy_scan.py":
            for term in BLOCKED_TERMS:
                if term in lowered:
                    findings.append(f"{path.relative_to(ROOT)}: blocked term {term!r}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: blocked secret/path pattern {pattern.pattern!r}")

    if findings:
        print("Public privacy scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("privacy scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
