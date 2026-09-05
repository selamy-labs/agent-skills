#!/usr/bin/env python3
"""Run one bounded AGY headless turn and retain verifiable evidence."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ACK_ENV = "ORCHESTRATE_AGY_PERMISSION_BYPASS_ACK"
ACK_VALUE = "authorized"
PROBE_TIMEOUT_SECONDS = 30
WALL_TIMEOUT_GRACE_SECONDS = 5
TERMINATION_GRACE_SECONDS = 2
REQUIRED_FLAGS = (
    "--log-file",
    "--mode",
    "--model",
    "--output-format",
    "--print",
    "--print-timeout",
    "--sandbox",
)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    pid: int
    process_group_id: int
    timed_out: bool
    survivors_harvested: bool
    process_group_alive_after_harvest: bool


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def open_private(path: Path, mode: str):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if "b" not in mode:
        flags |= getattr(os, "O_CLOEXEC", 0)
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


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_process_group(process_group_id: int, signum: signal.Signals) -> bool:
    try:
        os.killpg(process_group_id, signum)
    except ProcessLookupError:
        return False
    return True


def wait_for_process_group_exit(process_group_id: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_group_exists(process_group_id):
            return True
        time.sleep(0.05)
    return not process_group_exists(process_group_id)


def harvest_process_group(process_group_id: int) -> bool:
    if not process_group_exists(process_group_id):
        return False
    signal_process_group(process_group_id, signal.SIGTERM)
    if wait_for_process_group_exit(process_group_id, TERMINATION_GRACE_SECONDS):
        return True
    signal_process_group(process_group_id, signal.SIGKILL)
    wait_for_process_group_exit(process_group_id, TERMINATION_GRACE_SECONDS)
    return True


def run_process(
    command: list[str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
    on_start: Callable[[int, int], None] | None = None,
) -> ProcessResult:
    with open_private(stdout_path, "wb") as stdout, open_private(stderr_path, "wb") as stderr:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        process_group_id = process.pid
        if on_start:
            on_start(process.pid, process_group_id)

        timed_out = False
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            signal_process_group(process_group_id, signal.SIGTERM)
            try:
                returncode = process.wait(timeout=TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                signal_process_group(process_group_id, signal.SIGKILL)
                returncode = process.wait(timeout=TERMINATION_GRACE_SECONDS)

    survivors_harvested = harvest_process_group(process_group_id)
    group_alive = not wait_for_process_group_exit(process_group_id, TERMINATION_GRACE_SECONDS)
    return ProcessResult(
        returncode=returncode,
        pid=process.pid,
        process_group_id=process_group_id,
        timed_out=timed_out,
        survivors_harvested=survivors_harvested,
        process_group_alive_after_harvest=group_alive,
    )


def advertised_models(models_output: str) -> set[str]:
    models: set[str] = set()
    for raw_line in models_output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("fetching "):
            continue
        models.add(line.split(maxsplit=1)[0])
    return models


def classify_result(
    result: ProcessResult,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[str, dict[str, Any] | None]:
    if result.timed_out:
        return "timed_out", None
    if result.process_group_alive_after_harvest:
        return "harvest_failed", None
    if result.returncode != 0:
        return "cli_error", None

    stdout_text = stdout_path.read_text(errors="replace")
    if not stdout_text.strip():
        return "no_output", None
    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError:
        return "invalid_output", None
    if not isinstance(envelope, dict):
        return "invalid_output", None

    agy_status = envelope.get("status")
    if agy_status != "SUCCESS":
        return f"agy_status_{agy_status or 'MISSING'}", envelope

    response = envelope.get("response")
    if not isinstance(response, str) or not response.strip():
        stderr_text = stderr_path.read_text(errors="replace").lower()
        permission_notice = "permission" in stderr_text and (
            "cannot prompt" in stderr_text or "auto-denied" in stderr_text or "requires approval" in stderr_text
        )
        return ("permission_blocked" if permission_notice else "no_output"), envelope
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
    if args.run_dir.exists():
        parser.error(f"run directory already exists: {args.run_dir}")
    return args


def probe(
    args: argparse.Namespace,
    run_dir: Path,
    status: dict[str, object],
    name: str,
    command: list[str],
) -> ProcessResult:
    result = run_process(
        command,
        args.cwd,
        run_dir / f"agy-{name}.stdout",
        run_dir / f"agy-{name}.stderr",
        PROBE_TIMEOUT_SECONDS,
    )
    if result.timed_out or result.returncode != 0 or result.process_group_alive_after_harvest:
        status.update(
            state="finished",
            classification="capability_probe_failed",
            failed_probe=name,
            completed_at=utc_now(),
            **asdict(result),
        )
        write_status(run_dir / "status.json", status)
    return result


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


def prepare_command(
    args: argparse.Namespace,
    status: dict[str, object],
    prompt: str,
    started_monotonic: float,
) -> tuple[list[str] | None, int]:
    probes = (
        ("version", [str(args.agy), "--version"]),
        ("help", [str(args.agy), "--help"]),
        ("models", [str(args.agy), "models"]),
    )
    for name, command in probes:
        result = probe(args, args.run_dir, status, name, command)
        if result.timed_out or result.returncode != 0 or result.process_group_alive_after_harvest:
            print(f"AGY capability probe failed: {name}; evidence: {args.run_dir}", file=sys.stderr)
            return None, 1

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
            "--output-format",
            "json",
            "--print-timeout",
            f"{args.timeout_seconds}s",
            "--log-file",
            str(log_path),
            "--print",
            prompt,
        )
    )
    write_private(
        args.run_dir / "command.json",
        json.dumps([*command[:-1], "<prompt from prompt.txt>"], indent=2) + "\n",
    )
    return command, 0


def execute_dispatch(
    args: argparse.Namespace,
    status: dict[str, object],
    command: list[str],
    started_monotonic: float,
) -> int:
    def record_process(pid: int, process_group_id: int) -> None:
        status.update(state="running", pid=pid, process_group_id=process_group_id)
        write_status(args.run_dir / "status.json", status)

    result = run_process(
        command,
        args.cwd,
        args.run_dir / "stdout.json",
        args.run_dir / "stderr.log",
        args.timeout_seconds + WALL_TIMEOUT_GRACE_SECONDS,
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
    if classification == "cli_error" and 0 < result.returncode < 126:
        return result.returncode
    return 1


def main() -> int:
    args = parse_args()
    started_monotonic = time.monotonic()
    started_at = utc_now()
    args.run_dir.mkdir(mode=0o700, parents=True)
    args.run_dir.chmod(0o700)

    prompt = args.prompt_file.read_text()
    write_private(args.run_dir / "prompt.txt", prompt)
    if not prompt.strip():
        write_status(
            args.run_dir / "status.json",
            {
                "schema_version": 1,
                "state": "finished",
                "classification": "invalid_prompt",
                "started_at": started_at,
                "completed_at": utc_now(),
            },
        )
        print(f"AGY run failed: invalid_prompt; evidence: {args.run_dir}", file=sys.stderr)
        return 2

    status = initial_status(args, started_at)
    write_status(args.run_dir / "status.json", status)
    command, preparation_exit = prepare_command(args, status, prompt, started_monotonic)
    if command is None:
        return preparation_exit
    return execute_dispatch(args, status, command, started_monotonic)


if __name__ == "__main__":
    raise SystemExit(main())
