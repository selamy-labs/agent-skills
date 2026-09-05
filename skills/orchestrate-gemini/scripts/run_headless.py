#!/usr/bin/env python3
"""Run one bounded Gemini CLI turn in an isolated, auditable lane."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

GOAL_KEYS = {
    "schema_version",
    "goal_id",
    "objective",
    "base_sha",
    "allowed_paths",
    "verification_commands",
    "stop_conditions",
    "max_attempts",
    "auth_type",
    "allow_paid_generation",
}
AUTH_TYPES = {
    "oauth-personal",
    "gemini-api-key",
    "vertex-ai",
    "compute-default-credentials",
    "gateway",
}
GOAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
REQUIRED_FLAGS = (
    "--model",
    "--output-format",
    "--approval-mode",
    "--sandbox",
    "--admin-policy",
    "--extensions",
    "--resume",
)
SANDBOX_FLAGS = "--read-only --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=256"
PAID_ACK_ENV = "ORCHESTRATE_GEMINI_PAID_GENERATION_ACK"
PAID_ACK_VALUE = "authorized"
MODEL_ALIASES = {"auto", "pro", "flash", "flash-lite"}
POLL_INTERVAL_SECONDS = 0.05
TERMINATION_GRACE_SECONDS = 2
CLEANUP_GRACE_SECONDS = 5
STANDARD_ADMIN_POLICY_DIRS = (
    Path("/etc/gemini-cli/policies"),
    Path("/Library/Application Support/GeminiCli/policies"),
)


class PreflightError(Exception):
    """A classified failure safe to expose in terminal evidence."""

    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.classification = classification


class SupervisorInterrupted(Exception):
    """Raised when the wrapper receives SIGINT or SIGTERM."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"received signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class VerificationCommand:
    argv: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class Goal:
    schema_version: int
    goal_id: str
    objective: str
    base_sha: str
    allowed_paths: tuple[str, ...]
    verification_commands: tuple[VerificationCommand, ...]
    stop_conditions: tuple[str, ...]
    max_attempts: int
    auth_type: str
    allow_paid_generation: bool


@dataclass(frozen=True)
class GitState:
    head: str
    common_dir: str
    status: str


@dataclass(frozen=True)
class ProcessInfo:
    parent_pid: int
    process_group_id: int
    state: str
    started_at: str


@dataclass(frozen=True)
class ProcessIdentity:
    started_at: str


@dataclass(frozen=True)
class ProcessInventory:
    processes: dict[int, ProcessInfo]
    complete: bool


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    pid: int | None
    process_group_id: int | None
    timed_out: bool
    survivors_harvested: bool
    process_group_state_after_harvest: str
    descendant_state_after_harvest: str
    observed_descendant_pids: list[int]
    descendants_alive_after_harvest: list[int]
    interrupted_signal: int | None
    launch_error: str | None


@dataclass
class PreparedAttempt:
    goal: Goal
    attempt: int
    goal_digest: str
    git_state: GitState
    environment: dict[str, str]
    durable_policy: Path
    lease_payload: dict[str, Any]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def normalized_allowed_path(raw: object) -> str:
    if not is_nonempty_string(raw):
        raise ValueError("allowed paths must be non-empty strings")
    assert isinstance(raw, str)
    if "\\" in raw or raw.startswith("/"):
        raise ValueError("allowed paths must be repository-relative POSIX paths")
    trimmed = raw.rstrip("/")
    if not trimmed or trimmed.startswith("./") or "//" in trimmed:
        raise ValueError("allowed paths must be normalized")
    path = PurePosixPath(trimmed)
    if any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != trimmed:
        raise ValueError("allowed paths must not escape or normalize differently")
    return trimmed


def parse_verification_commands(raw: object) -> tuple[VerificationCommand, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("verification_commands must be a non-empty list")
    commands: list[VerificationCommand] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"argv", "timeout_seconds"}:
            raise ValueError("verification commands require only argv and timeout_seconds")
        argv = item["argv"]
        timeout = item["timeout_seconds"]
        if not isinstance(argv, list) or not argv or not all(is_nonempty_string(arg) for arg in argv):
            raise ValueError("verification argv must be a non-empty string list")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("verification timeout_seconds must be a positive integer")
        commands.append(VerificationCommand(tuple(argv), timeout))
    return tuple(commands)


def parse_string_list(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or not all(is_nonempty_string(item) for item in raw):
        raise ValueError(f"{label} must be a non-empty string list")
    return tuple(item.strip() for item in raw if isinstance(item, str))


def validate_goal_identity(raw: dict[str, object]) -> None:
    if set(raw) != GOAL_KEYS:
        raise ValueError("goal has missing or unknown fields")
    if raw["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    if not is_nonempty_string(raw["goal_id"]) or not GOAL_ID_PATTERN.fullmatch(raw["goal_id"]):
        raise ValueError("goal_id is invalid")
    if not is_nonempty_string(raw["objective"]):
        raise ValueError("objective is required")
    if not isinstance(raw["base_sha"], str) or not SHA_PATTERN.fullmatch(raw["base_sha"]):
        raise ValueError("base_sha must be a lowercase 40-character object ID")


def parse_goal(path: Path) -> Goal:
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PreflightError("invalid_goal", f"cannot read goal JSON: {error}") from error
    try:
        if not isinstance(raw, dict):
            raise ValueError("goal must be a JSON object")
        validate_goal_identity(raw)
        max_attempts = raw["max_attempts"]
        allow_paid = raw["allow_paid_generation"]
        allowed_paths = tuple(normalized_allowed_path(item) for item in raw["allowed_paths"])
        if not allowed_paths or len(set(allowed_paths)) != len(allowed_paths):
            raise ValueError("allowed_paths must be non-empty and unique")
        commands = parse_verification_commands(raw["verification_commands"])
        stop_conditions = parse_string_list(raw["stop_conditions"], "stop_conditions")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
            raise ValueError("max_attempts must be a positive integer")
        if raw["auth_type"] not in AUTH_TYPES:
            raise ValueError("auth_type is unsupported")
        if not isinstance(allow_paid, bool):
            raise ValueError("allow_paid_generation must be boolean")
    except (KeyError, TypeError, ValueError) as error:
        raise PreflightError("invalid_goal", str(error)) from error
    return Goal(
        schema_version=1,
        goal_id=raw["goal_id"],
        objective=raw["objective"].strip(),
        base_sha=raw["base_sha"],
        allowed_paths=allowed_paths,
        verification_commands=commands,
        stop_conditions=stop_conditions,
        max_attempts=max_attempts,
        auth_type=raw["auth_type"],
        allow_paid_generation=allow_paid,
    )


def goal_payload(goal: Goal) -> bytes:
    payload = asdict(goal)
    payload["allowed_paths"] = list(goal.allowed_paths)
    payload["verification_commands"] = [
        {"argv": list(command.argv), "timeout_seconds": command.timeout_seconds}
        for command in goal.verification_commands
    ]
    payload["stop_conditions"] = list(goal.stop_conditions)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def open_private(path: Path, mode: str = "w"):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    return os.fdopen(descriptor, mode)


def write_private(path: Path, content: str | bytes) -> None:
    mode = "wb" if isinstance(content, bytes) else "w"
    with open_private(path, mode) as output:
        output.write(content)


def write_atomic_private(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write_private(temporary, json.dumps(payload, sort_keys=True, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def persist_exact_private(path: Path, content: bytes, classification: str) -> None:
    if not path.exists():
        write_private(path, content)
        return
    try:
        existing = path.read_bytes()
    except OSError as error:
        raise PreflightError(classification, f"cannot read durable file: {error}") from error
    if existing != content:
        raise PreflightError(classification, f"durable file changed: {path.name}")


def path_is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def absolute_path(parser: argparse.ArgumentParser, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        parser.error(f"path must be absolute: {value}")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    for option in ("run-dir", "state-dir", "cwd", "goal-file", "prompt-file", "policy-file", "gemini"):
        parser.add_argument(f"--{option}", required=True, type=lambda value, p=parser: absolute_path(p, value))
    parser.add_argument("--sandbox-provider", required=True, choices=("docker", "podman", "runsc"))
    parser.add_argument("--timeout-seconds", required=True, type=int)
    parser.add_argument("--model", required=True)
    parser.add_argument("--approval-mode", required=True, choices=("plan", "default", "auto_edit"))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--resume-from", type=lambda value, p=parser: absolute_path(p, value))
    parser.add_argument("--validation-fake-responses", type=lambda value, p=parser: absolute_path(p, value))
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.model.lower() in MODEL_ALIASES or not is_nonempty_string(args.model):
        parser.error("--model must be a concrete, non-alias model ID")
    return args


def prepare_run_directory(run_dir: Path, state_dir: Path, cwd: Path) -> None:
    resolved_cwd = cwd.resolve()
    resolved_state = state_dir.resolve()
    resolved_run = run_dir.resolve(strict=False)
    if path_is_within(resolved_state, resolved_cwd) or path_is_within(resolved_run, resolved_cwd):
        raise PreflightError("invalid_path", "state and run directories must be outside the checkout")
    if not state_dir.is_dir():
        raise PreflightError("invalid_path", "state directory must already exist")
    state_mode = state_dir.stat().st_mode & 0o777
    if state_dir.stat().st_uid != os.getuid() or state_mode & 0o077:
        raise PreflightError("invalid_path", "state directory must be owner-controlled with mode 0700")
    runs_dir = run_dir.parent
    if runs_dir.parent.resolve() != resolved_state:
        raise PreflightError("invalid_path", "run directory must be a direct child of state-dir/runs")
    runs_dir.mkdir(mode=0o700, exist_ok=True)
    if runs_dir.stat().st_mode & 0o077:
        raise PreflightError("invalid_path", "runs directory must be owner-only")
    run_dir.mkdir(mode=0o700)


def finish_status(status_path: Path, status: dict[str, Any], classification: str, message: str | None = None) -> int:
    status.update({"classification": classification, "finished_at": utc_now()})
    if message:
        status["message"] = message
    write_atomic_private(status_path, status)
    return 0 if classification in {"preflight_succeeded", "succeeded", "validation_succeeded"} else 1


def persist_goal(state_dir: Path, goal: Goal) -> str:
    payload = goal_payload(goal)
    digest = hashlib.sha256(payload).hexdigest()
    canonical_path = state_dir / "goal.json"
    digest_path = state_dir / "goal.sha256"
    if not canonical_path.exists() and not digest_path.exists():
        write_private(canonical_path, payload)
        write_private(digest_path, digest + "\n")
        return digest
    if not canonical_path.is_file() or not digest_path.is_file():
        raise PreflightError("goal_mismatch", "durable goal state is incomplete")
    stored_payload = canonical_path.read_bytes()
    stored_digest = digest_path.read_text().strip()
    actual_stored_digest = hashlib.sha256(stored_payload).hexdigest()
    if digest != stored_digest or stored_digest != actual_stored_digest or stored_payload != payload:
        raise PreflightError("goal_mismatch", "goal differs from the durable lane goal")
    return digest


def ensure_owner_controlled_file(path: Path, classification: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PreflightError(classification, f"cannot inspect {path.name}: {error}") from error
    if path.is_symlink() or not path.is_file():
        raise PreflightError(classification, f"{path.name} must be a regular file")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise PreflightError(classification, f"{path.name} must be owner-controlled and not writable by others")


def resolve_controlled_executable(path: Path, label: str, classification: str) -> Path:
    """Resolve a launcher symlink without weakening policy-file checks."""
    try:
        link_metadata = path.lstat()
        resolved = path.resolve(strict=True)
        target_metadata = resolved.stat()
    except OSError as error:
        raise PreflightError(classification, f"cannot inspect {label} executable: {error}") from error
    allowed_owners = {0, os.getuid()}
    if link_metadata.st_uid not in allowed_owners or target_metadata.st_uid not in allowed_owners:
        raise PreflightError(classification, f"{label} executable must be owned by the lane user or root")
    if not resolved.is_file() or target_metadata.st_mode & 0o022 or not os.access(resolved, os.X_OK):
        raise PreflightError(
            classification,
            f"{label} executable target must be regular, executable, and not writable by other users",
        )
    return resolved


def resolve_executable(path: Path) -> Path:
    return resolve_controlled_executable(path, "Gemini", "capability_probe_failed")


def policy_tool_names(rule: dict[str, object]) -> set[str]:
    names = rule.get("toolName")
    if isinstance(names, str):
        return {names}
    if isinstance(names, list) and all(isinstance(name, str) for name in names):
        return set(names)
    return set()


def validate_policy(path: Path, run_dir: Path, gemini_home: Path) -> tuple[str, Path]:
    ensure_owner_controlled_file(path, "invalid_policy")
    try:
        content = path.read_bytes()
        parsed = tomllib.loads(content.decode())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PreflightError("invalid_policy", f"cannot parse policy TOML: {error}") from error
    rules = parsed.get("rule")
    if not isinstance(rules, list) or not rules or not all(isinstance(rule, dict) for rule in rules):
        raise PreflightError("invalid_policy", "policy must contain [[rule]] tables")
    deny_priorities = [
        rule.get("priority", 0) for rule in rules if rule.get("decision") == "deny" and "*" in policy_tool_names(rule)
    ]
    if not deny_priorities or not all(isinstance(priority, int) for priority in deny_priorities):
        raise PreflightError("invalid_policy", "policy requires an integer-priority catch-all deny")
    deny_priority = max(deny_priorities)
    narrow_allow = any(
        rule.get("decision") == "allow"
        and "*" not in policy_tool_names(rule)
        and bool(policy_tool_names(rule) or rule.get("mcpName"))
        and isinstance(rule.get("priority", 0), int)
        and int(rule.get("priority", 0)) > deny_priority
        for rule in rules
    )
    if not narrow_allow:
        raise PreflightError("invalid_policy", "policy requires a narrow allow above the catch-all deny")
    for standard_dir in STANDARD_ADMIN_POLICY_DIRS:
        try:
            if standard_dir.is_dir() and any(standard_dir.glob("*.toml")):
                raise PreflightError(
                    "policy_conflict",
                    "a standard admin policy directory would supersede the supplemental policy",
                )
        except PermissionError as error:
            raise PreflightError("policy_conflict", "cannot prove standard admin policy absence") from error
    write_private(run_dir / "policy.toml", content)
    durable_policy = gemini_home / "control-policy.toml"
    persist_exact_private(durable_policy, content, "runtime_mismatch")
    return hashlib.sha256(content).hexdigest(), durable_policy


def build_system_settings(provider: str) -> bytes:
    settings = {
        "admin": {"extensions": {"enabled": False}},
        "advanced": {"ignoreLocalEnv": True},
        "general": {"enableAutoUpdate": False},
        "security": {
            "disableAlwaysAllow": True,
            "disableYoloMode": True,
            "enablePermanentToolApproval": False,
            "environmentVariableRedaction": {"enabled": True},
            "folderTrust": {"enabled": True},
        },
        "telemetry": {"enabled": False},
        "tools": {
            "sandbox": {
                "command": provider,
                "enabled": True,
                "networkAccess": False,
            },
        },
    }
    return (json.dumps(settings, indent=2, sort_keys=True) + "\n").encode()


def prepare_runtime(state_dir: Path, provider: str, auth_type: str) -> tuple[dict[str, str], Path, Path]:
    if shutil.which(provider) is None:
        raise PreflightError("sandbox_unavailable", f"sandbox provider is unavailable: {provider}")
    if provider == "runsc" and shutil.which("docker") is None:
        raise PreflightError("sandbox_unavailable", "runsc requires Docker")
    gemini_home = state_dir / "gemini-home"
    tmp_dir = state_dir / "tmp"
    gemini_home.mkdir(mode=0o700, exist_ok=True)
    tmp_dir.mkdir(mode=0o700, exist_ok=True)
    for directory in (gemini_home, tmp_dir):
        if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise PreflightError("invalid_path", f"{directory.name} must be owner-only")
    system_settings = gemini_home / "system-settings.json"
    persist_exact_private(system_settings, build_system_settings(provider), "runtime_mismatch")
    user_settings_dir = gemini_home / ".gemini"
    user_settings_dir.mkdir(mode=0o700, exist_ok=True)
    if user_settings_dir.stat().st_uid != os.getuid() or user_settings_dir.stat().st_mode & 0o077:
        raise PreflightError("invalid_path", ".gemini must be owner-only")
    user_settings = user_settings_dir / "settings.json"
    persist_exact_private(
        user_settings,
        (json.dumps({"security": {"auth": {"selectedType": auth_type}}}, indent=2, sort_keys=True) + "\n").encode(),
        "runtime_mismatch",
    )
    inherited = os.environ
    environment = {
        key: inherited[key]
        for key in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TERM", "TZ")
        if inherited.get(key)
    }
    environment.update(
        {
            "GEMINI_CLI_HOME": str(gemini_home),
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(system_settings),
            "GEMINI_SANDBOX": provider,
            "SANDBOX_FLAGS": SANDBOX_FLAGS,
            "TMPDIR": str(tmp_dir),
        }
    )
    return environment, system_settings, gemini_home


def remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PreflightError("timed_out", "operation deadline expired")
    return remaining


def run_probe(
    executable: Path,
    arguments: list[str],
    cwd: Path,
    environment: dict[str, str],
    deadline: float,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    try:
        result = subprocess.run(
            [str(executable), *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=remaining_seconds(deadline),
        )
    except subprocess.TimeoutExpired as error:
        raise PreflightError("timed_out", f"capability probe timed out: {' '.join(arguments)}") from error
    except OSError as error:
        raise PreflightError("capability_probe_failed", f"cannot launch Gemini: {error}") from error
    write_private(stdout_path, result.stdout)
    write_private(stderr_path, result.stderr)
    if result.returncode != 0:
        raise PreflightError("capability_probe_failed", f"Gemini probe exited {result.returncode}")
    return result.stdout


def probe_sandbox_provider(
    args: argparse.Namespace,
    environment: dict[str, str],
    deadline: float,
) -> str:
    provider_path = shutil.which(args.sandbox_provider)
    if provider_path is None:
        raise PreflightError("sandbox_unavailable", f"sandbox provider is unavailable: {args.sandbox_provider}")
    provider = resolve_controlled_executable(Path(provider_path), args.sandbox_provider, "sandbox_unavailable")
    commands = {
        "docker": ["version", "--format", "{{.Server.Version}}"],
        "podman": ["info", "--format", "json"],
        "runsc": ["--version"],
    }
    try:
        run_probe(
            provider,
            commands[args.sandbox_provider],
            args.cwd,
            environment,
            deadline,
            args.run_dir / "sandbox-provider.stdout",
            args.run_dir / "sandbox-provider.stderr",
        )
        if args.sandbox_provider == "runsc":
            docker_path = shutil.which("docker")
            if docker_path is None:
                raise PreflightError("sandbox_unavailable", "runsc requires an accessible Docker server")
            run_probe(
                resolve_controlled_executable(Path(docker_path), "Docker", "sandbox_unavailable"),
                ["version", "--format", "{{.Server.Version}}"],
                args.cwd,
                environment,
                deadline,
                args.run_dir / "sandbox-docker.stdout",
                args.run_dir / "sandbox-docker.stderr",
            )
    except PreflightError as error:
        raise PreflightError("sandbox_unavailable", str(error)) from error
    return str(provider)


def probe_capabilities(args: argparse.Namespace, environment: dict[str, str], deadline: float) -> tuple[str, Path]:
    resolved_executable = resolve_executable(args.gemini)
    version = run_probe(
        args.gemini,
        ["--version"],
        args.cwd,
        environment,
        deadline,
        args.run_dir / "gemini-version.stdout",
        args.run_dir / "gemini-version.stderr",
    ).strip()
    help_text = run_probe(
        args.gemini,
        ["--help"],
        args.cwd,
        environment,
        deadline,
        args.run_dir / "gemini-help.stdout",
        args.run_dir / "gemini-help.stderr",
    )
    missing = [flag for flag in REQUIRED_FLAGS if flag not in help_text]
    if missing:
        raise PreflightError("capability_mismatch", f"Gemini help lacks required flags: {', '.join(missing)}")
    if not version:
        raise PreflightError("capability_mismatch", "Gemini version output is empty")
    return version, resolved_executable


def inventory_group_state(process_group_id: int, inventory: ProcessInventory) -> str:
    members = [info for info in inventory.processes.values() if info.process_group_id == process_group_id]
    if any(not info.state.startswith("Z") for info in members):
        return "alive"
    if inventory.complete and members:
        return "absent"
    return "unknown"


def process_group_state(process_group_id: int, inventory: ProcessInventory) -> str:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return inventory_group_state(process_group_id, inventory)
    return inventory_group_state(process_group_id, inventory)


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


def process_snapshot(timeout_seconds: float = 2) -> ProcessInventory:
    ps = shutil.which("ps")
    if not ps or timeout_seconds <= 0:
        return ProcessInventory({}, False)
    try:
        completed = subprocess.run(
            [ps, "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=min(2, timeout_seconds),
        )
    except (OSError, subprocess.SubprocessError):
        return ProcessInventory({}, False)
    snapshot: dict[int, ProcessInfo] = {}
    complete = completed.returncode == 0
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.split(maxsplit=4)
        if len(fields) != 5:
            complete = False
            continue
        try:
            pid, parent_pid, process_group_id = (int(value) for value in fields[:3])
        except ValueError:
            complete = False
            continue
        snapshot[pid] = ProcessInfo(parent_pid, process_group_id, fields[3], fields[4])
    return ProcessInventory(snapshot, complete and bool(snapshot))


def snapshot_before(deadline: float) -> ProcessInventory | None:
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
    tracked_processes: dict[int, ProcessIdentity],
) -> None:
    root_info = snapshot.get(root_pid)
    if not root_info:
        return
    known_root = tracked_processes.get(root_pid)
    if known_root and known_root.started_at != root_info.started_at:
        return
    tracked_processes[root_pid] = ProcessIdentity(root_info.started_at)
    for pid in descendant_pids(root_pid, snapshot):
        tracked_processes[pid] = ProcessIdentity(snapshot[pid].started_at)


def tracked_descendant_state(
    root_pid: int,
    tracked_processes: dict[int, ProcessIdentity],
    inventory: ProcessInventory,
) -> tuple[str, list[int]]:
    alive: list[int] = []
    identity_unknown = False
    for pid, identity in tracked_processes.items():
        if pid == root_pid:
            continue
        info = inventory.processes.get(pid)
        if info is None:
            identity_unknown = identity_unknown or not inventory.complete
            continue
        if info.started_at != identity.started_at or info.state.startswith("Z"):
            continue
        alive.append(pid)
    if alive:
        return "alive", sorted(alive)
    return ("unknown" if identity_unknown else "absent"), []


def wait_for_harvest(
    root_pid: int,
    process_group_id: int,
    tracked_processes: dict[int, ProcessIdentity],
    deadline: float,
    inventory: ProcessInventory,
    descendant_signal: signal.Signals,
) -> tuple[str, str, list[int], ProcessInventory]:
    while True:
        descendant_state, alive = tracked_descendant_state(root_pid, tracked_processes, inventory)
        for pid in alive:
            signal_process(pid, descendant_signal)
        group_state = process_group_state(process_group_id, inventory)
        if descendant_state == "absent" and group_state == "absent":
            return group_state, descendant_state, alive, inventory
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return group_state, descendant_state, alive, inventory
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        next_inventory = snapshot_before(deadline)
        if next_inventory is None:
            return group_state, descendant_state, alive, inventory
        inventory = next_inventory
        remember_process_tree(root_pid, inventory.processes, tracked_processes)


def harvest_process_tree(
    root_pid: int,
    process_group_id: int,
    tracked_processes: dict[int, ProcessIdentity],
    cleanup_deadline: float,
) -> tuple[bool, str, str, list[int]]:
    group_signaled = signal_process_group(process_group_id, signal.SIGTERM)
    term_deadline = min(time.monotonic() + TERMINATION_GRACE_SECONDS, cleanup_deadline)
    inventory = snapshot_before(term_deadline) or ProcessInventory({}, False)
    remember_process_tree(root_pid, inventory.processes, tracked_processes)
    _descendant_state, live_before = tracked_descendant_state(root_pid, tracked_processes, inventory)
    harvested = bool(live_before or group_signaled)
    for pid in live_before:
        signal_process(pid, signal.SIGTERM)
    group_state, descendant_state, alive, inventory = wait_for_harvest(
        root_pid,
        process_group_id,
        tracked_processes,
        term_deadline,
        inventory,
        signal.SIGTERM,
    )
    if group_state == "absent" and descendant_state == "absent":
        return harvested, group_state, descendant_state, alive
    if group_state != "absent":
        signal_process_group(process_group_id, signal.SIGKILL)
    for pid in alive:
        signal_process(pid, signal.SIGKILL)
    group_state, descendant_state, alive, _inventory = wait_for_harvest(
        root_pid,
        process_group_id,
        tracked_processes,
        cleanup_deadline,
        inventory,
        signal.SIGKILL,
    )
    return harvested, group_state, descendant_state, alive


@contextmanager
def termination_signal_handlers() -> Iterator[None]:
    handled = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in handled}
    interrupted = False

    def interrupt(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        for handled_signum in handled:
            signal.signal(handled_signum, signal.SIG_IGN)
        raise SupervisorInterrupted(signum)

    for signum in handled:
        signal.signal(signum, interrupt)
    try:
        yield
    finally:
        if not interrupted:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def monitor_process(
    process: subprocess.Popen[bytes],
    process_deadline: float,
    tracked_processes: dict[int, ProcessIdentity],
    on_heartbeat: Callable[[], None] | None,
) -> tuple[int, bool, int | None]:
    last_heartbeat = 0.0
    try:
        while True:
            returncode = process.poll()
            if returncode is not None:
                return returncode, False, None
            inventory = snapshot_before(process_deadline)
            if inventory is None:
                return 124, True, None
            remember_process_tree(process.pid, inventory.processes, tracked_processes)
            now = time.monotonic()
            if on_heartbeat and now - last_heartbeat >= 1:
                on_heartbeat()
                last_heartbeat = now
            remaining = process_deadline - now
            if remaining <= 0:
                return 124, True, None
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    except SupervisorInterrupted as error:
        return 128 + error.signum, False, error.signum


def launch_error_result(message: str) -> ProcessResult:
    return ProcessResult(127, None, None, False, False, "absent", "absent", [], [], None, message)


def run_process(
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    on_start: Callable[[int, int], None] | None = None,
    on_heartbeat: Callable[[], None] | None = None,
) -> ProcessResult:
    tracked_processes: dict[int, ProcessIdentity] = {}
    process_deadline = time.monotonic() + timeout_seconds
    cleanup_deadline = process_deadline + CLEANUP_GRACE_SECONDS
    with open_private(stdout_path, "wb") as stdout, open_private(stderr_path, "wb") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError as error:
            return launch_error_result(f"{type(error).__name__}: {error}")
        returncode = 127
        timed_out = False
        interrupted_signal: int | None = None
        try:
            if on_start:
                on_start(process.pid, process.pid)
            with termination_signal_handlers():
                returncode, timed_out, interrupted_signal = monitor_process(
                    process,
                    process_deadline,
                    tracked_processes,
                    on_heartbeat,
                )
        finally:
            final_cleanup_deadline = min(cleanup_deadline, time.monotonic() + CLEANUP_GRACE_SECONDS)
            survivors_harvested, group_state, descendant_state, alive_descendants = harvest_process_tree(
                process.pid,
                process.pid,
                tracked_processes,
                final_cleanup_deadline,
            )
            if process.poll() is None:
                signal_process(process.pid, signal.SIGKILL)
            wait_remaining = final_cleanup_deadline - time.monotonic()
            if wait_remaining > 0:
                try:
                    returncode = process.wait(timeout=wait_remaining)
                except subprocess.TimeoutExpired:
                    returncode = process.poll() if process.poll() is not None else returncode
    return ProcessResult(
        returncode,
        process.pid,
        process.pid,
        timed_out,
        survivors_harvested,
        group_state,
        descendant_state,
        sorted(pid for pid in tracked_processes if pid != process.pid),
        alive_descendants,
        interrupted_signal,
        None,
    )


def prior_attempt_count(state_dir: Path, current_run: Path) -> int:
    runs = state_dir / "runs"
    count = 0
    if not runs.is_dir():
        return count
    for status_path in runs.glob("*/status.json"):
        if status_path.parent == current_run:
            continue
        try:
            payload = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("attempt") is not None:
            count += 1
    return count


def git_output(cwd: Path, *arguments: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=False,
        capture_output=True,
        text=text,
        timeout=10,
    )
    if result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode(errors="replace")
        raise PreflightError("git_invalid", stderr.strip() or "Git command failed")
    return result.stdout


def inspect_git(cwd: Path, goal: Goal) -> GitState:
    if not cwd.is_dir():
        raise PreflightError("git_invalid", "checkout directory is missing")
    head = str(git_output(cwd, "rev-parse", "HEAD")).strip()
    common_raw = str(git_output(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip()
    common = Path(common_raw).resolve()
    if not path_is_within(common, cwd.resolve()):
        raise PreflightError("shared_git_dir", "Git common directory is outside the checkout")
    if head != goal.base_sha:
        raise PreflightError("base_mismatch", "checkout HEAD does not match goal base_sha")
    status_bytes = git_output(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", text=False)
    assert isinstance(status_bytes, bytes)
    if status_bytes:
        raise PreflightError("git_dirty", "checkout must be clean before dispatch")
    return GitState(head=head, common_dir=str(common), status="clean")


def authorize_generation(goal: Goal) -> None:
    if goal.auth_type == "oauth-personal":
        return
    if not goal.allow_paid_generation or os.environ.get(PAID_ACK_ENV) != PAID_ACK_VALUE:
        raise PreflightError(
            "billing_not_authorized",
            f"paid-capable auth requires goal authorization and {PAID_ACK_ENV}={PAID_ACK_VALUE}",
        )


def add_auth_environment(environment: dict[str, str], auth_type: str) -> None:
    names_by_auth = {
        "gemini-api-key": ("GEMINI_API_KEY",),
        "vertex-ai": (
            "GOOGLE_API_KEY",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_GENAI_USE_VERTEXAI",
        ),
        "compute-default-credentials": (
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ),
        "gateway": ("GEMINI_GATEWAY_URL", "GEMINI_GATEWAY_API_KEY"),
    }
    for name in names_by_auth.get(auth_type, ()):
        if os.environ.get(name):
            environment[name] = os.environ[name]


def stage_prompt(args: argparse.Namespace, git_state: GitState, goal: Goal, attempt: int) -> Path:
    ensure_owner_controlled_file(args.prompt_file, "invalid_prompt")
    try:
        content = args.prompt_file.read_bytes()
    except OSError as error:
        raise PreflightError("invalid_prompt", f"cannot read prompt: {error}") from error
    if not content or len(content) > 1024 * 1024:
        raise PreflightError("invalid_prompt", "prompt must contain between 1 byte and 1 MiB")
    write_private(args.run_dir / "prompt.txt", content)
    staging_dir = Path(git_state.common_dir) / "orchestrate-gemini"
    staging_dir.mkdir(mode=0o700, exist_ok=True)
    if staging_dir.stat().st_uid != os.getuid() or staging_dir.stat().st_mode & 0o077:
        raise PreflightError("invalid_path", "prompt staging directory must be owner-only")
    staged = staging_dir / f"{goal.goal_id}-{attempt}-{os.getpid()}.prompt"
    write_private(staged, content)
    return staged


def resume_session(args: argparse.Namespace, goal_digest: str, git_state: GitState) -> str | None:
    if args.resume_from is None:
        return None
    prior_run = args.resume_from.resolve()
    expected_parent = (args.state_dir / "runs").resolve()
    if prior_run.parent != expected_parent or prior_run == args.run_dir.resolve():
        raise PreflightError("invalid_resume", "resume source must be another run in this lane")
    try:
        prior = json.loads((prior_run / "status.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PreflightError("invalid_resume", f"cannot read prior terminal status: {error}") from error
    terminal = bool(prior.get("finished_at")) and prior.get("classification") != "starting"
    same_checkout = prior.get("git_before", {}).get("common_dir") == git_state.common_dir
    session_id = prior.get("session_id")
    if (
        not terminal
        or prior.get("goal_sha256") != goal_digest
        or not same_checkout
        or not is_nonempty_string(session_id)
    ):
        raise PreflightError("invalid_resume", "prior run is not a matching terminal session")
    return str(session_id)


def worker_command(
    args: argparse.Namespace,
    staged_prompt: Path,
    durable_policy: Path,
    resume_id: str | None,
    validation_responses: Path | None,
) -> list[str]:
    command = [
        str(args.gemini),
        "--model",
        args.model,
        "--output-format",
        "stream-json",
        "--approval-mode",
        args.approval_mode,
        "--sandbox",
        "--admin-policy",
        str(durable_policy),
        "--extensions",
        "none",
    ]
    if resume_id:
        command.extend(("--resume", resume_id))
    if validation_responses:
        command.extend(("--fake-responses", str(validation_responses)))
    command.extend(("--prompt", f"@{staged_prompt}"))
    return command


def prepare_validation_responses(args: argparse.Namespace, prepared: PreparedAttempt) -> Path | None:
    source = args.validation_fake_responses
    if source is None:
        authorize_generation(prepared.goal)
        return None
    ensure_owner_controlled_file(source, "invalid_validation_fixture")
    try:
        content = source.read_bytes()
    except OSError as error:
        raise PreflightError("invalid_validation_fixture", f"cannot read fake-response fixture: {error}") from error
    if not content or len(content) > 1024 * 1024:
        raise PreflightError("invalid_validation_fixture", "fake-response fixture must contain 1 byte to 1 MiB")
    retained = args.run_dir / "validation-fake-responses.json"
    write_private(retained, content)
    durable = Path(prepared.environment["GEMINI_CLI_HOME"]) / "validation-fake-responses.json"
    persist_exact_private(durable, content, "runtime_mismatch")
    return durable


def load_stream_events(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(errors="replace")
    except OSError as error:
        raise PreflightError("invalid_output", f"cannot read Gemini output: {error}") from error
    if not text.strip():
        raise PreflightError("no_output", "Gemini emitted no stream events")
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise PreflightError("invalid_output", f"Gemini emitted non-JSON output: {error}") from error
        if not isinstance(event, dict):
            raise PreflightError("invalid_output", "Gemini stream events must be JSON objects")
        events.append(event)
    return events


def parse_stream(path: Path, requested_model: str) -> dict[str, str]:
    events = load_stream_events(path)
    init_events = [event for event in events if event.get("type") == "init"]
    result_events = [event for event in events if event.get("type") == "result"]
    if len(init_events) != 1 or len(result_events) != 1:
        raise PreflightError("invalid_output", "Gemini stream requires exactly one init and one result event")
    init = init_events[0]
    result = result_events[0]
    resolved_model = init.get("model")
    session_id = init.get("session_id")
    if resolved_model != requested_model:
        raise PreflightError("model_mismatch", "Gemini resolved a different model than requested")
    if not is_nonempty_string(session_id):
        raise PreflightError("invalid_output", "Gemini init event lacks a session ID")
    fatal = any(event.get("type") == "error" and event.get("severity") in {"error", "fatal"} for event in events)
    if fatal:
        raise PreflightError("gemini_stream_error", "Gemini emitted a fatal stream error")
    if result.get("status") != "success":
        raise PreflightError("gemini_status_error", "Gemini terminal result was not successful")
    responses = [
        event.get("content") for event in events if event.get("type") == "message" and event.get("role") == "assistant"
    ]
    if not any(is_nonempty_string(response) for response in responses):
        raise PreflightError("no_output", "Gemini emitted no non-empty assistant response")
    return {"session_id": str(session_id), "resolved_model": str(resolved_model)}


def nul_paths(cwd: Path, *arguments: str) -> set[str]:
    output = git_output(cwd, *arguments, text=False)
    assert isinstance(output, bytes)
    return {part.decode(errors="surrogateescape") for part in output.split(b"\0") if part}


def changed_paths(cwd: Path, goal: Goal) -> list[str]:
    ancestry = subprocess.run(
        ["git", "-C", str(cwd), "merge-base", "--is-ancestor", goal.base_sha, "HEAD"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if ancestry.returncode != 0:
        raise PreflightError("git_boundary_violation", "post-run HEAD is not descended from the immutable base")
    paths: set[str] = set()
    paths.update(nul_paths(cwd, "diff", "--no-renames", "--name-only", "-z", f"{goal.base_sha}..HEAD"))
    paths.update(nul_paths(cwd, "diff", "--no-renames", "--name-only", "-z", "HEAD"))
    paths.update(nul_paths(cwd, "diff", "--cached", "--no-renames", "--name-only", "-z"))
    paths.update(nul_paths(cwd, "ls-files", "--others", "--exclude-standard", "-z"))
    return sorted(paths)


def path_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    return any(path == allowed or path.startswith(f"{allowed}/") for allowed in allowed_paths)


def enforce_scope(cwd: Path, goal: Goal, run_dir: Path, artifact_name: str = "changed-paths.json") -> list[str]:
    paths = changed_paths(cwd, goal)
    write_private(run_dir / artifact_name, json.dumps(paths, indent=2) + "\n")
    escaped = [path for path in paths if not path_allowed(path, goal.allowed_paths)]
    if escaped:
        error = PreflightError("scope_violation", f"changed paths escaped the goal scope: {', '.join(escaped)}")
        error.changed_paths = paths  # type: ignore[attr-defined]
        raise error
    return paths


def verification_environment() -> dict[str, str]:
    return {
        name: os.environ[name]
        for name in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TERM", "TZ")
        if os.environ.get(name)
    }


def run_verification(goal: Goal, cwd: Path, run_dir: Path, deadline: float) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index, command in enumerate(goal.verification_commands, start=1):
        timeout = min(float(command.timeout_seconds), remaining_seconds(deadline))
        timed_out = False
        returncode: int | None
        stdout: bytes
        stderr: bytes
        try:
            completed = subprocess.run(
                list(command.argv),
                cwd=cwd,
                env=verification_environment(),
                capture_output=True,
                timeout=timeout,
            )
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            returncode = None
            stdout = error.stdout or b""
            stderr = error.stderr or b""
        except OSError as error:
            returncode = 127
            stdout = b""
            stderr = f"{type(error).__name__}: {error}\n".encode()
        stdout_name = f"verification-{index:03}.stdout"
        stderr_name = f"verification-{index:03}.stderr"
        write_private(run_dir / stdout_name, stdout)
        write_private(run_dir / stderr_name, stderr)
        item = {
            "argv": list(command.argv),
            "returncode": returncode,
            "stderr": stderr_name,
            "stdout": stdout_name,
            "timed_out": timed_out,
            "timeout_seconds": command.timeout_seconds,
        }
        evidence.append(item)
        if timed_out or returncode != 0:
            error = PreflightError("verification_failed", f"verification command {index} failed")
            error.verification = evidence  # type: ignore[attr-defined]
            raise error
    return evidence


def prepare_attempt(args: argparse.Namespace, deadline: float, status: dict[str, Any]) -> PreparedAttempt:
    goal = parse_goal(args.goal_file)
    attempt = prior_attempt_count(args.state_dir, args.run_dir) + 1
    status.update({"attempt": attempt, "goal_id": goal.goal_id})
    if attempt > goal.max_attempts:
        raise PreflightError("attempt_exhausted", "goal attempt budget is exhausted")
    digest = persist_goal(args.state_dir, goal)
    git_state = inspect_git(args.cwd, goal)
    environment, system_settings, gemini_home = prepare_runtime(
        args.state_dir,
        args.sandbox_provider,
        goal.auth_type,
    )
    policy_digest, durable_policy = validate_policy(args.policy_file, args.run_dir, gemini_home)
    sandbox_executable = probe_sandbox_provider(args, environment, deadline)
    version, resolved_executable = probe_capabilities(args, environment, deadline)
    status.update(
        {
            "goal_sha256": digest,
            "git_before": asdict(git_state),
            "gemini_version": version,
            "gemini_executable": str(args.gemini),
            "gemini_executable_resolved": str(resolved_executable),
            "policy_sha256": policy_digest,
            "sandbox_executable": sandbox_executable,
            "runtime": {
                "environment_names": sorted(environment),
                "gemini_home": environment["GEMINI_CLI_HOME"],
                "sandbox_provider": args.sandbox_provider,
                "system_settings": str(system_settings),
                "tmpdir": environment["TMPDIR"],
            },
        }
    )
    lease_payload = {
        "attempt": attempt,
        "goal_id": goal.goal_id,
        "goal_sha256": digest,
        "pid": os.getpid(),
        "started_at": status["started_at"],
        "heartbeat_at": utc_now(),
    }
    write_atomic_private(args.state_dir / "lease.json", lease_payload)
    return PreparedAttempt(goal, attempt, digest, git_state, environment, durable_policy, lease_payload)


def validate_process_result(result: ProcessResult) -> None:
    if result.launch_error:
        raise PreflightError("launch_failed", result.launch_error)
    if result.interrupted_signal is not None:
        raise PreflightError("interrupted", f"worker interrupted by signal {result.interrupted_signal}")
    if result.timed_out:
        raise PreflightError("timed_out", "Gemini worker exceeded the attempt deadline")
    if result.process_group_state_after_harvest != "absent" or result.descendant_state_after_harvest != "absent":
        raise PreflightError("survivor_detected", "worker cleanup could not prove all processes absent")
    if result.returncode != 0:
        raise PreflightError("cli_error", f"Gemini exited {result.returncode}")


def execute_attempt(
    args: argparse.Namespace,
    prepared: PreparedAttempt,
    deadline: float,
    status_path: Path,
    status: dict[str, Any],
) -> int:
    validation_responses = prepare_validation_responses(args, prepared)
    add_auth_environment(prepared.environment, prepared.goal.auth_type)
    resume_id = resume_session(args, prepared.goal_digest, prepared.git_state)
    staged_prompt = stage_prompt(args, prepared.git_state, prepared.goal, prepared.attempt)
    command = worker_command(args, staged_prompt, prepared.durable_policy, resume_id, validation_responses)
    write_private(
        args.run_dir / "command.json",
        json.dumps(
            {
                "argv": command,
                "environment_names": sorted(prepared.environment),
                "prompt_transport": "owner-only-staged-file-reference",
                "validation_mode": validation_responses is not None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )

    def update_lease(worker_pid: int | None = None, process_group_id: int | None = None) -> None:
        prepared.lease_payload["heartbeat_at"] = utc_now()
        if worker_pid is not None:
            prepared.lease_payload["worker_pid"] = worker_pid
        if process_group_id is not None:
            prepared.lease_payload["process_group_id"] = process_group_id
        write_atomic_private(args.state_dir / "lease.json", prepared.lease_payload)

    def worker_started(worker_pid: int, process_group_id: int) -> None:
        status.update(
            {
                "classification": "running",
                "heartbeat_at": utc_now(),
                "process_group_id": process_group_id,
                "worker_pid": worker_pid,
            }
        )
        write_atomic_private(status_path, status)
        update_lease(worker_pid, process_group_id)

    try:
        result = run_process(
            command,
            args.cwd,
            prepared.environment,
            args.run_dir / "stdout.jsonl",
            args.run_dir / "stderr.log",
            remaining_seconds(deadline),
            on_start=worker_started,
            on_heartbeat=update_lease,
        )
    finally:
        staged_prompt.unlink(missing_ok=True)
    status["process"] = asdict(result)
    validate_process_result(result)
    status.update(parse_stream(args.run_dir / "stdout.jsonl", args.model))
    enforce_scope(args.cwd, prepared.goal, args.run_dir)
    verification = run_verification(prepared.goal, args.cwd, args.run_dir, deadline)
    paths = enforce_scope(args.cwd, prepared.goal, args.run_dir, "changed-paths-after-verification.json")
    status.update(
        {
            "changed_paths": paths,
            "git_after": {
                "head": str(git_output(args.cwd, "rev-parse", "HEAD")).strip(),
                "status": str(git_output(args.cwd, "status", "--short")),
            },
            "verification": verification,
            "validation_mode": validation_responses is not None,
        }
    )
    classification = "validation_succeeded" if validation_responses else "succeeded"
    return finish_status(status_path, status, classification)


def run_locked_attempt(
    args: argparse.Namespace,
    deadline: float,
    status_path: Path,
    status: dict[str, Any],
) -> int:
    try:
        prepared = prepare_attempt(args, deadline, status)
        if args.preflight_only:
            return finish_status(status_path, status, "preflight_succeeded")
        return execute_attempt(args, prepared, deadline, status_path, status)
    except PreflightError as error:
        if hasattr(error, "changed_paths"):
            status["changed_paths"] = error.changed_paths
        if hasattr(error, "verification"):
            status["verification"] = error.verification
        return finish_status(status_path, status, error.classification, str(error))
    except Exception as error:  # noqa: BLE001 - terminal evidence must survive wrapper faults.
        return finish_status(status_path, status, "internal_error", f"{type(error).__name__}: {error}")


def main() -> int:
    args = parse_args()
    deadline = time.monotonic() + args.timeout_seconds
    try:
        prepare_run_directory(args.run_dir, args.state_dir, args.cwd)
    except (FileExistsError, OSError, PreflightError) as error:
        classification = error.classification if isinstance(error, PreflightError) else "invalid_path"
        print(f"{classification}: {error}", file=sys.stderr)
        return 1
    status_path = args.run_dir / "status.json"
    status: dict[str, Any] = {"classification": "starting", "started_at": utc_now()}
    write_atomic_private(status_path, status)
    lease_path = args.state_dir / "lease.lock"
    lease_descriptor = os.open(lease_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    with os.fdopen(lease_descriptor, "r+") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return finish_status(status_path, status, "lane_locked", "another writer holds the lane lease")
        return run_locked_attempt(args, deadline, status_path, status)


if __name__ == "__main__":
    raise SystemExit(main())
