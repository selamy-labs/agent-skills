"""Regression tests for bounded AGY headless orchestration."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "skills" / "orchestrate-agy" / "scripts" / "run_headless.py"


def _make_fake_agy(tmp_path: Path) -> Path:
    fake_agy = tmp_path / "fake-agy.py"
    fake_agy.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print("1.1.25")
    raise SystemExit(0)
if args == ["--help"]:
    print(
        "--mode --model --effort --sandbox --output-format --print-timeout "
        "--log-file --print --dangerously-skip-permissions"
    )
    raise SystemExit(0)
if args == ["models"]:
    print("Fetching available models...", file=sys.stderr)
    print("gemini-test-medium\\tGemini Test (Medium)")
    raise SystemExit(0)

argv_path = os.environ.get("FAKE_AGY_ARGV")
if argv_path:
    pathlib.Path(argv_path).write_text(json.dumps(args))
log_path = pathlib.Path(args[args.index("--log-file") + 1])
log_path.write_text("fake AGY log\\n")

behavior = os.environ.get("FAKE_AGY_BEHAVIOR", "success")
if behavior == "success":
    print(json.dumps({"conversation_id": "conversation-1", "status": "SUCCESS", "response": "done\\n"}))
elif behavior == "permission":
    print(json.dumps({"conversation_id": "conversation-2", "status": "SUCCESS", "response": ""}))
    print(
        'jetski: no output produced — a tool required the "command" permission '
        "that headless mode cannot prompt for, so it was auto-denied.",
        file=sys.stderr,
    )
elif behavior == "nonzero":
    print(json.dumps({"conversation_id": "conversation-3", "status": "ERROR", "response": "", "error": "failed"}))
    raise SystemExit(7)
elif behavior == "empty":
    pass
elif behavior == "malformed":
    print("not json")
elif behavior == "waiting":
    print(
        json.dumps(
            {"conversation_id": "conversation-4", "status": "WAITING", "response": "", "error": "input required"}
        )
    )
elif behavior == "timeout":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pathlib.Path(os.environ["FAKE_AGY_CHILD_PID"]).write_text(str(child.pid))
    time.sleep(60)
else:
    raise AssertionError(f"unknown behavior: {behavior}")
"""
    )
    fake_agy.chmod(0o755)
    return fake_agy


def _run_runner(
    tmp_path: Path,
    *,
    behavior: str = "success",
    model: str = "gemini-test-medium",
    extra_args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
    timeout_seconds: int = 2,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text('First $HOME $(not-executed) "quotes"\nSecond `backticks`; apostrophe\'s value\n')
    fake_agy = _make_fake_agy(tmp_path)
    run_dir = tmp_path / "evidence" / "run-1"
    argv_path = tmp_path / "argv.json"
    env = os.environ | {
        "FAKE_AGY_ARGV": str(argv_path),
        "FAKE_AGY_BEHAVIOR": behavior,
    }
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--run-dir",
            str(run_dir),
            "--cwd",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--agy",
            str(fake_agy),
            "--timeout-seconds",
            str(timeout_seconds),
            "--mode",
            "plan",
            "--model",
            model,
            "--effort",
            "high",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result, run_dir, argv_path, prompt


def _status(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "status.json").read_text())


def test_runner_constructs_a_sandboxed_bounded_command(tmp_path: Path) -> None:
    result, run_dir, argv_path, prompt = _run_runner(tmp_path)

    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_path.read_text())
    assert argv[-2:] == ["--print", prompt.read_text()]
    print_position = argv.index("--print")
    for flag in ("--mode", "--model", "--effort", "--sandbox", "--output-format", "--print-timeout", "--log-file"):
        assert argv.index(flag) < print_position
    assert argv[argv.index("--mode") + 1] == "plan"
    assert argv[argv.index("--model") + 1] == "gemini-test-medium"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--print-timeout") + 1] == "2s"
    assert (run_dir / "prompt.txt").read_text() == prompt.read_text()
    assert stat.S_IMODE((run_dir / "prompt.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "status.json").stat().st_mode) == 0o600
    assert json.loads((run_dir / "command.json").read_text())[-1] == "<prompt from prompt.txt>"
    assert (run_dir / "agy-version.stdout").read_text().strip() == "1.1.25"
    assert "--print-timeout" in (run_dir / "agy-help.stdout").read_text()
    assert "gemini-test-medium" in (run_dir / "agy-models.stdout").read_text()
    assert _status(run_dir)["classification"] == "succeeded"


def test_runner_classifies_zero_exit_empty_response_as_permission_blocked(tmp_path: Path) -> None:
    result, run_dir, _, _ = _run_runner(tmp_path, behavior="permission")

    assert result.returncode != 0
    assert _status(run_dir)["classification"] == "permission_blocked"
    assert "headless mode cannot prompt" in (run_dir / "stderr.log").read_text()


@pytest.mark.parametrize(
    ("behavior", "classification"),
    [
        pytest.param("nonzero", "cli_error", id="nonzero-exit"),
        pytest.param("empty", "no_output", id="empty-stdout"),
        pytest.param("malformed", "invalid_output", id="malformed-json"),
        pytest.param("waiting", "agy_status_WAITING", id="non-success-status"),
    ],
)
def test_runner_rejects_terminal_failure_outputs(tmp_path: Path, behavior: str, classification: str) -> None:
    result, run_dir, _, _ = _run_runner(tmp_path, behavior=behavior)

    assert result.returncode != 0
    assert _status(run_dir)["classification"] == classification


def test_runner_rejects_a_model_missing_from_live_discovery(tmp_path: Path) -> None:
    result, run_dir, argv_path, _ = _run_runner(tmp_path, model="missing-model")

    assert result.returncode != 0
    assert _status(run_dir)["classification"] == "capability_mismatch"
    assert not argv_path.exists()


def test_runner_harvests_the_process_group_after_wall_timeout(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    result, run_dir, _, _ = _run_runner(
        tmp_path,
        behavior="timeout",
        timeout_seconds=1,
        extra_env={"FAKE_AGY_CHILD_PID": str(child_pid_path)},
    )

    assert result.returncode == 124
    status = _status(run_dir)
    assert status["classification"] == "timed_out"
    assert status["process_group_alive_after_harvest"] is False
    deadline = time.monotonic() + 2
    child_pid = int(child_pid_path.read_text())
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"child process {child_pid} survived timeout harvesting")


def test_runner_rejects_permission_bypass_without_acknowledgement(tmp_path: Path) -> None:
    result, run_dir, _, _ = _run_runner(tmp_path, extra_args=("--allow-all-permissions",))

    assert result.returncode == 2
    assert not run_dir.exists()


def test_runner_allows_explicitly_acknowledged_permission_bypass(tmp_path: Path) -> None:
    result, _, argv_path, _ = _run_runner(
        tmp_path,
        extra_args=("--allow-all-permissions",),
        extra_env={"ORCHESTRATE_AGY_PERMISSION_BYPASS_ACK": "authorized"},
    )

    assert result.returncode == 0, result.stderr
    assert "--dangerously-skip-permissions" in json.loads(argv_path.read_text())


def test_skill_documents_the_headless_control_contract() -> None:
    skill = (REPO_ROOT / "skills" / "orchestrate-agy" / "SKILL.md").read_text().lower()
    required_guidance = (
        "run_headless.py",
        "headless `plan`",
        "headless `accept-edits`",
        "permissions.allow",
        "--dangerously-skip-permissions",
        "permission_blocked",
        "fallback",
        "one writer",
        "independently inspect",
    )

    missing = [item for item in required_guidance if item not in skill]
    assert not missing, f"missing AGY headless guidance: {missing}"
