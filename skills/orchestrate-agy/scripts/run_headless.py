#!/usr/bin/env python3
"""Run one bounded AGY headless turn and retain verifiable evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

ACK_ENV = "ORCHESTRATE_AGY_PERMISSION_BYPASS_ACK"
ACK_VALUE = "authorized"
PROBE_TIMEOUT_SECONDS = 30
WALL_TIMEOUT_GRACE_SECONDS = 5
TERMINATION_GRACE_SECONDS = 2
REQUIRED_FLAGS = (
    "--input-format",
    "--log-file",
    "--mode",
    "--model",
    "--output-format",
    "--print-timeout",
    "--sandbox",
)
POLL_INTERVAL_SECONDS = 0.05


class SupervisorInterrupted(Exception):
    """Raised by the wrapper's SIGINT/SIGTERM handlers."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"received signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class ProcessInfo:
    parent_pid: int
    process_group_id: int
    state: str
    started_at: str


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    pid: int | None
    process_group_id: int | None
    timed_out: bool
    survivors_harvested: bool
    process_group_alive_after_harvest: bool
    process_group_state_after_harvest: str
    observed_descendant_pids: list[int]
    descendants_alive_after_harvest: list[int]
    interrupted_signal: int | None
    launch_error: str | None


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def open_private(path: Path, mode: str):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, mode)


def write_private(path: Path, content: str) -> None:
    with open_private(path, "w") as output:
        output.write(content)


def write_status(path: Path, status: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(status, indent=2, sort_keys=True) + "\n"
    try:
        with open_private(temporary, "w") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def process_group_state(process_group_id: int, snapshot: dict[int, ProcessInfo]) -> str:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        group_members = [info for info in snapshot.values() if info.process_group_id == process_group_id]
        if group_members and all(info.state.startswith("Z") for info in group_members):
            return "absent"
        return "unknown"
    return "alive"


def signal_process_group(process_group_id: int, signum: signal.Signals) -> bool:
    try:
        os.killpg(process_group_id, signum)
    except (PermissionError, ProcessLookupError):
        return False
    return True


def signal_process(pid: int, signum: signal.Signals) -> bool:
    try:
        os.kill(pid, signum)
    except (PermissionError, ProcessLookupError):
        return False
    return True


def process_snapshot(timeout_seconds: float = 2) -> dict[int, ProcessInfo]:
    ps = shutil.which("ps")
    if not ps or timeout_seconds <= 0:
        return {}
    try:
        completed = subprocess.run(
            [ps, "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=min(2, timeout_seconds),
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    snapshot: dict[int, ProcessInfo] = {}
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.split(maxsplit=4)
        if len(fields) != 5:
            continue
        try:
            pid, parent_pid, process_group_id = (int(value) for value in fields[:3])
        except ValueError:
            continue
        snapshot[pid] = ProcessInfo(parent_pid, process_group_id, fields[3], fields[4])
    return snapshot


def snapshot_before(deadline: float) -> dict[int, ProcessInfo] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return process_snapshot(remaining)


def descendant_pids(root_pid: int, snapshot: dict[int, ProcessInfo]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, info in snapshot.items():
        children.setdefault(info.parent_pid, []).append(pid)
    descendants: set[int] = set()
    frontier = [root_pid]
    while frontier:
        for child_pid in children.get(frontier.pop(), []):
            if child_pid not in descendants:
                descendants.add(child_pid)
                frontier.append(child_pid)
    return descendants


def remember_process_tree(
    root_pid: int,
    snapshot: dict[int, ProcessInfo],
    tracked_processes: dict[int, str],
) -> None:
    root_info = snapshot.get(root_pid)
    if not root_info:
        return
    known_root_start = tracked_processes.get(root_pid)
    if known_root_start and known_root_start != root_info.started_at:
        return
    tracked_processes[root_pid] = root_info.started_at
    for pid in descendant_pids(root_pid, snapshot):
        tracked_processes[pid] = snapshot[pid].started_at


def live_pids(tracked_processes: dict[int, str], snapshot: dict[int, ProcessInfo]) -> set[int]:
    return {
        pid
        for pid, started_at in tracked_processes.items()
        if pid in snapshot and snapshot[pid].started_at == started_at and not snapshot[pid].state.startswith("Z")
    }


def group_is_alive(
    process_group_id: int,
    snapshot: dict[int, ProcessInfo],
    tracked_processes: dict[int, str],
) -> bool:
    return any(
        pid in snapshot
        and snapshot[pid].started_at == started_at
        and snapshot[pid].process_group_id == process_group_id
        and not snapshot[pid].state.startswith("Z")
        for pid, started_at in tracked_processes.items()
    )


def wait_for_harvest(
    root_pid: int,
    process_group_id: int,
    tracked_processes: dict[int, str],
    deadline: float,
    snapshot: dict[int, ProcessInfo],
) -> tuple[str, list[int], dict[int, ProcessInfo]]:
    while True:
        alive = sorted(live_pids(tracked_processes, snapshot))
        group_state = process_group_state(process_group_id, snapshot)
        if not alive and group_state == "absent":
            return group_state, alive, snapshot
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return group_state, alive, snapshot
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        next_snapshot = snapshot_before(deadline)
        if next_snapshot is None:
            return process_group_state(process_group_id, snapshot), alive, snapshot
        snapshot = next_snapshot
        remember_process_tree(root_pid, snapshot, tracked_processes)


def harvest_process_tree(
    root_pid: int,
    process_group_id: int,
    tracked_processes: dict[int, str],
    cleanup_deadline: float,
) -> tuple[bool, str, list[int]]:
    group_signaled = signal_process_group(process_group_id, signal.SIGTERM)
    term_deadline = min(time.monotonic() + TERMINATION_GRACE_SECONDS, cleanup_deadline)
    snapshot = snapshot_before(term_deadline) or {}
    remember_process_tree(root_pid, snapshot, tracked_processes)
    live_before = live_pids(tracked_processes, snapshot)
    harvested = bool(live_before or group_signaled)

    for pid in live_before:
        signal_process(pid, signal.SIGTERM)

    group_state, alive, snapshot = wait_for_harvest(
        root_pid,
        process_group_id,
        tracked_processes,
        term_deadline,
        snapshot,
    )
    if group_state == "absent" and not alive:
        return harvested, group_state, alive

    if group_state != "absent":
        signal_process_group(process_group_id, signal.SIGKILL)
    for pid in alive:
        signal_process(pid, signal.SIGKILL)

    group_state, alive, _snapshot = wait_for_harvest(
        root_pid,
        process_group_id,
        tracked_processes,
        cleanup_deadline,
        snapshot,
    )
    return harvested, group_state, alive


@contextmanager
def termination_signal_handlers() -> Iterator[None]:
    handled = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in handled}

    def interrupt(signum: int, _frame: object) -> None:
        for handled_signum in handled:
            signal.signal(handled_signum, signal.SIG_IGN)
        raise SupervisorInterrupted(signum)

    for signum in handled:
        signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def launch_process(
    command: list[str],
    cwd: Path,
    stdin: BinaryIO,
    stdout: BinaryIO,
    stderr: BinaryIO,
) -> tuple[subprocess.Popen[bytes] | None, str | None]:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    except OSError as error:
        return None, f"{type(error).__name__}: {error}"
    return process, None


def monitor_process(
    process: subprocess.Popen[bytes],
    process_deadline: float,
    tracked_processes: dict[int, str],
) -> tuple[int, bool, int | None]:
    try:
        while True:
            returncode = process.poll()
            if returncode is not None:
                return returncode, False, None
            snapshot = snapshot_before(process_deadline)
            if snapshot is None:
                return 124, True, None
            remember_process_tree(process.pid, snapshot, tracked_processes)
            remaining = process_deadline - time.monotonic()
            if remaining <= 0:
                return 124, True, None
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    except SupervisorInterrupted as error:
        return 128 + error.signum, False, error.signum


def launch_error_result(launch_error: str) -> ProcessResult:
    return ProcessResult(
        returncode=127,
        pid=None,
        process_group_id=None,
        timed_out=False,
        survivors_harvested=False,
        process_group_alive_after_harvest=False,
        process_group_state_after_harvest="absent",
        observed_descendant_pids=[],
        descendants_alive_after_harvest=[],
        interrupted_signal=None,
        launch_error=launch_error,
    )


def run_process(
    command: list[str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    stdin_path: Path | None = None,
    on_start: Callable[[int, int], None] | None = None,
) -> ProcessResult:
    tracked_processes: dict[int, str] = {}
    process_deadline = time.monotonic() + timeout_seconds
    overall_wall_deadline = process_deadline + WALL_TIMEOUT_GRACE_SECONDS

    with open_private(stdout_path, "wb") as stdout, open_private(stderr_path, "wb") as stderr:
        stdin = stdin_path.open("rb") if stdin_path else open(os.devnull, "rb")
        try:
            process, launch_error = launch_process(command, cwd, stdin, stdout, stderr)
            if not process:
                return launch_error_result(launch_error or "unknown launch error")
            try:
                if on_start:
                    on_start(process.pid, process.pid)
                returncode, timed_out, interrupted_signal = monitor_process(
                    process,
                    process_deadline,
                    tracked_processes,
                )
            finally:
                cleanup_deadline = min(
                    overall_wall_deadline,
                    time.monotonic() + WALL_TIMEOUT_GRACE_SECONDS,
                )
                survivors_harvested, group_state, alive_descendants = harvest_process_tree(
                    process.pid,
                    process.pid,
                    tracked_processes,
                    cleanup_deadline,
                )
                if process.poll() is None:
                    signal_process(process.pid, signal.SIGKILL)
                remaining = cleanup_deadline - time.monotonic()
                if remaining > 0:
                    try:
                        returncode = process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        returncode = process.poll() or returncode
        finally:
            stdin.close()
    return ProcessResult(
        returncode=returncode,
        pid=process.pid,
        process_group_id=process.pid,
        timed_out=timed_out,
        survivors_harvested=survivors_harvested,
        process_group_alive_after_harvest=group_state != "absent",
        process_group_state_after_harvest=group_state,
        observed_descendant_pids=sorted(pid for pid in tracked_processes if pid != process.pid),
        descendants_alive_after_harvest=alive_descendants,
        interrupted_signal=interrupted_signal,
        launch_error=None,
    )


def advertised_models(models_output: str) -> set[str]:
    models: set[str] = set()
    for raw_line in models_output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("fetching "):
            continue
        models.add(line.split(maxsplit=1)[0])
    return models


def terminal_stream_result(stdout_text: str) -> tuple[str | None, dict[str, Any] | None]:
    if not stdout_text.strip():
        return "no_output", None
    result_events: list[dict[str, Any]] = []
    try:
        events = [json.loads(line) for line in stdout_text.splitlines()]
    except json.JSONDecodeError:
        return "invalid_output", None
    if any(not isinstance(event, dict) for event in events):
        return "invalid_output", None
    for event in events:
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            result_events.append(event["result"])
    if len(result_events) != 1:
        return "invalid_output", None
    return None, result_events[0]


def empty_response_classification(stderr_path: Path) -> str:
    stderr_text = stderr_path.read_text(errors="replace").lower()
    permission_notice = "permission" in stderr_text and (
        "cannot prompt" in stderr_text or "auto-denied" in stderr_text or "requires approval" in stderr_text
    )
    return "permission_blocked" if permission_notice else "no_output"


def classify_result(
    result: ProcessResult,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[str, dict[str, Any] | None]:
    if result.process_group_state_after_harvest != "absent" or result.descendants_alive_after_harvest:
        return "harvest_failed", None
    if result.interrupted_signal:
        return "interrupted", None
    if result.timed_out:
        return "timed_out", None
    if result.launch_error:
        return "launch_error", None
    if result.returncode != 0:
        return "cli_error", None

    stdout_text = stdout_path.read_text(errors="replace")
    stream_error, envelope = terminal_stream_result(stdout_text)
    if stream_error or not envelope:
        return stream_error or "invalid_output", None

    agy_status = envelope.get("status")
    if agy_status != "SUCCESS":
        return f"agy_status_{agy_status or 'MISSING'}", envelope

    response = envelope.get("response")
    if not isinstance(response, str) or not response.strip():
        return empty_response_classification(stderr_path), envelope
    return "succeeded", envelope


def absolute_path(parser: argparse.ArgumentParser, raw_path: str, label: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        parser.error(f"{label} must be absolute: {raw_path}")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--agy", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    parser.add_argument("--mode", required=True, choices=("plan", "accept-edits"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", choices=("low", "medium", "high"))
    parser.add_argument("--allow-all-permissions", action="store_true")
    args = parser.parse_args()

    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.allow_all_permissions and os.environ.get(ACK_ENV) != ACK_VALUE:
        parser.error(f"--allow-all-permissions requires {ACK_ENV}={ACK_VALUE}")

    args.run_dir = absolute_path(parser, args.run_dir, "run directory")
    args.cwd = absolute_path(parser, args.cwd, "cwd")
    args.prompt_file = absolute_path(parser, args.prompt_file, "prompt path")
    args.agy = absolute_path(parser, args.agy, "AGY path")
    if not args.cwd.is_dir():
        parser.error(f"cwd is not a directory: {args.cwd}")
    if not args.prompt_file.is_file():
        parser.error(f"prompt is not a file: {args.prompt_file}")
    if not args.agy.is_file() or not os.access(args.agy, os.X_OK):
        parser.error(f"AGY is not executable: {args.agy}")
    args.cwd = args.cwd.resolve()
    args.prompt_file = args.prompt_file.resolve()
    args.agy = args.agy.resolve()
    args.run_dir = args.run_dir.resolve(strict=False)
    if args.run_dir.is_relative_to(args.cwd):
        parser.error(f"run directory must be outside cwd: {args.run_dir}")
    if args.run_dir.exists():
        parser.error(f"run directory already exists: {args.run_dir}")
    return args


def probe(
    args: argparse.Namespace,
    run_dir: Path,
    name: str,
    command: list[str],
    timeout_seconds: float,
) -> ProcessResult:
    return run_process(
        command,
        args.cwd,
        run_dir / f"agy-{name}.stdout",
        run_dir / f"agy-{name}.stderr",
        timeout_seconds,
    )


def initial_status(args: argparse.Namespace, started_at: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "state": "probing",
        "classification": "running",
        "started_at": started_at,
        "cwd": str(args.cwd),
        "mode": args.mode,
        "model": args.model,
        "effort": args.effort,
        "timeout_seconds": args.timeout_seconds,
        "wall_timeout_seconds": args.timeout_seconds + WALL_TIMEOUT_GRACE_SECONDS,
        "permission_bypass_requested": args.allow_all_permissions,
    }


def run_capability_probes(
    args: argparse.Namespace,
    status: dict[str, object],
    started_monotonic: float,
    operation_deadline: float,
) -> int | None:
    probes = (
        ("version", [str(args.agy), "--version"]),
        ("help", [str(args.agy), "--help"]),
        ("models", [str(args.agy), "models"]),
    )
    for name, command in probes:
        remaining = operation_deadline - time.monotonic()
        if remaining <= 0:
            status.update(
                state="finished",
                classification="timed_out",
                failed_stage="capability_probes",
                completed_at=utc_now(),
                duration_seconds=round(time.monotonic() - started_monotonic, 3),
            )
            write_status(args.run_dir / "status.json", status)
            print(f"AGY run timed out during capability probes; evidence: {args.run_dir}", file=sys.stderr)
            return 124
        result = probe(args, args.run_dir, name, command, min(PROBE_TIMEOUT_SECONDS, remaining))
        failed = (
            result.timed_out
            or result.launch_error
            or result.returncode != 0
            or result.process_group_alive_after_harvest
            or result.descendants_alive_after_harvest
            or result.interrupted_signal
        )
        if failed:
            overall_timeout = result.timed_out and time.monotonic() >= operation_deadline
            if result.interrupted_signal:
                classification = "interrupted"
            else:
                classification = "timed_out" if overall_timeout else "capability_probe_failed"
            status.update(
                state="finished",
                classification=classification,
                failed_probe=name,
                completed_at=utc_now(),
                duration_seconds=round(time.monotonic() - started_monotonic, 3),
                **asdict(result),
            )
            write_status(args.run_dir / "status.json", status)
            print(f"AGY capability probe failed: {name}; evidence: {args.run_dir}", file=sys.stderr)
            if result.interrupted_signal:
                return 128 + result.interrupted_signal
            return 124 if classification == "timed_out" else 1
    return None


def prepare_command(
    args: argparse.Namespace,
    status: dict[str, object],
    prompt: str,
    started_monotonic: float,
    operation_deadline: float,
) -> tuple[list[str] | None, int]:
    probe_exit = run_capability_probes(args, status, started_monotonic, operation_deadline)
    if probe_exit is not None:
        return None, probe_exit

    help_text = "\n".join(
        (
            (args.run_dir / "agy-help.stdout").read_text(errors="replace"),
            (args.run_dir / "agy-help.stderr").read_text(errors="replace"),
        )
    )
    required_flags = list(REQUIRED_FLAGS)
    if args.effort:
        required_flags.append("--effort")
    if args.allow_all_permissions:
        required_flags.append("--dangerously-skip-permissions")
    missing_flags = [flag for flag in required_flags if flag not in help_text]
    models = advertised_models((args.run_dir / "agy-models.stdout").read_text(errors="replace"))
    if missing_flags or args.model not in models:
        status.update(
            state="finished",
            classification="capability_mismatch",
            missing_flags=missing_flags,
            model_advertised=args.model in models,
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
        )
        write_status(args.run_dir / "status.json", status)
        print(f"AGY capability mismatch; evidence: {args.run_dir}", file=sys.stderr)
        return None, 2

    remaining = operation_deadline - time.monotonic()
    if remaining <= 0:
        status.update(
            state="finished",
            classification="timed_out",
            failed_stage="capability_probes",
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
        )
        write_status(args.run_dir / "status.json", status)
        return None, 124

    agent_timeout_seconds = max(1, math.ceil(remaining))
    status["agent_timeout_seconds"] = agent_timeout_seconds
    log_path = args.run_dir / "agy.log"
    write_private(log_path, "")
    command = [
        str(args.agy),
        "--mode",
        args.mode,
        "--sandbox",
        "--model",
        args.model,
    ]
    if args.effort:
        command.extend(("--effort", args.effort))
    if args.allow_all_permissions:
        command.append("--dangerously-skip-permissions")
    command.extend(
        (
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--print-timeout",
            f"{agent_timeout_seconds}s",
            "--log-file",
            str(log_path),
        )
    )
    input_event = {"event": "user", "message": {"content": prompt}}
    write_private(args.run_dir / "input.ndjson", json.dumps(input_event, separators=(",", ":")) + "\n")
    write_private(
        args.run_dir / "command.json",
        json.dumps(command, indent=2) + "\n",
    )
    return command, 0


def execute_dispatch(
    args: argparse.Namespace,
    status: dict[str, object],
    command: list[str],
    started_monotonic: float,
    operation_deadline: float,
) -> int:
    def record_process(pid: int, process_group_id: int) -> None:
        status.update(state="running", pid=pid, process_group_id=process_group_id)
        write_status(args.run_dir / "status.json", status)

    remaining = operation_deadline - time.monotonic()
    if remaining <= 0:
        status.update(
            state="finished",
            classification="timed_out",
            failed_stage="before_dispatch",
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
        )
        write_status(args.run_dir / "status.json", status)
        return 124

    result = run_process(
        command,
        args.cwd,
        args.run_dir / "stdout.json",
        args.run_dir / "stderr.log",
        remaining,
        stdin_path=args.run_dir / "input.ndjson",
        on_start=record_process,
    )
    classification, envelope = classify_result(
        result,
        args.run_dir / "stdout.json",
        args.run_dir / "stderr.log",
    )
    status.update(
        state="finished",
        classification=classification,
        completed_at=utc_now(),
        duration_seconds=round(time.monotonic() - started_monotonic, 3),
        **asdict(result),
    )
    if envelope:
        status["agy_status"] = envelope.get("status")
        status["conversation_id"] = envelope.get("conversation_id")
        status["agy_error"] = envelope.get("error")
    write_status(args.run_dir / "status.json", status)

    if classification == "succeeded":
        print(f"classification=succeeded\nrun_dir={args.run_dir}")
        return 0
    print(f"AGY run failed: {classification}; evidence: {args.run_dir}", file=sys.stderr)
    if classification == "timed_out":
        return 124
    if classification == "interrupted" and result.interrupted_signal:
        return 128 + result.interrupted_signal
    if classification == "cli_error" and 0 < result.returncode < 126:
        return result.returncode
    return 1


def main() -> int:
    args = parse_args()
    started_monotonic = time.monotonic()
    started_at = utc_now()
    args.run_dir.mkdir(mode=0o700, parents=True)
    args.run_dir.chmod(0o700)

    status = initial_status(args, started_at)
    write_status(args.run_dir / "status.json", status)
    operation_deadline = started_monotonic + args.timeout_seconds

    try:
        with termination_signal_handlers():
            prompt = args.prompt_file.read_text()
            write_private(args.run_dir / "prompt.txt", prompt)
            if not prompt.strip():
                status.update(
                    state="finished",
                    classification="invalid_prompt",
                    completed_at=utc_now(),
                    duration_seconds=round(time.monotonic() - started_monotonic, 3),
                )
                write_status(args.run_dir / "status.json", status)
                print(f"AGY run failed: invalid_prompt; evidence: {args.run_dir}", file=sys.stderr)
                return 2

            command, preparation_exit = prepare_command(
                args,
                status,
                prompt,
                started_monotonic,
                operation_deadline,
            )
            if command is None:
                return preparation_exit
            return execute_dispatch(args, status, command, started_monotonic, operation_deadline)
    except SupervisorInterrupted as error:
        status.update(
            state="finished",
            classification="interrupted",
            interrupted_signal=error.signum,
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
        )
        write_status(args.run_dir / "status.json", status)
        return 128 + error.signum
    except Exception as error:
        status.update(
            state="finished",
            classification="internal_error",
            error_type=type(error).__name__,
            error_message=str(error),
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
        )
        write_status(args.run_dir / "status.json", status)
        print(f"AGY runner failed internally; evidence: {args.run_dir}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
