"""Privacy scanning logic for public skill content.

The denylist is loaded from ``denylist.yml`` (this package's single source of
truth).  All consuming repos pin to a tagged release of agent-skills and
verify their vendored copy has not drifted.
"""

from __future__ import annotations

import re
from pathlib import Path

from tools.privacy.loader import load_denylist

_DENYLIST_PATH = Path(__file__).with_name("denylist.yml")
_denylist = load_denylist(_DENYLIST_PATH)

TEXT_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".txt", ".toml", ".json"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "tests"}

# Category mapping preserves the display labels used in findings output.
_CATEGORY_MAP = {
    "org_product": "org/product",
    "business_person": "business/person/vendor",
    "internal_process": "internal process",
    "counterpart_leak": "internal counterpart leak",
}

BLOCKED_TERMS: dict[str, list[str]] = {
    _CATEGORY_MAP[key]: _denylist[key]
    for key in _CATEGORY_MAP
}

BLOCKED_TERM_PATTERNS = [
    (category, term, re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.IGNORECASE))
    for category, terms in BLOCKED_TERMS.items()
    for term in terms
]

_FLAG_MAP = {"IGNORECASE": re.IGNORECASE}

SECRET_PATTERNS = [
    (
        entry["label"],
        re.compile(entry["pattern"], _FLAG_MAP.get(entry.get("flags", ""), 0)),
    )
    for entry in _denylist["secret_patterns"]
]

SELF_SCAN_SECRET_LABELS = {
    "GitHub classic token",
    "GitHub fine-grained token",
    "API key token",
    "private key PEM",
}

SILENCE_PHRASES: list[str] = _denylist["silence_phrase"]

SILENCE_PATTERNS = [
    re.compile(rf"(?<![A-Za-z0-9_]){re.escape(phrase)}(?![A-Za-z0-9_])", re.IGNORECASE)
    for phrase in SILENCE_PHRASES
]

# Self-scan exclusion paths — files that define or test the denylist itself.
_SELF_SCAN_PATHS = {
    "scripts/privacy_scan.py",
    "tools/privacy.py",
    "tools/privacy/__init__.py",
    "tools/privacy/denylist.yml",
    "tools/privacy/loader.py",
}


def iter_text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def is_allowed_blocked_term_context(text: str, rel_path: str, term: str) -> bool:
    """Allow a small set of unavoidable infra references in narrow contexts.

    The public CI runs on the speedforge self-hosted runner, so workflow
    files must carry runs-on: speedforge lines. Permit only that exact
    usage in .github/workflows/ and nowhere else.
    """
    if rel_path.startswith(".github/workflows/") and term == "speedforge":
        allowed_lines = {"runs-on: speedforge"}
        for line in text.splitlines():
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", line, re.IGNORECASE):
                if line.strip() not in allowed_lines:
                    return False
        return True
    return False


def findings_for_text(text: str, rel_path: str) -> list[str]:
    findings: list[str] = []
    is_scanner = rel_path in _SELF_SCAN_PATHS
    if not is_scanner:
        for category, term, pattern in BLOCKED_TERM_PATTERNS:
            if pattern.search(text) and not is_allowed_blocked_term_context(text, rel_path, term):
                findings.append(f"{rel_path}: blocked {category} term {term!r}")
    for label, pattern in SECRET_PATTERNS:
        if is_scanner and label not in SELF_SCAN_SECRET_LABELS:
            continue
        if pattern.search(text):
            findings.append(f"{rel_path}: blocked secret/path pattern {label!r}")

    if not is_scanner:
        for sp in SILENCE_PATTERNS:
            if sp.search(text):
                findings.append(f"{rel_path}: blocked publish-boundary silence phrase")
                break
    return findings


def scan_all(root: Path) -> list[str]:
    """Scan all text files under root for privacy violations."""
    findings: list[str] = []
    for path in iter_text_files(root):
        text = path.read_text(errors="ignore")
        rel_path = path.relative_to(root).as_posix()
        findings.extend(findings_for_text(text, rel_path))
    return findings
