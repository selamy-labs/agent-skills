"""Verification for raw branch coverage threshold (AS-TS-R13-SOL61-03, AS-TS-R15-SOL61-03)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def is_tracked_by_git(path: Path | str) -> bool:
    """Return True if path is tracked by git."""
    target = Path(path)
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(target)],
            capture_output=True,
            text=True,
            check=False,
            cwd=target.parent if target.parent.is_dir() else None,
        )
        return proc.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def check_report_freshness(path: Path, min_mtime: float | None = None) -> None:
    """Verify that coverage report is not older than the current test run."""
    if min_mtime is not None:
        if path.stat().st_mtime < min_mtime - 0.05:
            raise AssertionError(
                f"Coverage report {path} is older than required run start time "
                f"({path.stat().st_mtime:.2f} < {min_mtime:.2f}). Pre-existing reports are refused."
            )
        return

    marker = path.parent / ".coverage_session_start"
    if not marker.is_file():
        if path.name == "coverage.json":
            raise AssertionError(
                f"Coverage report {path} was not produced by a pytest run in the current session "
                f"(missing session marker {marker}). Pre-existing or unbound reports are refused."
            )
        return

    try:
        session_start = float(marker.read_text(encoding="utf-8").strip())
    except ValueError:
        raise AssertionError(f"Corrupt session marker: {marker}")

    if path.stat().st_mtime < session_start - 0.05:
        raise AssertionError(
            f"Coverage report {path} is older than the current pytest run "
            f"(mtime={path.stat().st_mtime:.2f} < session_start={session_start:.2f}). "
            f"Stale or pre-existing reports are refused."
        )


def verify_coverage_report(
    report_path: Path | str,
    *,
    min_mtime: float | None = None,
    check_run: bool = True,
) -> tuple[float, float]:
    """Verify coverage report has valid file, percent_covered >= 90.0, num_branches > 0,
    and raw branch coverage >= 90.0%.
    """
    path = Path(report_path)
    if not path.is_file():
        raise FileNotFoundError(f"Coverage JSON report not found: {path}")

    if is_tracked_by_git(path):
        raise AssertionError(f"Coverage report {path} is tracked in git; coverage reports must never be committed.")

    if check_run:
        check_report_freshness(path, min_mtime)

    data = json.loads(path.read_text(encoding="utf-8"))
    totals = data.get("totals", {})
    percent_covered = float(totals.get("percent_covered", 0.0))
    num_branches = int(totals.get("num_branches", 0))
    covered_branches = int(totals.get("covered_branches", 0))

    assert percent_covered >= 90.0, f"Combined coverage {percent_covered:.2f}% < 90.0%"
    assert num_branches > 0, f"Coverage report contains no branches ({num_branches})"

    raw_branch_pct = (covered_branches / num_branches) * 100.0
    assert raw_branch_pct >= 90.0, (
        f"Raw branch coverage {raw_branch_pct:.2f}% < 90.0% ({covered_branches}/{num_branches})"
    )

    return percent_covered, raw_branch_pct


def verify_coverage_json_thresholds(
    json_path: Path | str,
    *,
    min_combined_pct: float = 90.0,
    min_branch_pct: float = 90.0,
) -> tuple[float, float, int, int]:
    """Verify combined and raw branch coverage from coverage json output."""
    path = Path(json_path)
    if not path.is_file():
        raise FileNotFoundError(f"Coverage JSON file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    totals = data.get("totals", {})
    num_branches = int(totals.get("num_branches", 0))
    covered_branches = int(totals.get("covered_branches", 0))
    percent_covered = float(totals.get("percent_covered", 0.0))

    if num_branches > 0:
        branch_ratio = covered_branches / num_branches
        branch_pct = 100.0 * branch_ratio
        assert branch_pct >= min_branch_pct, (
            f"Raw branch coverage {branch_pct:.2f}% < {min_branch_pct}% ({covered_branches}/{num_branches})"
        )
    else:
        branch_pct = 100.0

    assert percent_covered >= min_combined_pct, (
        f"Combined coverage {percent_covered:.2f}% is below minimum {min_combined_pct}%"
    )

    return percent_covered, branch_pct, covered_branches, num_branches


def test_coverage_threshold_and_raw_branch_metric() -> None:
    """Test assertion verifying raw branch coverage and combined coverage."""
    if "COVERAGE_JSON_PATH" in os.environ:
        verify_coverage_report(os.environ["COVERAGE_JSON_PATH"])
        return

    candidate = Path("coverage.json")
    if candidate.is_file():
        percent_covered, branch_pct = verify_coverage_report(candidate)
        assert percent_covered >= 90.0
        assert branch_pct >= 90.0


def test_verify_coverage_report_rejects_high_combined_low_raw_branch(tmp_path: Path) -> None:
    """AS-TS-R16-SOL61-01: combined >= 90% but raw branch < 90% must raise AssertionError."""
    report = tmp_path / "high_comb_low_branch.json"
    report.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 91.67,
                    "covered_branches": 10,
                    "num_branches": 20,
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"Raw branch coverage 50\.00% < 90\.0%"):
        verify_coverage_report(report)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point to verify coverage report."""
    args = sys.argv[1:] if argv is None else argv
    report_path = args[0] if args else os.environ.get("COVERAGE_JSON_PATH", "coverage.json")
    try:
        percent_covered, raw_branch_pct = verify_coverage_report(report_path)
        print(
            f"Coverage report verified ({report_path}): "
            f"combined={percent_covered:.2f}%, raw_branch={raw_branch_pct:.2f}%"
        )
        return 0
    except (FileNotFoundError, AssertionError) as exc:
        print(f"Coverage threshold check failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error checking coverage report ({report_path}): {exc}", file=sys.stderr)
        return 1


def test_verify_coverage_report_fixtures(tmp_path: Path) -> None:
    valid_report = tmp_path / "valid_cov.json"
    valid_report.write_text(
        json.dumps(
            {
                "totals": {
                    "num_branches": 668,
                    "covered_branches": 631,
                    "percent_covered": 96.04,
                }
            }
        ),
        encoding="utf-8",
    )
    combined, raw_b = verify_coverage_report(valid_report)
    assert combined == 96.04
    assert pytest.approx(raw_b, 0.01) == 94.46

    zero_report = tmp_path / "zero_cov.json"
    zero_report.write_text(
        json.dumps(
            {
                "totals": {
                    "num_branches": 668,
                    "covered_branches": 0,
                    "percent_covered": 0.0,
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="Combined coverage 0.00% < 90.0%"):
        verify_coverage_report(zero_report)

    low_branch_report = tmp_path / "low_branch_cov.json"
    low_branch_report.write_text(
        json.dumps(
            {
                "totals": {
                    "num_branches": 100,
                    "covered_branches": 80,
                    "percent_covered": 95.0,
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"Raw branch coverage 80.00% < 90.0%"):
        verify_coverage_report(low_branch_report)

    no_branches_report = tmp_path / "no_branches_cov.json"
    no_branches_report.write_text(
        json.dumps(
            {
                "totals": {
                    "num_branches": 0,
                    "covered_branches": 0,
                    "percent_covered": 95.0,
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="Coverage report contains no branches"):
        verify_coverage_report(no_branches_report)

    with pytest.raises(FileNotFoundError):
        verify_coverage_report(tmp_path / "nonexistent.json")


def test_verify_coverage_json_thresholds_synthetic(tmp_path: Path) -> None:
    f = tmp_path / "cov.json"
    f.write_text(
        json.dumps(
            {
                "totals": {
                    "num_branches": 100,
                    "covered_branches": 95,
                    "percent_covered": 96.0,
                }
            }
        ),
        encoding="utf-8",
    )
    comb, br, cov_b, tot_b = verify_coverage_json_thresholds(f, min_combined_pct=90.0, min_branch_pct=90.0)
    assert comb == 96.0
    assert br == 95.0
    assert cov_b == 95
    assert tot_b == 100

    f_low = tmp_path / "low.json"
    f_low.write_text(
        json.dumps(
            {
                "totals": {
                    "num_branches": 100,
                    "covered_branches": 80,
                    "percent_covered": 85.0,
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="Raw branch coverage"):
        verify_coverage_json_thresholds(f_low, min_branch_pct=90.0)

    f_zero = tmp_path / "zero.json"
    f_zero.write_text(
        json.dumps(
            {
                "totals": {
                    "num_branches": 0,
                    "covered_branches": 0,
                    "percent_covered": 95.0,
                }
            }
        ),
        encoding="utf-8",
    )
    verify_coverage_json_thresholds(f_zero, min_combined_pct=90.0)

    with pytest.raises(FileNotFoundError):
        verify_coverage_json_thresholds(tmp_path / "nonexistent.json")


def test_coverage_threshold_fails_when_env_path_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-TS-R16-SOL61-01: Explicitly requested non-existent report must fail closed."""
    monkeypatch.setenv("COVERAGE_JSON_PATH", "/tmp/deliberately-missing-coverage-report.json")
    with pytest.raises(FileNotFoundError, match="Coverage JSON report not found"):
        test_coverage_threshold_and_raw_branch_metric()


def test_main_cli_success_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    valid_report = tmp_path / "valid.json"
    valid_report.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 95.0,
                    "covered_branches": 95,
                    "num_branches": 100,
                }
            }
        ),
        encoding="utf-8",
    )
    assert main([str(valid_report)]) == 0

    missing = tmp_path / "nonexistent.json"
    assert main([str(missing)]) == 1

    monkeypatch.setenv("COVERAGE_JSON_PATH", str(valid_report))
    assert main([]) == 0

    monkeypatch.setenv("COVERAGE_JSON_PATH", str(missing))
    assert main([]) == 1

    low_report = tmp_path / "low.json"
    low_report.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 95.0,
                    "covered_branches": 10,
                    "num_branches": 20,
                }
            }
        ),
        encoding="utf-8",
    )
    assert main([str(low_report)]) == 1


def test_verify_coverage_report_refuses_git_tracked_coverage_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AS-TS-R17-01: Committed coverage report tracked by git must be refused."""
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 99.0,
                    "covered_branches": 99,
                    "num_branches": 100,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("tests.test_coverage_threshold.is_tracked_by_git", lambda _p: True)
    with pytest.raises(AssertionError, match="tracked in git"):
        verify_coverage_report(report)
    assert main([str(report)]) == 1


def test_verify_coverage_report_refuses_pre_existing_stale_report(tmp_path: Path) -> None:
    """AS-TS-R17-01: A pre-existing coverage report created before the test run started must be refused."""
    marker = tmp_path / ".coverage_session_start"
    run_start = 1700000100.0
    marker.write_text(str(run_start), encoding="utf-8")

    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 99.0,
                    "covered_branches": 99,
                    "num_branches": 100,
                }
            }
        ),
        encoding="utf-8",
    )
    old_time = run_start - 3600.0
    os.utime(report, (old_time, old_time))

    with pytest.raises(AssertionError, match="older than the current pytest run"):
        verify_coverage_report(report)
    assert main([str(report)]) == 1


def test_verify_coverage_report_refuses_unbound_report_without_session(tmp_path: Path) -> None:
    """AS-TS-R17-01: A coverage.json found when no pytest run occurred (missing session marker) must be refused."""
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 99.0,
                    "covered_branches": 99,
                    "num_branches": 100,
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="missing session marker"):
        verify_coverage_report(report)
    assert main([str(report)]) == 1


def test_verify_coverage_report_refuses_real_git_tracked_file(tmp_path: Path) -> None:
    """AS-TS-R17-01: Exercise real git ls-files command on a tracked file in a git repo."""
    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@selamy.dev"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    cov = repo / "coverage.json"
    cov.write_text(
        json.dumps({"totals": {"percent_covered": 99.0, "covered_branches": 99, "num_branches": 100}}),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "coverage.json"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add cov"], cwd=repo, check=True, capture_output=True)

    with pytest.raises(AssertionError, match="tracked in git"):
        verify_coverage_report(cov)
    assert main([str(cov)]) == 1


if __name__ == "__main__":
    sys.exit(main())
