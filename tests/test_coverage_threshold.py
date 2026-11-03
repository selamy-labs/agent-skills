"""Verification for raw branch coverage threshold (AS-TS-R13-SOL61-03, AS-TS-R15-SOL61-03)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

try:
    from tools.validate_skills import is_tracked_by_git
except ModuleNotFoundError:
    _root = Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from tools.validate_skills import is_tracked_by_git


def _has_enclosing_git_repo(target: Path) -> bool:
    cur = target.parent if target.is_file() else target
    while cur != cur.parent:
        if (cur / ".git").is_dir():
            return True
        cur = cur.parent
    return False


@pytest.fixture(autouse=True)
def _mock_untracked_for_synthetic_tests(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    test_name = request.node.name
    skip_keywords = (
        "is_tracked_by_git",
        "refuses_git_tracked",
        "refuses_real_git_tracked",
        "refuses_tracked_marker",
    )
    if any(k in test_name for k in skip_keywords):
        return

    orig_is_tracked = is_tracked_by_git

    def _safe_is_tracked(p: Path | str) -> bool:
        target = Path(p)
        if _has_enclosing_git_repo(target):
            return orig_is_tracked(p)
        return False

    monkeypatch.setattr("tests.test_coverage_threshold.is_tracked_by_git", _safe_is_tracked)


def _parse_session_marker_dict(raw: str, marker: Path) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception:
        raise AssertionError(f"Corrupt session marker in {marker}: not valid JSON")
    if not isinstance(data, dict):
        raise AssertionError(f"Corrupt session marker in {marker}: expected JSON object")
    return data


def _validate_session_marker_fields(data: dict[str, Any], marker: Path) -> None:
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise AssertionError(f"Session marker in {marker} is missing required 'session_id'")
    if "timestamp" not in data:
        raise AssertionError(f"Session marker in {marker} is missing required 'timestamp'")
    try:
        ts = float(data["timestamp"])
    except (ValueError, TypeError):
        raise AssertionError(f"Invalid timestamp in {marker}: {data.get('timestamp')}")

    import math
    import time

    if not math.isfinite(ts) or ts <= 0:
        raise AssertionError(f"Invalid session marker timestamp in {marker}: {ts}")
    if ts < 1000000000.0:
        raise AssertionError(
            f"Invalid session marker timestamp in {marker}: {ts}. Ancient or forged marker timestamps are refused."
        )
    now = time.time()
    max_age_seconds = 3600.0
    if not (now - max_age_seconds <= ts <= now + 300.0):
        raise AssertionError(
            f"Session marker in {marker} timestamp {ts} is outside allowed recency window. "
            "Stale, ancient, or future marker timestamps are refused."
        )

    report_digest = data.get("report_digest")
    if not isinstance(report_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", report_digest):
        raise AssertionError(
            f"Session marker in {marker} is missing required valid 64-hex 'report_digest'. "
            "Markers without report digest are refused."
        )


def _read_session_marker(marker: Path) -> dict[str, Any]:
    if is_tracked_by_git(marker):
        raise AssertionError(f"Refusing session marker tracked in git: {marker}")
    raw = marker.read_text(encoding="utf-8").strip()
    data = _parse_session_marker_dict(raw, marker)
    _validate_session_marker_fields(data, marker)
    return data


def _verify_report_session_binding(path: Path, marker_data: dict[str, Any], report_data: dict[str, Any] | None) -> None:
    expected_session_id = str(marker_data.get("session_id", ""))
    if report_data is None:
        try:
            report_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise AssertionError(f"Failed to parse coverage report {path} for session binding")
    report_session_id = report_data.get("session_id")
    if not report_session_id or report_session_id != expected_session_id:
        raise AssertionError(
            f"Coverage report {path} session_id ({report_session_id!r}) does not match "
            f"pytest session marker session_id ({expected_session_id!r}). Forged or unbound reports are refused."
        )
    expected_digest = marker_data.get("report_digest")
    if not expected_digest or not isinstance(expected_digest, str):
        raise AssertionError(
            "Coverage session marker is missing required 'report_digest'. Markers without report digest are refused."
        )
    import hashlib

    actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_digest != expected_digest:
        raise AssertionError(
            f"Coverage report {path} digest ({actual_digest}) does not match "
            f"session marker report_digest ({expected_digest}). Forged or replayed reports are refused."
        )


def check_report_freshness(
    path: Path,
    min_mtime: float | None = None,
    report_data: dict[str, Any] | None = None,
) -> None:
    """Verify that coverage report is not older than the current test run and matches session marker."""
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

    marker_data = _read_session_marker(marker)
    session_start = float(marker_data["timestamp"])
    if path.stat().st_mtime < session_start - 0.05:
        raise AssertionError(
            f"Coverage report {path} is older than the current pytest run "
            f"(mtime={path.stat().st_mtime:.2f} < session_start={session_start:.2f}). "
            f"Stale or pre-existing reports are refused."
        )

    import time

    now = time.time()
    max_age_seconds = 3600.0
    report_mtime = path.stat().st_mtime
    if not (now - max_age_seconds <= report_mtime <= now + 300.0):
        raise AssertionError(
            f"Coverage report {path} mtime {report_mtime:.2f} is outside the allowed recency window. "
            "Stale, ancient, or future reports are refused."
        )

    _verify_report_session_binding(path, marker_data, report_data)

    try:
        marker.unlink()
    except Exception as exc:
        raise AssertionError(f"Failed to consume single-use coverage session marker {marker}: {exc}")


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

    data = json.loads(path.read_text(encoding="utf-8"))
    if check_run:
        check_report_freshness(path, min_mtime, report_data=data)
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
    import time

    marker = tmp_path / ".coverage_session_start"
    run_start = time.time()
    marker.write_text(
        json.dumps({"session_id": "test-sess", "timestamp": run_start, "report_digest": "0" * 64}),
        encoding="utf-8",
    )

    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "test-sess",
                "totals": {
                    "percent_covered": 99.0,
                    "covered_branches": 99,
                    "num_branches": 100,
                },
            }
        ),
        encoding="utf-8",
    )
    old_time = run_start - 100.0
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


def test_is_tracked_by_git_fails_closed_when_git_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-TS-R18-01: When git cannot execute, is_tracked_by_git must fail closed (return True)."""

    def _mock_run(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", _mock_run)
    assert is_tracked_by_git("any_file.txt") is True


def test_verify_coverage_report_refuses_zero_or_negative_timestamp_marker(tmp_path: Path) -> None:
    """AS-TS-R18-01: A forged session marker containing 0 or negative timestamp must be refused."""
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "test-sess",
                "totals": {"percent_covered": 99.0, "covered_branches": 99, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / ".coverage_session_start"
    for bad_ts in (0, 0.0, -100.5):
        marker.write_text(json.dumps({"session_id": "test-sess", "timestamp": bad_ts}), encoding="utf-8")
        with pytest.raises(AssertionError, match="Invalid session marker timestamp"):
            verify_coverage_report(report)


def test_verify_coverage_report_refuses_tracked_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-TS-R18-01: A session marker tracked in git must be refused."""
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "test-sess",
                "totals": {"percent_covered": 99.0, "covered_branches": 99, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps({"session_id": "test-sess", "timestamp": time.time(), "report_digest": "0" * 64}),
        encoding="utf-8",
    )

    def _mock_is_tracked(p: Path | str) -> bool:
        return str(p).endswith(".coverage_session_start")

    monkeypatch.setattr("tests.test_coverage_threshold.is_tracked_by_git", _mock_is_tracked)
    with pytest.raises(AssertionError, match="Refusing session marker tracked in git"):
        verify_coverage_report(report)


def test_verify_coverage_report_refuses_nonfinite_and_ancient_markers(tmp_path: Path) -> None:
    """AS-TS-R19-SOL61-02: Non-finite timestamps (nan, inf) and ancient/forged timestamps (1, 1.0) must be refused."""
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "test-sess",
                "totals": {"percent_covered": 99.0, "covered_branches": 99, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / ".coverage_session_start"
    for bad_ts in ("nan", "NaN", "inf", "-inf"):
        marker.write_text(json.dumps({"session_id": "test-sess", "timestamp": bad_ts}), encoding="utf-8")
        with pytest.raises(AssertionError, match="Invalid (session marker timestamp|timestamp)"):
            verify_coverage_report(report)

    for ancient_ts in (1, 1.0, 1000, 999999999):
        marker.write_text(json.dumps({"session_id": "test-sess", "timestamp": ancient_ts}), encoding="utf-8")
        with pytest.raises(AssertionError, match="Ancient or forged marker timestamps are refused"):
            verify_coverage_report(report)


def test_verify_coverage_report_refuses_plain_float_or_non_json_marker(tmp_path: Path) -> None:
    """AS-TS-R20-02 / SOL61-03: Plain numeric floats and non-JSON marker files are refused."""
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "test-sess",
                "totals": {"percent_covered": 99.0, "covered_branches": 99, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / ".coverage_session_start"
    for bad_content in ("1700000000.0", "1700000100", "not json at all", "[1, 2, 3]"):
        marker.write_text(bad_content, encoding="utf-8")
        with pytest.raises(AssertionError, match="Corrupt session marker in"):
            verify_coverage_report(report)


def test_verify_coverage_report_refuses_missing_or_empty_session_id_in_marker(tmp_path: Path) -> None:
    """AS-TS-R20-02 / SOL61-03: Markers missing required non-empty session_id are refused."""
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "test-sess",
                "totals": {"percent_covered": 99.0, "covered_branches": 99, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / ".coverage_session_start"
    for payload in ({"timestamp": 1700000000.0}, {"session_id": "", "timestamp": 1700000000.0}):
        marker.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(AssertionError, match="missing required 'session_id'"):
            verify_coverage_report(report)


def test_verify_coverage_report_refuses_mismatched_session_id(tmp_path: Path) -> None:
    """AS-TS-R20-02 / SOL61-03: Stale or forged reports with mismatched session_id are refused."""
    import time

    now = time.time()
    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps({"session_id": "session-active", "timestamp": now, "report_digest": "0" * 64}),
        encoding="utf-8",
    )

    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "session-stale-or-forged",
                "totals": {"percent_covered": 99.0, "covered_branches": 99, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="does not match pytest session marker session_id"):
        verify_coverage_report(report)


def test_verify_coverage_report_accepts_valid_matching_session(tmp_path: Path) -> None:
    """AS-TS-R20-02 / SOL61-03: Valid matching session_id, digest, and fresh timestamp passes."""
    import hashlib
    import time

    now = time.time()
    report = tmp_path / "coverage.json"
    rep_content = json.dumps(
        {
            "session_id": "sess-ok",
            "totals": {"percent_covered": 95.0, "covered_branches": 95, "num_branches": 100},
        }
    )
    report.write_text(rep_content, encoding="utf-8")
    digest = hashlib.sha256(rep_content.encode("utf-8")).hexdigest()

    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps({"session_id": "sess-ok", "timestamp": now, "report_digest": digest}),
        encoding="utf-8",
    )
    percent, branch_pct = verify_coverage_report(report)
    assert percent == 95.0
    assert branch_pct == 95.0


def test_verify_coverage_report_marker_is_single_use(tmp_path: Path) -> None:
    """SOL61-04: Session marker is single-use; subsequent verification fails."""
    import hashlib
    import time

    now = time.time()
    report = tmp_path / "coverage.json"
    rep_content = json.dumps(
        {
            "session_id": "sess-single-use",
            "totals": {"percent_covered": 95.0, "covered_branches": 95, "num_branches": 100},
        }
    )
    report.write_text(rep_content, encoding="utf-8")
    rep_digest = hashlib.sha256(rep_content.encode("utf-8")).hexdigest()

    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps({"session_id": "sess-single-use", "timestamp": now, "report_digest": rep_digest}),
        encoding="utf-8",
    )

    percent, _ = verify_coverage_report(report)
    assert percent == 95.0

    # Second call fails because marker was consumed (single-use)
    with pytest.raises(AssertionError, match="missing session marker"):
        verify_coverage_report(report)


def test_verify_coverage_report_refuses_replayed_report_with_mismatched_digest(tmp_path: Path) -> None:
    """SOL61-04: Report digest mismatch between report and marker is refused."""
    import time

    now = time.time()
    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps(
            {
                "session_id": "sess-replayed",
                "timestamp": now,
                "report_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            }
        ),
        encoding="utf-8",
    )

    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "sess-replayed",
                "totals": {"percent_covered": 95.0, "covered_branches": 95, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="does not match session marker report_digest"):
        verify_coverage_report(report)


def test_replaying_older_report_with_forged_marker_fails(tmp_path: Path) -> None:
    """SOL61-04: Replaying older report with a forged marker is refused."""
    import time

    now = time.time()
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "previous-pytest-session",
                "totals": {"percent_covered": 95.0, "covered_branches": 95, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    old_time = now - 100.0
    os.utime(report, (old_time, old_time))

    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps({"session_id": "previous-pytest-session", "timestamp": now, "report_digest": "0" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="older than the current pytest run"):
        verify_coverage_report(report)


def test_marker_missing_report_digest_fails(tmp_path: Path) -> None:
    """TS22-04 / SOL61-04: Marker without report_digest is refused."""
    import time

    now = time.time()
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "session_id": "sess-no-digest",
                "totals": {"percent_covered": 95.0, "covered_branches": 95, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps({"session_id": "sess-no-digest", "timestamp": now}),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="report_digest"):
        verify_coverage_report(report)


def test_sol61_04_forged_marker_ancient_timestamp_replay_fails(tmp_path: Path) -> None:
    """SOL61-04 / ARGON-TS-R22-002: Replaying historical report (mtime=1700000001)
    with forged marker (ts=1700000000) is refused.
    """
    import hashlib

    report = tmp_path / "coverage.json"
    content = json.dumps(
        {
            "session_id": "previous-pytest-session",
            "totals": {"percent_covered": 95.0, "covered_branches": 95, "num_branches": 100},
        }
    )
    report.write_text(content, encoding="utf-8")
    os.utime(report, (1700000001.0, 1700000001.0))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps(
            {
                "session_id": "previous-pytest-session",
                "timestamp": 1700000000.0,
                "report_digest": digest,
            }
        ),
        encoding="utf-8",
    )
    expected_err = "Stale, ancient, or future marker timestamps are refused|outside allowed recency window"
    with pytest.raises(AssertionError, match=expected_err):
        verify_coverage_report(report)


def test_stale_report_outside_recency_window_fails(tmp_path: Path) -> None:
    """ARGON-TS-R22-002: Report with mtime older than allowed recency window (now - 7200s) fails closed."""
    import hashlib
    import time

    now = time.time()
    report = tmp_path / "coverage.json"
    content = json.dumps(
        {
            "session_id": "sess-too-old",
            "totals": {"percent_covered": 95.0, "covered_branches": 95, "num_branches": 100},
        }
    )
    report.write_text(content, encoding="utf-8")
    os.utime(report, (now - 7200.0, now - 7200.0))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps(
            {
                "session_id": "sess-too-old",
                "timestamp": now - 7300.0,
                "report_digest": digest,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="outside allowed recency window|Stale, ancient, or future"):
        verify_coverage_report(report)


def test_marker_unlink_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SOL61-04: Failure to consume marker must fail closed."""
    import hashlib
    import time

    now = time.time()
    report = tmp_path / "coverage.json"
    content = json.dumps(
        {
            "session_id": "sess-unlink-fail",
            "totals": {"percent_covered": 95.0, "covered_branches": 95, "num_branches": 100},
        }
    )
    report.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = tmp_path / ".coverage_session_start"
    marker.write_text(
        json.dumps({"session_id": "sess-unlink-fail", "timestamp": now, "report_digest": digest}),
        encoding="utf-8",
    )

    def _mock_unlink(self: Path) -> None:
        raise PermissionError("Access denied")

    monkeypatch.setattr(Path, "unlink", _mock_unlink)
    with pytest.raises(AssertionError, match="Failed to consume single-use coverage session marker"):
        verify_coverage_report(report)


def test_is_tracked_by_git_fatal_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-TS-R19-SOL61-03: When git exits with fatal error, is_tracked_by_git must fail closed."""

    class MockCompletedProcess:
        returncode = 128

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: MockCompletedProcess())
    assert is_tracked_by_git("some_file.json") is True


def test_is_tracked_by_git_unmatched_returns_untracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-TS-R19-SOL61-03: When git exits with 1 (unmatched), is_tracked_by_git returns False."""

    class MockCompletedProcess:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: MockCompletedProcess())
    assert is_tracked_by_git("some_file.json") is False


if __name__ == "__main__":
    sys.exit(main())
