#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.privacy import findings_for_text, scan_all

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    findings = scan_all(ROOT)
    if findings:
        print("Public privacy scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("privacy scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
