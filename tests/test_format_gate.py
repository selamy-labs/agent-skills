"""Contract tests for the CI Python-format gate (issue #101).

Two guarantees:

1. Static workflow contract — the Quality workflow must run
   ``ruff format --check`` over the whole tree (``.``), so formatting drift
   fails CI. This locks the gate in place; deleting or narrowing it fails here.
2. Behavioral proof — ``ruff format --check`` actually rejects a misformatted
   file and accepts a clean one. The misformatted fixture is generated at
   runtime (never committed) so the tree-wide gate cannot flag it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality.yaml"

# A file the formatter would rewrite (bad spacing, single quotes) and its
# formatted counterpart. Kept config-independent so the result does not depend
# on the repo's line-length.
MISFORMATTED = "x = {  'a' :1 ,'b':2 }\n"
FORMATTED = 'x = {"a": 1, "b": 2}\n'


def test_workflow_runs_ruff_format_check_over_whole_tree() -> None:
    text = WORKFLOW.read_text()
    assert "ruff format --check" in text, "Quality workflow must gate on `ruff format --check`"
    # The gate must cover the whole tree, not just tools/ tests/ — scripts/ has
    # real Python that `ruff check tools/ tests/` skips (issue #101).
    gate_lines = [ln for ln in text.splitlines() if "ruff format --check" in ln]
    # `.` must be a standalone path argument (space-dot at end of line), not a
    # substring of some filename — so the gate covers the whole tree, not a
    # narrowed subset like tools/ tests/.
    assert any(ln.rstrip().endswith(" .") for ln in gate_lines), (
        f"`ruff format --check` must target the whole tree (` .`); got: {gate_lines}"
    )


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ruff_format_check_fails_on_misformatted_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(MISFORMATTED)
    result = subprocess.run(
        ["ruff", "format", "--check", str(bad)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "gate must fail on a misformatted file"


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ruff_format_check_passes_on_formatted_file(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(FORMATTED)
    result = subprocess.run(
        ["ruff", "format", "--check", str(good)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"gate must pass on a clean file; stderr: {result.stderr}"
