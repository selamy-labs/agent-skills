#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.naming import validate_all_skills

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors, count = validate_all_skills(ROOT)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"validated {count} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
