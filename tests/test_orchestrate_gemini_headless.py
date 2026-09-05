"""Regression tests for contained Gemini CLI headless orchestration."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "skills" / "orchestrate-gemini" / "scripts" / "run_headless.py"


def _run(command: list[str], cwd: Path) -> str:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir()
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.name", "Test Operator"], path)
    _run(["git", "config", "user.email", "operator@example.com"], path)
    (path / "README.md").write_text("fixture\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-qm", "fixture"], path)
    return _run(["git", "rev-parse", "HEAD"], path)


def _write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def _fake_gemini(path: Path) -> Path:
    executable = path / "gemini"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('0.51.0')\n"
        "elif '--help' in sys.argv:\n"
        "    print('--model --output-format --approval-mode --sandbox --admin-policy --extensions --resume')\n"
        "else:\n"
        "    raise SystemExit(97)\n"
    )
    executable.chmod(0o755)
    return executable


def _fake_provider(path: Path, name: str = "docker") -> Path:
    bindir = path / "bin"
    bindir.mkdir(exist_ok=True)
    provider = bindir / name
    provider.write_text("#!/bin/sh\nexit 0\n")
    provider.chmod(0o755)
    return bindir


def _goal(base_sha: str, **overrides: object) -> dict[str, object]:
    goal: dict[str, object] = {
        "schema_version": 1,
        "goal_id": "gemini-contained-test",
        "objective": "Make the bounded fixture satisfy its tests.",
        "base_sha": base_sha,
        "allowed_paths": ["README.md", "src/", "tests/"],
        "verification_commands": [
            {"argv": ["git", "status", "--short"], "timeout_seconds": 5},
        ],
        "stop_conditions": ["scope would expand", "credentials are unavailable"],
        "max_attempts": 3,
        "auth_type": "oauth-personal",
        "allow_paid_generation": False,
    }
    goal.update(overrides)
    return goal


def _policy(path: Path) -> Path:
    policy = path / "policy.toml"
    _write_private(
        policy,
        '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\n\n'
        '[[rule]]\ntoolName = "read_file"\ndecision = "allow"\npriority = 100\n',
    )
    return policy


def _fixture(tmp_path: Path, **goal_overrides: object) -> dict[str, object]:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    goal_file = tmp_path / "goal.json"
    _write_private(goal_file, json.dumps(_goal(base_sha, **goal_overrides)))
    prompt = tmp_path / "prompt.txt"
    _write_private(prompt, "Inspect the fixture without changing it.\n")
    return {
        "repo": repo,
        "base_sha": base_sha,
        "state": state,
        "goal": goal_file,
        "prompt": prompt,
        "policy": _policy(tmp_path),
        "gemini": _fake_gemini(tmp_path),
        "path": _fake_provider(tmp_path),
    }


def _invoke(
    fixture: dict[str, object],
    run_name: str,
    *,
    goal_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    run_dir = Path(fixture["state"]) / "runs" / run_name
    command = [
        sys.executable,
        str(RUNNER),
        "--run-dir",
        str(run_dir),
        "--state-dir",
        str(fixture["state"]),
        "--cwd",
        str(fixture["repo"]),
        "--goal-file",
        str(goal_file or fixture["goal"]),
        "--prompt-file",
        str(fixture["prompt"]),
        "--policy-file",
        str(fixture["policy"]),
        "--gemini",
        str(fixture["gemini"]),
        "--sandbox-provider",
        "docker",
        "--timeout-seconds",
        "5",
        "--preflight-only",
    ]
    env = os.environ | {"PATH": f"{fixture['path']}:{os.environ['PATH']}"} | (extra_env or {})
    return subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)


def _status(fixture: dict[str, object], run_name: str) -> dict[str, object]:
    path = Path(fixture["state"]) / "runs" / run_name / "status.json"
    return json.loads(path.read_text())


def test_preflight_persists_an_immutable_durable_goal(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    result = _invoke(fixture, "attempt-001")

    assert result.returncode == 0, result.stderr
    assert _status(fixture, "attempt-001")["classification"] == "preflight_succeeded"
    canonical_goal = Path(fixture["state"]) / "goal.json"
    assert json.loads(canonical_goal.read_text())["goal_id"] == "gemini-contained-test"
    assert canonical_goal.stat().st_mode & 0o777 == 0o600
    assert (Path(fixture["state"]) / "goal.sha256").stat().st_mode & 0o777 == 0o600


def test_preflight_rejects_a_changed_durable_goal(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    assert _invoke(fixture, "attempt-001").returncode == 0
    changed = tmp_path / "changed-goal.json"
    original = json.loads(Path(fixture["goal"]).read_text())
    original["objective"] = "A different objective"
    _write_private(changed, json.dumps(original))

    result = _invoke(fixture, "attempt-002", goal_file=changed)

    assert result.returncode != 0
    assert _status(fixture, "attempt-002")["classification"] == "goal_mismatch"


def test_preflight_rejects_an_exhausted_attempt_budget(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, max_attempts=1)
    assert _invoke(fixture, "attempt-001").returncode == 0

    result = _invoke(fixture, "attempt-002")

    assert result.returncode != 0
    assert _status(fixture, "attempt-002")["classification"] == "attempt_exhausted"


def test_preflight_rejects_escaping_allowed_paths(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, allowed_paths=["../outside"])

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "invalid_goal"


def test_preflight_requires_the_exact_clean_base(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (Path(fixture["repo"]) / "README.md").write_text("dirty\n")

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "git_dirty"


def test_preflight_rejects_shared_git_metadata(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    source = Path(fixture["repo"])
    linked = tmp_path / "linked"
    _run(["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)], source)
    fixture["repo"] = linked

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "shared_git_dir"


def test_preflight_uses_a_kernel_backed_single_writer_lease(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    lease = Path(fixture["state"]) / "lease.lock"
    lease.touch(mode=0o600)
    with lease.open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "lane_locked"


def test_preflight_rejects_state_nested_under_the_checkout(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    nested = Path(fixture["repo"]) / "lane-state"
    nested.mkdir(mode=0o700)
    fixture["state"] = nested

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert "invalid_path" in result.stderr
    assert not (nested / "runs" / "attempt-001").exists()
