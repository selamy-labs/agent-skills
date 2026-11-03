#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.naming import validate_all_skills

ROOT = Path(__file__).resolve().parents[1]


def _check_no_tracked_coverage_json(repo_root: Path) -> list[str]:
    cov_path = repo_root / "coverage.json"
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(cov_path)],
            capture_output=True,
            text=True,
            check=False,
            cwd=repo_root,
        )
        if proc.returncode == 0:
            return ["coverage.json must not be tracked in git repository"]
    except (FileNotFoundError, OSError):
        pass
    return []


def main() -> int:
    errors, count = validate_all_skills(ROOT)
    errors.extend(_check_no_tracked_coverage_json(ROOT))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"validated {count} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
