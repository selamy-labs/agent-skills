#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".txt", ".toml", ".json"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}

BLOCKED_TERMS = {
    "org/product": [
        "matchpoint",
        "match point",
        "tradestream",
        "sterling",
        "signalstream",
        "velix",
        "veinroute",
        "beaconfi",
        "speedforge",
        "loam",
        "nash",
        "reid",
        "reeve",
        "sable",
        "axel",
        "nolan",
        "forge",
    ],
    "business/person/vendor": [
        "louie",
        "laura",
        "lerman",
        "jerry",
        "bryan",
        "remine",
        "ylopo",
        "gohighlevel",
        "ghl",
        "showingtime",
    ],
    "internal process": [
        "codex-q",
        "ralph",
        "dotfiles-dispatch",
        "orchestrator",
        "swimlane",
        "drive-codex",
        "tokenomics",
        "hermes pod",
        "selamy-agents",
    ],
    "internal counterpart leak": [
        "internal-agent-skills",
        "internal skills",
        "internal initiative",
        "internal initiatives",
        "private repo",
        "private repository",
        "private repos",
        "private repositories",
        "public subset",
        "internal counterpart",
        "internal counterparts",
        "private sources",
        "private skill",
        "private skills",
    ],
}

BLOCKED_TERM_PATTERNS = [
    (category, term, re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.IGNORECASE))
    for category, terms in BLOCKED_TERMS.items()
    for term in terms
]

SECRET_PATTERNS = [
    ("GitHub classic token", re.compile(r"ghp_[A-Za-z0-9_]{20,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("API key token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("private key PEM", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("IPv4 address", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")),
    ("GSM secret path", re.compile(r"\b(?:gsm|secret-manager|google-secret-manager)://[A-Za-z0-9_./-]+", re.IGNORECASE)),
    ("pass entry path", re.compile(r"\bpass(?: show)? [A-Za-z0-9_./-]+", re.IGNORECASE)),
    ("localhost identity", re.compile(r"\b[A-Za-z0-9._%+-]+@localhost\b", re.IGNORECASE)),
    ("internal group/user", re.compile(r"\bpatrick-agents\b", re.IGNORECASE)),
    ("internal service host", re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.selamy\.dev\b", re.IGNORECASE)),
    ("local home path", re.compile(r"/home/[A-Za-z0-9._-]+/")),
    ("state-file path", re.compile(r"/tmp/tmux-session-state[A-Za-z0-9._/-]*")),
]

SELF_SCAN_SECRET_LABELS = {
    "GitHub classic token",
    "GitHub fine-grained token",
    "API key token",
    "private key PEM",
}


def is_allowed_blocked_term_context(text: str, rel_path: str, term: str) -> bool:
    if rel_path.startswith(".github/workflows/") and term == "speedforge":
        allowed_lines = {"runs-on: speedforge"}
        for line in text.splitlines():
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", line, re.IGNORECASE):
                if line.strip() not in allowed_lines:
                    return False
        return True
    return False


def iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def findings_for_text(text: str, rel_path: str) -> list[str]:
    findings: list[str] = []
    if rel_path != "scripts/privacy_scan.py":
        for category, term, pattern in BLOCKED_TERM_PATTERNS:
            if pattern.search(text) and not is_allowed_blocked_term_context(text, rel_path, term):
                findings.append(f"{rel_path}: blocked {category} term {term!r}")
    for label, pattern in SECRET_PATTERNS:
        if rel_path == "scripts/privacy_scan.py" and label not in SELF_SCAN_SECRET_LABELS:
            continue
        if pattern.search(text):
            findings.append(f"{rel_path}: blocked secret/path pattern {label!r}")
    return findings


def main() -> int:
    findings: list[str] = []
    for path in iter_text_files():
        text = path.read_text(errors="ignore")
        rel_path = path.relative_to(ROOT).as_posix()
        findings.extend(findings_for_text(text, rel_path))

    if findings:
        print("Public privacy scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("privacy scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
