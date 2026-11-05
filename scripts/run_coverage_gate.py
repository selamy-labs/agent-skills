#!/usr/bin/env python3
"""Run test suite and verify coverage thresholds locally end-to-end.

Generates a synchronized COVERAGE_SESSION_ID for both pytest and
the threshold verifier, ensuring local execution passes without manual
environment variable configuration.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_coverage_gate(argv: list[str] | None = None) -> int:
    env = os.environ.copy()
    session_id = env.get("COVERAGE_SESSION_ID")
    if not session_id:
        session_id = f"local-session-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        env["COVERAGE_SESSION_ID"] = session_id

    pytest_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--cov=tools",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=json:coverage.json",
        "--cov-fail-under=90",
    ]
    if argv:
        pytest_cmd.extend(argv)

    pytest_res = subprocess.run(pytest_cmd, cwd=str(REPO_ROOT), env=env)
    if pytest_res.returncode != 0:
        return pytest_res.returncode

    verify_cmd = [
        sys.executable,
        "-m",
        "tests.test_coverage_threshold",
        "coverage.json",
    ]
    verify_res = subprocess.run(verify_cmd, cwd=str(REPO_ROOT), env=env)
    return verify_res.returncode


def main() -> int:
    return run_coverage_gate(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
