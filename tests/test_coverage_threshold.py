"""Verification for raw branch coverage threshold (AS-TS-R13-SOL61-03, AS-TS-R15-SOL61-03)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


def verify_coverage_report(report_path: Path | str) -> tuple[float, float]:
    """Verify coverage report has valid file, percent_covered >= 90.0, num_branches > 0,
    and raw branch coverage >= 90.0%.
    """
    path = Path(report_path)
    if not path.is_file():
        raise FileNotFoundError(f"Coverage JSON report not found: {path}")

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


if __name__ == "__main__":
    sys.exit(main())
