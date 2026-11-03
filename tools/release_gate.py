"""Release gate verification.

Ensures that release creation cannot proceed unless required CI checks have
passed on the exact commit SHA. Fails closed if any required check is missing,
in progress, failed, or cancelled. Supports bounded polling for in-progress runs
and verifies producing app identity.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_REQUIRED_CHECKS = {
    "Skill Quality And Privacy",
    "Tests",
}

DEFAULT_EXPECTED_APP_SLUG = "github-actions"


def _parse_run_id(run_id: Any) -> int:
    """Parse run id into an integer for reliable numerical comparison."""
    if isinstance(run_id, bool) or run_id is None:
        return 0
    if isinstance(run_id, int):
        return run_id
    if isinstance(run_id, str):
        try:
            return int(run_id.strip())
        except ValueError:
            return 0
    return 0


def _matches_check_suite(run: dict[str, Any], expected_id: int | None) -> bool:
    """Check if run matches optional expected check suite ID."""
    if expected_id is None:
        return True
    cs = run.get("check_suite")
    cs_id = _parse_run_id(cs.get("id")) if isinstance(cs, dict) else _parse_run_id(run.get("check_suite_id"))
    return cs_id == expected_id


def _run_sort_key(run: dict[str, Any]) -> tuple[int, str, str]:
    """Extract sorting key for check runs to order by id and timestamp."""
    id_val = _parse_run_id(run.get("id"))
    completed_at = str(run.get("completed_at") or "")
    started_at = str(run.get("started_at") or "")
    return (id_val, completed_at, started_at)


def _evaluate_single_check(check_name: str, runs: list[dict[str, Any]]) -> list[str]:
    """Evaluate runs for a single required check name against the latest attempt."""
    if not runs:
        return [f"missing required check: {check_name!r}"]

    latest_run = max(enumerate(runs), key=lambda item: (*_run_sort_key(item[1]), item[0]))[1]
    status = latest_run.get("status")
    conclusion = latest_run.get("conclusion")

    reasons: list[str] = []
    if status != "completed":
        reasons.append(f"required check {check_name!r} is not completed (status={status!r})")
    elif conclusion != "success":
        reasons.append(f"required check {check_name!r} did not succeed (conclusion={conclusion!r})")

    return reasons


def _parse_optional_int(val: Any) -> int | None:
    """Parse integer or return None if empty/None."""
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            raise argparse.ArgumentTypeError(f"Invalid integer value: {val!r}")
    if isinstance(val, int):
        return val
    raise argparse.ArgumentTypeError(f"Invalid integer value: {val!r}")


def _matches_run(
    run: dict[str, Any],
    expected_sha: str | None,
    expected_app_slug: str | None,
    expected_check_suite_id: int | None,
) -> bool:
    """Check if an individual run matches the SHA, app slug, and check suite filters."""
    if expected_sha is not None and run.get("head_sha") != expected_sha:
        return False
    if expected_app_slug is not None:
        app = run.get("app")
        if not isinstance(app, dict) or app.get("slug") != expected_app_slug:
            return False
    return _matches_check_suite(run, expected_check_suite_id)


def _filter_matching_runs(
    raw_runs: list[dict[str, Any]],
    expected_sha: str | None,
    expected_app_slug: str | None,
    expected_check_suite_id: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Filter check runs by SHA, app slug, and optional check suite ID, grouping by run name."""
    if expected_app_slug is not None and not expected_app_slug.strip():
        raise ValueError("expected_app_slug must not be blank")
    runs_by_name: dict[str, list[dict[str, Any]]] = {}
    for run in raw_runs:
        name = run.get("name", "")
        if not name or not _matches_run(run, expected_sha, expected_app_slug, expected_check_suite_id):
            continue
        runs_by_name.setdefault(name, []).append(run)
    return runs_by_name


def evaluate_check_runs(
    check_runs_data: list[dict[str, Any]] | dict[str, Any],
    *,
    required_checks: set[str] | list[str] | None = None,
    expected_sha: str | None = None,
    expected_app_slug: str | None = DEFAULT_EXPECTED_APP_SLUG,
    expected_check_suite_id: int | None = None,
) -> tuple[bool, list[str]]:
    """Evaluate check run results against required checks, target SHA, and app.

    Returns:
        (passed, reasons) tuple.
        passed is True only if every required check is present, has status
        'completed', and has conclusion 'success'.
    """
    if expected_app_slug is not None and not expected_app_slug.strip():
        raise ValueError("expected_app_slug must not be blank")
    if required_checks is None:
        required_set = set(DEFAULT_REQUIRED_CHECKS)
    else:
        required_set = set(required_checks)

    if isinstance(check_runs_data, dict):
        raw_runs = check_runs_data.get("check_runs", [])
    else:
        raw_runs = check_runs_data

    runs_by_name = _filter_matching_runs(
        raw_runs,
        expected_sha,
        expected_app_slug,
        expected_check_suite_id=expected_check_suite_id,
    )

    reasons: list[str] = []
    for check_name in sorted(required_set):
        reasons.extend(_evaluate_single_check(check_name, runs_by_name.get(check_name, [])))

    return (len(reasons) == 0, reasons)


def _fetch_single_check_runs_page(url: str, token: str) -> tuple[dict[str, Any], str]:
    """Fetch a single page of check runs from GitHub API."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-skills-release-gate",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        link_header = resp.headers.get("Link", "") if hasattr(resp, "headers") else ""
        return (body, link_header)


def _validate_check_runs_total(raw_total: Any) -> int:
    if raw_total is None or not isinstance(raw_total, int) or isinstance(raw_total, bool) or raw_total < 0:
        raise RuntimeError("missing or invalid total_count in check runs response")
    return raw_total


def _has_more_pages(link_header: str, all_runs: list[Any], total_count: int, page_len: int) -> bool:
    return 'rel="next"' in link_header or (len(all_runs) < total_count and page_len > 0)


def fetch_github_check_runs(
    sha: str,
    *,
    token: str,
    repo: str,
    filter_policy: str = "all",
) -> dict[str, Any]:
    """Fetch all check runs from GitHub API for a commit SHA with full pagination."""
    page = 1
    all_runs: list[dict[str, Any]] = []
    total_count: int | None = None

    while True:
        url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?filter={filter_policy}&per_page=100&page={page}"
        body, link_header = _fetch_single_check_runs_page(url, token)
        raw_total = _validate_check_runs_total(body.get("total_count"))
        if total_count is None:
            total_count = raw_total

        runs = body.get("check_runs", [])
        all_runs.extend(runs)

        if not _has_more_pages(link_header, all_runs, total_count, len(runs)) or not runs:
            break
        page += 1

    if total_count is not None and len(all_runs) < total_count:
        raise RuntimeError(f"incomplete check runs retrieved: expected {total_count}, got {len(all_runs)}")

    return {"total_count": len(all_runs), "check_runs": all_runs}


def _evaluate_poll_attempt(
    sha: str,
    token: str,
    repo: str,
    required_checks: set[str] | list[str] | None,
    expected_app_slug: str | None,
    expected_check_suite_id: int | None,
    fetch_fn: Any,
) -> tuple[bool, bool, list[str]]:
    """Evaluate a single poll attempt. Return (done, passed, reasons)."""
    try:
        data = fetch_fn(sha, token=token, repo=repo)
    except Exception as exc:
        return (True, False, [f"failed to fetch check runs from GitHub API: {exc}"])

    passed, reasons = evaluate_check_runs(
        data,
        required_checks=required_checks,
        expected_sha=sha,
        expected_app_slug=expected_app_slug,
        expected_check_suite_id=expected_check_suite_id,
    )
    if passed:
        return (True, True, [])
    if any("did not succeed" in r for r in reasons):
        return (True, False, reasons)
    return (False, False, reasons)


def _poll_check_runs_gate(
    sha: str,
    *,
    token: str,
    repo: str,
    required_checks: set[str] | list[str] | None,
    expected_app_slug: str | None,
    expected_check_suite_id: int | None = None,
    wait_seconds: float,
    poll_interval_seconds: float,
    fetcher: Any,
    sleeper: Any,
    time_provider: Any,
) -> tuple[bool, list[str]]:
    """Poll GitHub API until checks pass, definitively fail, or timeout expires."""
    fetch_fn = fetcher or fetch_github_check_runs
    start_time = time_provider()

    while True:
        done, passed, reasons = _evaluate_poll_attempt(
            sha,
            token,
            repo,
            required_checks,
            expected_app_slug,
            expected_check_suite_id,
            fetch_fn,
        )
        if done:
            return (passed, reasons)

        elapsed = time_provider() - start_time
        if elapsed >= wait_seconds:
            return (False, reasons)

        sleep_duration = min(poll_interval_seconds, wait_seconds - elapsed)
        if sleep_duration > 0:
            sleeper(sleep_duration)


def verify_exact_commit_gate(
    sha: str,
    *,
    token: str | None = None,
    repo: str | None = None,
    check_runs_data: list[dict[str, Any]] | dict[str, Any] | None = None,
    required_checks: set[str] | list[str] | None = None,
    expected_app_slug: str | None = DEFAULT_EXPECTED_APP_SLUG,
    expected_check_suite_id: int | None = None,
    wait_seconds: float = 0.0,
    poll_interval_seconds: float = 5.0,
    fetcher: Any = None,
    sleeper: Any = time.sleep,
    time_provider: Any = time.monotonic,
) -> tuple[bool, list[str]]:
    """Fail-closed gate check for a commit with optional bounded polling.

    If check_runs_data is not provided, fetches live check runs from GitHub API.
    Fails closed if token/repo are missing or if network call fails.
    """
    if expected_app_slug is not None and not expected_app_slug.strip():
        raise ValueError("expected_app_slug must not be blank")

    if check_runs_data is not None:
        return evaluate_check_runs(
            check_runs_data,
            required_checks=required_checks,
            expected_sha=sha,
            expected_app_slug=expected_app_slug,
            expected_check_suite_id=expected_check_suite_id,
        )

    if not token or not repo:
        return (False, ["missing GH_TOKEN/GITHUB_TOKEN or GITHUB_REPOSITORY for release gate"])

    return _poll_check_runs_gate(
        sha,
        token=token,
        repo=repo,
        required_checks=required_checks,
        expected_app_slug=expected_app_slug,
        expected_check_suite_id=expected_check_suite_id,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        fetcher=fetcher,
        sleeper=sleeper,
        time_provider=time_provider,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify exact-commit required CI release gate")
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""), help="Commit SHA to verify")
    parser.add_argument("--check-runs-file", help="Path to JSON file containing check-runs data (for testing)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/repo")
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument(
        "--wait-seconds", type=float, default=0.0, help="Maximum seconds to wait for in-progress checks"
    )
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0, help="Seconds between poll requests")
    parser.add_argument(
        "--expected-app-slug", default=DEFAULT_EXPECTED_APP_SLUG, help="Expected app slug producing check runs"
    )
    parser.add_argument(
        "--check-suite-id",
        type=_parse_optional_int,
        default=None,
        help="Optional check suite ID to scope required checks to a specific workflow run",
    )
    args = parser.parse_args()

    if not args.sha:
        print("error: commit SHA is required (--sha or GITHUB_SHA)", file=sys.stderr)
        return 1

    if args.expected_app_slug is not None and not args.expected_app_slug.strip():
        print("error: --expected-app-slug must not be blank", file=sys.stderr)
        return 1

    check_runs_data: dict[str, Any] | list[dict[str, Any]] | None = None
    if args.check_runs_file:
        with open(args.check_runs_file, encoding="utf-8") as f:
            check_runs_data = json.load(f)

    passed, reasons = verify_exact_commit_gate(
        args.sha,
        token=args.token or None,
        repo=args.repo or None,
        check_runs_data=check_runs_data,
        expected_app_slug=args.expected_app_slug,
        expected_check_suite_id=args.check_suite_id,
        wait_seconds=args.wait_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )

    if not passed:
        print(f"Release gate rejected commit {args.sha[:10]}:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 1

    print(f"Release gate passed for commit {args.sha[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
