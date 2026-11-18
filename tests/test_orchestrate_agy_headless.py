"""Regression tests for bounded AGY headless orchestration."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

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
    time.sleep(float(os.environ.get("FAKE_AGY_PROBE_DELAY", "0")))
    print("1.1.25")
    raise SystemExit(0)
if args == ["--help"]:
    time.sleep(float(os.environ.get("FAKE_AGY_PROBE_DELAY", "0")))
    print(
        "--mode --model --effort --sandbox --input-format --output-format --print-timeout "
        "--log-file --print --dangerously-skip-permissions",
        file=sys.stderr,
    )
    raise SystemExit(0)
if args == ["models"]:
    time.sleep(float(os.environ.get("FAKE_AGY_PROBE_DELAY", "0")))
    print("Fetching available models...", file=sys.stderr)
    print("gemini-test-medium\\tGemini Test (Medium)")
    raise SystemExit(0)

argv_path = os.environ.get("FAKE_AGY_ARGV")
if argv_path:
    pathlib.Path(argv_path).write_text(json.dumps(args))
log_path = pathlib.Path(args[args.index("--log-file") + 1])
log_path.write_text("fake AGY log\\n")
input_event = json.loads(sys.stdin.readline())
prompt = input_event["message"]["content"]
pathlib.Path(os.environ["FAKE_AGY_PROMPT"]).write_text(prompt)

def emit(envelope):
    print(json.dumps({"event": "result", "result": envelope}))


def record_child_pid(pid):
    tmp_pid = pathlib.Path(os.environ["FAKE_AGY_CHILD_PID"] + ".tmp")
    tmp_pid.write_text(str(pid), encoding="utf-8")
    os.replace(tmp_pid, os.environ["FAKE_AGY_CHILD_PID"])

behavior = os.environ.get("FAKE_AGY_BEHAVIOR", "success")
if behavior == "success":
    emit({"conversation_id": "conversation-1", "status": "SUCCESS", "response": "done\\n"})
elif behavior == "permission":
    emit({"conversation_id": "conversation-2", "status": "SUCCESS", "response": ""})
    print(
        'jetski: no output produced — a tool required the "command" permission '
        "that headless mode cannot prompt for, so it was auto-denied.",
        file=sys.stderr,
    )
elif behavior == "nonzero":
    emit({"conversation_id": "conversation-3", "status": "ERROR", "response": "", "error": "failed"})
    raise SystemExit(7)
elif behavior == "empty":
    pass
elif behavior == "malformed":
    print("not json")
elif behavior == "waiting":
    emit({"conversation_id": "conversation-4", "status": "WAITING", "response": "", "error": "input required"})
elif behavior == "timeout":
    child = subprocess.Popen(["/bin/sleep", "60"])
    record_child_pid(child.pid)
    time.sleep(60)
elif behavior == "escape":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    record_child_pid(child.pid)
    time.sleep(0.3)
    emit({"conversation_id": "conversation-5", "status": "SUCCESS", "response": "done\\n"})
else:
    raise AssertionError(f"unknown behavior: {behavior}")
"""
    )
    fake_agy.chmod(0o755)
    return fake_agy


def _prepare_runner_workspace(
    tmp_path: Path,
    *,
    model: str = "gemini-test-medium",
    extra_args: tuple[str, ...] = (),
    timeout_seconds: int = 5,
    agy_path: Path | None = None,
) -> tuple[list[str], dict[str, str], Path, Path]:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text('First $HOME $(not-executed) "quotes"\nSecond `backticks`; apostrophe\'s value\n')
    fake_agy = agy_path or _make_fake_agy(tmp_path)
    run_dir = tmp_path / "evidence" / "run-1"
    observed_prompt_path = tmp_path / "observed-prompt.txt"
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    runner_args = [
        "--run-dir",
        str(run_dir),
        "--cwd",
        str(cwd),
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
    ]
    fake_env = {"FAKE_AGY_PROMPT": str(observed_prompt_path)}
    return runner_args, fake_env, run_dir, prompt


def _run_runner(
    tmp_path: Path,
    *,
    behavior: str = "success",
    model: str = "gemini-test-medium",
    extra_args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
    timeout_seconds: int = 5,
    agy_path: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    runner_args, fake_env, run_dir, prompt = _prepare_runner_workspace(
        tmp_path,
        model=model,
        extra_args=extra_args,
        timeout_seconds=timeout_seconds,
        agy_path=agy_path,
    )
    argv_path = tmp_path / "argv.json"
    env = os.environ | fake_env | {"FAKE_AGY_ARGV": str(argv_path), "FAKE_AGY_BEHAVIOR": behavior}
    if extra_env:
        env.update(extra_env)

    command = [sys.executable, str(RUNNER), *runner_args]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except BaseException:
        _kill_test_process_tree(process)
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    return result, run_dir, argv_path, prompt


def _descendant_pids(root_pid: int) -> set[int]:
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid="],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    ).stdout
    children: dict[int, list[int]] = {}
    for line in output.splitlines():
        pid, parent_pid = (int(value) for value in line.split())
        children.setdefault(parent_pid, []).append(pid)
    descendants: set[int] = set()
    frontier = [root_pid]
    while frontier:
        for child_pid in children.get(frontier.pop(), []):
            if child_pid not in descendants:
                descendants.add(child_pid)
                frontier.append(child_pid)
    return descendants


def _kill_test_process_tree(process: subprocess.Popen[str], known_pids: set[int] | None = None) -> None:
    targets = _descendant_pids(process.pid) | (known_pids or set())
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (PermissionError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _kill_known_pids(pids: set[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass


def _read_optional_pid(pid_path: Path) -> int | None:
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _read_status_payload(run_dir: Path | None) -> dict[str, object]:
    if run_dir is None:
        return {}
    status_path = run_dir / "status.json"
    try:
        raw = status_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _kill_foreign_group(pgid: object) -> None:
    if not isinstance(pgid, int) or pgid <= 0 or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (PermissionError, ProcessLookupError):
        pass


def _kill_pid_file_and_run_group(known_pids: set[int], child_pid_path: Path, run_dir: Path | None = None) -> None:
    recorded_pid = _read_optional_pid(child_pid_path)
    if recorded_pid is not None:
        known_pids.add(recorded_pid)
    payload = _read_status_payload(run_dir)
    _kill_foreign_group(payload.get("process_group_id"))
    pid = payload.get("pid")
    if isinstance(pid, int) and pid > 0 and pid != os.getpid():
        known_pids.add(pid)
    _kill_known_pids(known_pids)


def _spawn_sleep_and_record_pid_script(sleep_seconds: float) -> str:
    return (
        "import os, pathlib, subprocess, sys, time; "
        "child = subprocess.Popen(['/bin/sleep', '60']); "
        "tmp = sys.argv[1] + '.tmp'; "
        "pathlib.Path(tmp).write_text(str(child.pid), encoding='utf-8'); "
        "os.replace(tmp, sys.argv[1]); "
        f"time.sleep({sleep_seconds})"
    )


def _pid_is_running(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _load_runner_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("orchestrate_agy_run_headless", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _status(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "status.json").read_text())


def test_runner_constructs_a_sandboxed_bounded_command(tmp_path: Path) -> None:
    result, run_dir, argv_path, prompt = _run_runner(tmp_path)

    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_path.read_text())
    assert prompt.read_text() not in argv
    assert "--print" not in argv
    for flag in (
        "--mode",
        "--model",
        "--effort",
        "--sandbox",
        "--input-format",
        "--output-format",
        "--print-timeout",
        "--log-file",
    ):
        assert flag in argv
    assert argv[argv.index("--mode") + 1] == "plan"
    assert argv[argv.index("--model") + 1] == "gemini-test-medium"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert (run_dir / "prompt.txt").read_text() == prompt.read_text()
    assert json.loads((run_dir / "input.ndjson").read_text())["message"]["content"] == prompt.read_text()
    assert (tmp_path / "observed-prompt.txt").read_text() == prompt.read_text()
    assert stat.S_IMODE((run_dir / "prompt.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "status.json").stat().st_mode) == 0o600
    assert prompt.read_text() not in (run_dir / "command.json").read_text()
    assert (run_dir / "agy-version.stdout").read_text().strip() == "1.1.25"
    assert "--print-timeout" in (run_dir / "agy-help.stderr").read_text()
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


def _monotonic_frozen_until_pid_file(
    pid_path: Path,
    max_freeze_seconds: float = 15.0,
) -> SimpleNamespace:
    real_monotonic = time.monotonic
    started = real_monotonic()
    ready_at: float | None = None

    def monotonic() -> float:
        nonlocal ready_at
        now = real_monotonic()
        if ready_at is None:
            is_ready = pid_path.exists() and pid_path.stat().st_size > 0
            if not is_ready and now - started < max_freeze_seconds:
                return started
            ready_at = now
        return started + (now - ready_at)

    return SimpleNamespace(monotonic=monotonic, sleep=time.sleep)


def test_runner_harvests_the_process_group_after_wall_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner_module()
    child_pid_path = tmp_path / "child.pid"
    runner_args, fake_env, run_dir, _ = _prepare_runner_workspace(tmp_path, timeout_seconds=1)
    for key, value in (fake_env | {"FAKE_AGY_BEHAVIOR": "timeout", "FAKE_AGY_CHILD_PID": str(child_pid_path)}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["run_headless.py", *runner_args])
    monkeypatch.setattr(runner, "time", _monotonic_frozen_until_pid_file(child_pid_path))

    known_pids: set[int] = set()
    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        returncode = runner.main()

        assert returncode == 124
        status = _status(run_dir)
        assert status["classification"] == "timed_out"
        assert status["agent_timeout_seconds"] == 1
        assert status["process_group_alive_after_harvest"] is False
        deadline = time.monotonic() + 2
        child_pid = int(child_pid_path.read_text())
        known_pids.add(child_pid)
        while time.monotonic() < deadline:
            if not _pid_is_running(child_pid):
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"child process {child_pid} survived timeout harvesting")
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)
        _kill_pid_file_and_run_group(known_pids, child_pid_path, run_dir)


def test_runner_harvests_a_descendant_that_starts_a_new_session(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    known_pids: set[int] = set()
    try:
        result, run_dir, _, _ = _run_runner(
            tmp_path,
            behavior="escape",
            extra_env={"FAKE_AGY_CHILD_PID": str(child_pid_path)},
        )

        assert result.returncode == 0, result.stderr
        child_pid = int(child_pid_path.read_text())
        known_pids.add(child_pid)
        assert child_pid in _status(run_dir)["observed_descendant_pids"]
        assert not _pid_is_running(child_pid)
    finally:
        _kill_known_pids(known_pids)


def test_run_process_harvests_when_on_start_callback_raises(tmp_path: Path) -> None:
    runner = _load_runner_module()
    known_pids: set[int] = set()

    def fail_after_start(pid: int, _process_group_id: int) -> None:
        known_pids.add(pid)
        raise RuntimeError("injected status-write failure")

    try:
        with pytest.raises(RuntimeError, match="injected status-write failure"):
            runner.run_process(
                ["/bin/sleep", "60"],
                tmp_path,
                tmp_path / "stdout.log",
                tmp_path / "stderr.log",
                5,
                on_start=fail_after_start,
            )
        assert known_pids
        assert all(not _pid_is_running(pid) for pid in known_pids)
    finally:
        _kill_known_pids(known_pids)


def test_early_interruption_uses_only_the_short_cleanup_grace(tmp_path: Path) -> None:
    runner = _load_runner_module()
    runner.WALL_TIMEOUT_GRACE_SECONDS = 0.3
    runner.TERMINATION_GRACE_SECONDS = 0.1
    runner.process_snapshot = lambda _timeout_seconds=2: runner.ProcessInventory({}, False)
    known_pids: set[int] = set()

    def interrupt_after_start(pid: int, _process_group_id: int) -> None:
        known_pids.add(pid)
        raise runner.SupervisorInterrupted(signal.SIGINT)

    started = time.monotonic()
    try:
        with pytest.raises(runner.SupervisorInterrupted):
            runner.run_process(
                ["/bin/sleep", "60"],
                tmp_path,
                tmp_path / "stdout.log",
                tmp_path / "stderr.log",
                1.5,
                on_start=interrupt_after_start,
            )
        assert time.monotonic() - started < 0.7
        assert all(not _pid_is_running(pid) for pid in known_pids)
    finally:
        _kill_known_pids(known_pids)


def test_process_identity_rejects_a_reused_pid() -> None:
    runner = _load_runner_module()
    tracked_processes = {123: runner.ProcessIdentity("Mon Jan  1 00:00:00 2024")}
    snapshot = {
        123: runner.ProcessInfo(
            parent_pid=1,
            process_group_id=123,
            state="S",
            started_at="Tue Jan  2 00:00:00 2024",
        )
    }

    assert runner.tracked_descendant_state(
        999,
        tracked_processes,
        runner.ProcessInventory(snapshot, True),
    ) == ("absent", [])


def test_tracked_descendant_state_is_independent_of_process_group() -> None:
    runner = _load_runner_module()
    tracked_processes = {
        123: runner.ProcessIdentity("Mon Jan  1 00:00:00 2024"),
        456: runner.ProcessIdentity("Mon Jan  1 00:00:01 2024"),
    }
    same_group_alive = runner.ProcessInventory(
        {
            456: runner.ProcessInfo(
                parent_pid=123,
                process_group_id=123,
                state="S",
                started_at="Mon Jan  1 00:00:01 2024",
            )
        },
        True,
    )

    assert runner.tracked_descendant_state(
        123,
        tracked_processes,
        same_group_alive,
    ) == ("alive", [456])
    assert runner.tracked_descendant_state(
        123,
        tracked_processes,
        runner.ProcessInventory({}, False),
    ) == ("unknown", [])
    assert runner.tracked_descendant_state(
        123,
        tracked_processes,
        runner.ProcessInventory({}, True),
    ) == ("absent", [])


def test_harvest_directly_signals_a_tracked_same_group_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    inventory = runner.ProcessInventory(
        {
            123: runner.ProcessInfo(1, 123, "S", "Mon Jan  1 00:00:00 2024"),
            456: runner.ProcessInfo(123, 123, "S", "Mon Jan  1 00:00:01 2024"),
        },
        True,
    )
    tracked_processes = {
        123: runner.ProcessIdentity("Mon Jan  1 00:00:00 2024"),
        456: runner.ProcessIdentity("Mon Jan  1 00:00:01 2024"),
    }
    process_signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(runner, "snapshot_before", lambda _deadline: inventory)
    monkeypatch.setattr(runner, "signal_process_group", lambda _pgid, _signum: False)
    monkeypatch.setattr(
        runner,
        "signal_process",
        lambda pid, signum: process_signals.append((pid, signum)) or True,
    )
    monkeypatch.setattr(
        runner,
        "wait_for_harvest",
        lambda *_args: ("absent", "absent", [], inventory),
    )

    runner.harvest_process_tree(
        123,
        123,
        tracked_processes,
        time.monotonic() + 1,
    )

    assert (456, signal.SIGTERM) in process_signals


def test_run_process_harvests_owned_group_when_process_listing_fails(tmp_path: Path) -> None:
    runner = _load_runner_module()
    child_pid_path = tmp_path / "child.pid"
    known_pids: set[int] = set()
    script = _spawn_sleep_and_record_pid_script(0.2)
    runner.process_snapshot = lambda _timeout_seconds=2: runner.ProcessInventory({}, False)

    try:
        result = runner.run_process(
            [sys.executable, "-c", script, str(child_pid_path)],
            tmp_path,
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
            5,
        )
        assert result.returncode == 0
        child_pid = int(child_pid_path.read_text())
        known_pids.add(child_pid)
        assert not _pid_is_running(child_pid)
        assert result.process_group_alive_after_harvest is False
    finally:
        _kill_pid_file_and_run_group(known_pids, child_pid_path)


def test_run_process_times_out_cleanly_without_process_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner_module()
    child_pid_path = tmp_path / "child.pid"
    known_pids: set[int] = set()
    script = _spawn_sleep_and_record_pid_script(60)
    monkeypatch.setattr(
        runner,
        "process_snapshot",
        lambda _timeout_seconds=2: runner.ProcessInventory({}, False),
    )
    monkeypatch.setattr(runner, "time", _monotonic_frozen_until_pid_file(child_pid_path))

    try:
        result = runner.run_process(
            [sys.executable, "-c", script, str(child_pid_path)],
            tmp_path,
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
            0.2,
        )
        assert result.timed_out is True
        assert result.process_group_state_after_harvest in {"absent", "unknown"}
        assert result.process_group_alive_after_harvest is (result.process_group_state_after_harvest == "unknown")
        child_pid = int(child_pid_path.read_text())
        known_pids.add(child_pid)
        assert not _pid_is_running(child_pid)
    finally:
        _kill_pid_file_and_run_group(known_pids, child_pid_path)


def test_permission_denied_group_probe_is_unknown_unless_snapshot_proves_zombies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()

    def deny_probe(_process_group_id: int, _signum: int) -> None:
        raise PermissionError("injected EPERM")

    monkeypatch.setattr(runner.os, "killpg", deny_probe)
    unrelated = {
        456: runner.ProcessInfo(1, 456, "S", "Mon Jan  1 00:00:00 2024"),
    }
    zombies = {
        123: runner.ProcessInfo(1, 123, "Z", "Mon Jan  1 00:00:00 2024"),
    }

    assert runner.process_group_state(123, runner.ProcessInventory(unrelated, False)) == "unknown"
    assert runner.process_group_state(123, runner.ProcessInventory({}, False)) == "unknown"
    assert runner.process_group_state(123, runner.ProcessInventory(zombies, False)) == "unknown"
    assert runner.process_group_state(123, runner.ProcessInventory(zombies, True)) == "absent"


def test_successful_group_probe_uses_inventory_to_distinguish_live_and_zombie_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    monkeypatch.setattr(runner.os, "killpg", lambda _process_group_id, _signum: None)
    live = {
        123: runner.ProcessInfo(1, 123, "S", "Mon Jan  1 00:00:00 2024"),
    }
    zombies = {
        123: runner.ProcessInfo(1, 123, "Z", "Mon Jan  1 00:00:00 2024"),
    }

    assert runner.process_group_state(123, runner.ProcessInventory(live, True)) == "alive"
    assert runner.process_group_state(123, runner.ProcessInventory(zombies, False)) == "unknown"
    assert runner.process_group_state(123, runner.ProcessInventory(zombies, True)) == "absent"


def test_unknown_group_state_is_a_harvest_failure(tmp_path: Path) -> None:
    runner = _load_runner_module()
    result = runner.ProcessResult(
        returncode=-9,
        pid=123,
        process_group_id=123,
        timed_out=True,
        survivors_harvested=True,
        process_group_alive_after_harvest=True,
        process_group_state_after_harvest="unknown",
        descendant_state_after_harvest="absent",
        observed_descendant_pids=[],
        descendants_alive_after_harvest=[],
        interrupted_signal=None,
        launch_error=None,
    )

    classification, envelope = runner.classify_result(
        result,
        tmp_path / "unused.stdout",
        tmp_path / "unused.stderr",
    )

    assert classification == "harvest_failed"
    assert envelope is None


def test_capability_probe_rejects_unknown_descendant_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    run_dir = tmp_path / "evidence"
    run_dir.mkdir()
    result = runner.ProcessResult(
        returncode=0,
        pid=123,
        process_group_id=123,
        timed_out=False,
        survivors_harvested=True,
        process_group_alive_after_harvest=False,
        process_group_state_after_harvest="absent",
        descendant_state_after_harvest="unknown",
        observed_descendant_pids=[456],
        descendants_alive_after_harvest=[],
        interrupted_signal=None,
        launch_error=None,
    )
    monkeypatch.setattr(runner, "probe", lambda *_args: result)
    started = time.monotonic()

    exit_code = runner.run_capability_probes(
        SimpleNamespace(run_dir=run_dir, cwd=tmp_path, agy=Path("/fake/agy")),
        {},
        started,
        started + 5,
    )

    assert exit_code == 1
    status = json.loads((run_dir / "status.json").read_text())
    assert status["classification"] == "capability_probe_failed"
    assert status["descendant_state_after_harvest"] == "unknown"


def test_run_process_escalates_group_cleanup_with_partial_process_listing(tmp_path: Path) -> None:
    runner = _load_runner_module()
    child_pid_path = tmp_path / "child.pid"
    known_pids: set[int] = set()
    child_script = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "tmp = sys.argv[1] + '.tmp'; "
        "pathlib.Path(tmp).write_text(str(os.getpid())); "
        "os.replace(tmp, sys.argv[1]); "
        "time.sleep(60)"
    )
    parent_script = (
        "import signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}, sys.argv[1]]); "
        "time.sleep(60)"
    )
    unrelated_snapshot = {
        999_999: runner.ProcessInfo(
            parent_pid=1,
            process_group_id=999_999,
            state="S",
            started_at="Mon Jan  1 00:00:00 2024",
        )
    }
    timed_out_triggered = False

    def sync_snapshot(_timeout_seconds: float = 2) -> runner.ProcessInventory | None:
        nonlocal timed_out_triggered
        if child_pid_path.exists() and child_pid_path.stat().st_size > 0 and not timed_out_triggered:
            timed_out_triggered = True
            return None
        return runner.ProcessInventory(unrelated_snapshot, False)

    runner.process_snapshot = sync_snapshot

    try:
        result = runner.run_process(
            [sys.executable, "-c", parent_script, str(child_pid_path)],
            tmp_path,
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
            5,
        )
        assert result.timed_out is True
        assert result.process_group_state_after_harvest in {"absent", "unknown"}
        child_pid = int(child_pid_path.read_text())
        known_pids.add(child_pid)
        assert not _pid_is_running(child_pid)
    finally:
        _kill_known_pids(known_pids)


def test_detached_descendant_is_unknown_when_cleanup_inventory_disappears(tmp_path: Path) -> None:
    runner = _load_runner_module()
    runner.WALL_TIMEOUT_GRACE_SECONDS = 0.3
    runner.TERMINATION_GRACE_SECONDS = 0.1
    child_pid_path = tmp_path / "child.pid"
    known_pids: set[int] = set()
    root_pid: list[int] = []
    original_snapshot = runner.process_snapshot
    child_script = "import time; time.sleep(60)"
    parent_script = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}], start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(0.3)"
    )

    def disappearing_inventory(timeout_seconds: float = 2) -> object:
        inventory = original_snapshot(timeout_seconds)
        if root_pid and root_pid[0] not in inventory.processes and child_pid_path.exists():
            return runner.ProcessInventory({}, False)
        return inventory

    def record_root(pid: int, _process_group_id: int) -> None:
        root_pid.append(pid)

    runner.process_snapshot = disappearing_inventory
    try:
        result = runner.run_process(
            [sys.executable, "-c", parent_script, str(child_pid_path)],
            tmp_path,
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
            5,
            on_start=record_root,
        )
        child_pid = int(child_pid_path.read_text())
        known_pids.add(child_pid)
        assert child_pid in result.observed_descendant_pids
        assert result.process_group_state_after_harvest == "absent"
        assert result.descendant_state_after_harvest == "unknown"
        classification, _envelope = runner.classify_result(
            result,
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
        )
        assert classification == "harvest_failed"
        assert _pid_is_running(child_pid)
    finally:
        _kill_known_pids(known_pids)


def test_run_process_harvests_tracked_child_across_group_probe_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    child_pid_path = tmp_path / "child.pid"
    ready_path = tmp_path / "child.ready"
    release_path = tmp_path / "child.release"
    detached_path = tmp_path / "child.detached"
    known_pids: set[int] = set()
    child_script = (
        "import os, pathlib, signal, sys, time; "
        "ready, release, detached = map(pathlib.Path, sys.argv[1:]); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "ready.write_text('ready'); "
        "\nwhile not release.exists(): time.sleep(0.01); "
        "\nos.setsid(); detached.write_text('detached'); time.sleep(60)"
    )
    parent_script = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}, *sys.argv[2:]]); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "ready = pathlib.Path(sys.argv[2]); "
        "\nwhile not ready.exists(): time.sleep(0.01); "
        "\ntime.sleep(0.3)"
    )
    original_killpg = runner.os.killpg

    def release_child_before_group_probe(process_group_id: int, signum: int) -> None:
        if signum == 0 and child_pid_path.exists() and not release_path.exists():
            release_path.write_text("release")
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not detached_path.exists():
                time.sleep(0.01)
            assert detached_path.exists(), "child did not change process groups"
        original_killpg(process_group_id, signum)

    monkeypatch.setattr(runner.os, "killpg", release_child_before_group_probe)

    try:
        result = runner.run_process(
            [
                sys.executable,
                "-c",
                parent_script,
                str(child_pid_path),
                str(ready_path),
                str(release_path),
                str(detached_path),
            ],
            tmp_path,
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
            5,
        )
        child_pid = int(child_pid_path.read_text())
        known_pids.add(child_pid)
        assert child_pid in result.observed_descendant_pids
        assert result.descendant_state_after_harvest == "absent"
        assert not _pid_is_running(child_pid)
    finally:
        _kill_known_pids(known_pids)


def test_slow_process_inventory_cannot_exceed_the_wall_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    slow_ps = tmp_path / "slow-ps.py"
    slow_ps.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
    slow_ps.chmod(0o755)
    monkeypatch.setattr(runner.shutil, "which", lambda _name: str(slow_ps))
    runner.WALL_TIMEOUT_GRACE_SECONDS = 0.5
    runner.TERMINATION_GRACE_SECONDS = 0.2

    started = time.monotonic()
    result = runner.run_process(
        ["/bin/sleep", "60"],
        tmp_path,
        tmp_path / "stdout.log",
        tmp_path / "stderr.log",
        0.1,
    )

    assert time.monotonic() - started < 0.9
    assert result.timed_out is True
    assert result.process_group_state_after_harvest in {"absent", "unknown"}


def test_runner_records_sigint_during_capability_probe_as_interrupted(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("bounded task\n")
    fake_agy = _make_fake_agy(tmp_path)
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    run_dir = tmp_path / "evidence" / "run-1"
    env = os.environ | {
        "FAKE_AGY_PROBE_DELAY": "60",
        "FAKE_AGY_PROMPT": str(tmp_path / "observed-prompt.txt"),
    }
    command = [
        sys.executable,
        str(RUNNER),
        "--run-dir",
        str(run_dir),
        "--cwd",
        str(cwd),
        "--prompt-file",
        str(prompt),
        "--agy",
        str(fake_agy),
        "--timeout-seconds",
        "30",
        "--mode",
        "plan",
        "--model",
        "gemini-test-medium",
    ]
    process = subprocess.Popen(command, env=env, start_new_session=True)
    known_pids: set[int] = set()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            known_pids.update(_descendant_pids(process.pid))
            if known_pids:
                break
            time.sleep(0.05)
        assert known_pids, "capability probe did not launch"
        os.kill(process.pid, signal.SIGINT)
        assert process.wait(timeout=5) == 128 + signal.SIGINT
        status = _status(run_dir)
        assert status["classification"] == "interrupted"
        assert status["interrupted_signal"] == signal.SIGINT
        assert all(not _pid_is_running(pid) for pid in known_pids)
    finally:
        _kill_test_process_tree(process, known_pids)


def test_signal_handlers_keep_repeated_termination_ignored_after_interrupt() -> None:
    runner = _load_runner_module()
    handled = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in handled}
    try:
        with runner.termination_signal_handlers():
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            with pytest.raises(runner.SupervisorInterrupted):
                handler(signal.SIGTERM, None)

        assert all(signal.getsignal(signum) == signal.SIG_IGN for signum in handled)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def test_runner_harvests_workers_and_records_interruption_on_sigterm(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("bounded task\n")
    fake_agy = _make_fake_agy(tmp_path)
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    run_dir = tmp_path / "evidence" / "run-1"
    env = os.environ | {
        "FAKE_AGY_BEHAVIOR": "timeout",
        "FAKE_AGY_CHILD_PID": str(child_pid_path),
        "FAKE_AGY_PROMPT": str(tmp_path / "observed-prompt.txt"),
    }
    command = [
        sys.executable,
        str(RUNNER),
        "--run-dir",
        str(run_dir),
        "--cwd",
        str(cwd),
        "--prompt-file",
        str(prompt),
        "--agy",
        str(fake_agy),
        "--timeout-seconds",
        "30",
        "--mode",
        "accept-edits",
        "--model",
        "gemini-test-medium",
    ]
    process = subprocess.Popen(command, env=env, start_new_session=True)
    known_pids: set[int] = set()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.05)
        assert child_pid_path.exists(), "fake AGY did not launch its child"
        known_pids.add(int(child_pid_path.read_text()))
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=5) == 128 + signal.SIGTERM
        status = _status(run_dir)
        assert status["classification"] == "interrupted"
        assert status["interrupted_signal"] == signal.SIGTERM
        for pid in known_pids:
            assert not _pid_is_running(pid)
    finally:
        _kill_test_process_tree(process, known_pids)


def test_runner_ignores_repeated_sigterm_until_worker_cleanup_finishes(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("bounded task\n")
    fake_agy = _make_fake_agy(tmp_path)
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    run_dir = tmp_path / "evidence" / "run-1"
    env = os.environ | {
        "FAKE_AGY_BEHAVIOR": "timeout",
        "FAKE_AGY_CHILD_PID": str(child_pid_path),
        "FAKE_AGY_PROMPT": str(tmp_path / "observed-prompt.txt"),
    }
    command = [
        sys.executable,
        str(RUNNER),
        "--run-dir",
        str(run_dir),
        "--cwd",
        str(cwd),
        "--prompt-file",
        str(prompt),
        "--agy",
        str(fake_agy),
        "--timeout-seconds",
        "30",
        "--mode",
        "accept-edits",
        "--model",
        "gemini-test-medium",
    ]
    process = subprocess.Popen(command, env=env, start_new_session=True)
    known_pids: set[int] = set()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.05)
        assert child_pid_path.exists(), "fake AGY did not launch its child"
        known_pids.add(int(child_pid_path.read_text()))
        os.kill(process.pid, signal.SIGTERM)
        time.sleep(0.01)
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=5) == 128 + signal.SIGTERM
        status = _status(run_dir)
        assert status["classification"] == "interrupted"
        assert all(not _pid_is_running(pid) for pid in known_pids)
    finally:
        _kill_test_process_tree(process, known_pids)


def test_runner_uses_one_overall_deadline_for_capability_probes(tmp_path: Path) -> None:
    started = time.monotonic()
    result, run_dir, _, _ = _run_runner(
        tmp_path,
        timeout_seconds=1,
        extra_env={"FAKE_AGY_PROBE_DELAY": "5"},
    )

    assert result.returncode == 124
    assert time.monotonic() - started < 3
    assert _status(run_dir)["classification"] == "timed_out"


def test_runner_records_launch_errors_as_terminal_evidence(tmp_path: Path) -> None:
    fake_agy = _make_fake_agy(tmp_path)
    fake_agy.write_text("#!/definitely/missing/interpreter\n")
    fake_agy.chmod(0o755)

    result, run_dir, _, _ = _run_runner(tmp_path, agy_path=fake_agy)

    assert result.returncode != 0
    status = _status(run_dir)
    assert status["state"] == "finished"
    assert status["classification"] == "capability_probe_failed"
    assert status["launch_error"]


def test_runner_rejects_evidence_directory_inside_worker_cwd(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("bounded task\n")
    fake_agy = _make_fake_agy(tmp_path)
    cwd = tmp_path / "worktree"
    cwd.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--run-dir",
            str(cwd / "evidence"),
            "--cwd",
            str(cwd),
            "--prompt-file",
            str(prompt),
            "--agy",
            str(fake_agy),
            "--timeout-seconds",
            "2",
            "--mode",
            "plan",
            "--model",
            "gemini-test-medium",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert "run directory must be outside cwd" in result.stderr
    assert not (cwd / "evidence").exists()


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
        "input.ndjson",
        "outside the worker cwd",
        "observed descendant",
        "interrupted",
        "double-forks",
        "fallback",
        "one writer",
        "independently inspect",
    )

    missing = [item for item in required_guidance if item not in skill]
    assert not missing, f"missing AGY headless guidance: {missing}"
