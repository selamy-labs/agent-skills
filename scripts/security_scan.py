#!/usr/bin/env python3
"""Fail CI on malicious-instruction / prompt-injection patterns in public content.

Defense-in-depth companion to ``privacy_scan.py`` (which blocks leaks/secrets).
This blocks content that would weaponize an agent that follows the skill.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.security import scan_all

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    findings = scan_all(ROOT)
    if findings:
        print("Security scan failed (malicious-instruction patterns):", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("security scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
