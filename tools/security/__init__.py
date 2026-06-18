"""Static malicious-content / prompt-injection scanner for public skill + MCP content.

A defense-in-depth gate (ToxicSkills-style) for the agent-built / contributed
content pipeline: skills and MCP manifests are instructions an autonomous agent
will *follow*, so a malicious contribution can weaponize the agent itself. This
catches the high-confidence malicious-**instruction** patterns that do not appear
in legitimate skill prose.

Scope on purpose: this is conservative and tuned for **zero false-positives on
legitimate skill content** — it is NOT a complete malware scanner. It targets
instructions that subvert the agent (jailbreak / instruction-override), tell the
agent to act covertly against the user, or exfiltrate secrets — none of which
belong in a normative skill. Broad patterns that also appear in legitimate setup
docs (e.g. ``curl … | bash`` installers) are deliberately excluded to keep the
gate trustworthy. Companion to the privacy scanner (secret/leak detection);
this is the malicious-intent half.
"""

from __future__ import annotations

import re
from pathlib import Path

TEXT_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".txt", ".toml", ".json", ".js", ".ts", ".sh"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}

# Files that DEFINE or TEST these patterns — excluded so the scanner does not
# flag its own malicious-pattern literals (mirrors tools.privacy self-scan).
_SELF_SCAN_PATHS = {
    "scripts/security_scan.py",
    "scripts/test_security_scan.py",
    "tools/security/__init__.py",
    "tests/test_security.py",
    # Intentional known-bad sample for the regression test (see
    # tests/test_fixtures.py::TestFixtureRejections.test_malicious_skill_rejected).
    # Excluded from the live gate so the deliberate poison does not fail the real
    # scan, while the scanner still inspects every OTHER file under tests/.
    "tests/fixtures/malicious-skill/SKILL.md",
}

# (category, human description, compiled pattern). IGNORECASE throughout.
# Each pattern is validated to produce ZERO findings on the current known-good
# corpus; add a pattern only with that same guarantee.
MALICIOUS_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # 1. Instruction-override / jailbreak aimed at the agent.
    (
        "instruction-override",
        "tells the agent to ignore/disregard its prior or system instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
            r"(all\s+)?(your\s+|the\s+|previous\s+|prior\s+|above\s+|earlier\s+|system\s+)+"
            r"(instructions?|prompts?|rules?|guidelines?|directives?)",
            re.IGNORECASE,
        ),
    ),
    (
        "jailbreak-persona",
        "switches the agent into an unrestricted/jailbreak persona",
        re.compile(
            r"\byou\s+are\s+now\b[^.\n]{0,40}\b"
            r"(developer\s+mode|dan|jailbroken|jailbreak|unrestricted|no\s+longer\s+bound)",
            re.IGNORECASE,
        ),
    ),
    # 2. Covert action against the user.
    (
        "covert-action",
        "instructs the agent to act without the user's knowledge/consent",
        re.compile(
            r"\bwithout\b[^.\n]{0,30}\bthe\s+user'?s?\b[^.\n]{0,20}"
            r"\b(knowledge|consent|awareness|permission)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "covert-action",
        "instructs the agent to hide an action from the user",
        re.compile(
            r"\b(do\s+not|don'?t|never)\b[^.\n]{0,30}"
            r"\b(tell|inform|notify|alert|mention\s+to|reveal\s+to)\b[^.\n]{0,15}\bthe\s+user\b",
            re.IGNORECASE,
        ),
    ),
    # 3. Secret exfiltration to an external/attacker destination.
    (
        "exfiltration",
        "explicit exfiltration of secrets/credentials",
        re.compile(
            r"\b(exfiltrat\w+|ex-filtrat\w+)\b[^.\n]{0,40}"
            r"\b(secret|credential|token|password|api[-_ ]?key|env|cookie|session)",
            re.IGNORECASE,
        ),
    ),
    (
        "exfiltration",
        "sends secrets/credentials to a hardcoded external URL",
        re.compile(
            r"\b(curl|wget|fetch|requests?\.(post|get)|http[_-]?post|invoke-webrequest)\b"
            r"[^\n]{0,80}"
            r"\b(\$?\{?(secret|token|password|api[_-]?key|credential)s?\}?|\.env\b|id_rsa\b|\.aws/credentials)"
            r"[^\n]{0,80}https?://",
            re.IGNORECASE,
        ),
    ),
    # 4. Obfuscated remote code execution (decode-then-exec; not plain installers).
    (
        "obfuscated-exec",
        "base64/hex-decodes a payload and pipes it to a shell or interpreter",
        re.compile(
            r"\bbase64\s+(-d|--decode|-D)\b[^\n|]*\|\s*(sudo\s+)?(bash|sh|zsh|python\d?|perl|ruby|node)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "obfuscated-exec",
        "eval of decoded/dynamic content",
        re.compile(
            r"\beval\s*\(\s*(atob|base64|bytes\.fromhex|codecs\.decode|String\.fromCharCode)\b",
            re.IGNORECASE,
        ),
    ),
]


def iter_text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def findings_for_text(text: str, rel_path: str) -> list[str]:
    """Return malicious-pattern findings for one file's text (empty = clean)."""
    if rel_path in _SELF_SCAN_PATHS:
        return []
    findings: list[str] = []
    for category, description, pattern in MALICIOUS_PATTERNS:
        if pattern.search(text):
            findings.append(f"{rel_path}: {category} — {description}")
    return findings


def scan_all(root: Path) -> list[str]:
    """Scan every text file under root for malicious-instruction patterns."""
    findings: list[str] = []
    for path in iter_text_files(root):
        text = path.read_text(errors="ignore")
        rel_path = path.relative_to(root).as_posix()
        findings.extend(findings_for_text(text, rel_path))
    return findings
