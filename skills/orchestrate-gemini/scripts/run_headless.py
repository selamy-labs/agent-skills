#!/usr/bin/env python3
"""Run one bounded Gemini CLI turn in an isolated, auditable lane."""

from __future__ import annotations

import argparse
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
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
    "sandbox_image",
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
IMAGE_PATTERN = re.compile(r"[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}\Z")
SUPPORTED_GEMINI_VERSION = "0.51.0"
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
API_KEY_PLACEHOLDER = "__ORCHESTRATE_GEMINI_RUNTIME_SECRET__"
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
    sandbox_image: str


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
    gemini_executable: Path
    prompt_content: bytes
    sandbox_executable: Path
    lane_label: str
    filesystem_before: dict[str, str]
    git_metadata_before: dict[str, str]


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
        raw = json.loads(read_owner_only_file(path, "invalid_goal").decode())
    except (UnicodeError, json.JSONDecodeError) as error:
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
        if not isinstance(raw["sandbox_image"], str) or not IMAGE_PATTERN.fullmatch(raw["sandbox_image"]):
            raise ValueError("sandbox_image must be an immutable image@sha256 digest reference")
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
        sandbox_image=raw["sandbox_image"],
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
        output.flush()
        os.fsync(output.fileno())


def write_atomic_private(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write_private(temporary, json.dumps(payload, sort_keys=True, indent=2) + "\n")
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_owner_only_file(path: Path, classification: str, maximum_bytes: int = 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PreflightError(classification, f"cannot open owner-only file: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise PreflightError(classification, "file must be regular, current-user-owned, and owner-only")
        content = bytearray()
        while chunk := os.read(descriptor, min(65536, maximum_bytes + 1 - len(content))):
            content.extend(chunk)
            if len(content) > maximum_bytes:
                raise PreflightError(classification, f"file exceeds {maximum_bytes} bytes")
        return bytes(content)
    finally:
        os.close(descriptor)


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


def persist_exact_executable(path: Path, content: bytes) -> None:
    persist_exact_private(path, content, "runtime_mismatch")
    path.chmod(0o700)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 or not os.access(path, os.X_OK):
        raise PreflightError("runtime_mismatch", f"runtime helper is not owner-only executable: {path.name}")


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
    parser.add_argument("--credential-env-file", type=lambda value, p=parser: absolute_path(p, value))
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
    group_writable = bool(target_metadata.st_mode & 0o020)
    private_primary_group = False
    if group_writable and target_metadata.st_uid == os.getuid() and target_metadata.st_gid == os.getgid():
        current_name = pwd.getpwuid(os.getuid()).pw_name
        primary_users = {entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == target_metadata.st_gid}
        listed_members = set(grp.getgrgid(target_metadata.st_gid).gr_mem)
        private_primary_group = primary_users <= {current_name} and listed_members <= {current_name}
    unsafe_permissions = bool(target_metadata.st_mode & 0o002) or (group_writable and not private_primary_group)
    if not resolved.is_file() or unsafe_permissions or not os.access(resolved, os.X_OK):
        raise PreflightError(
            classification,
            f"{label} executable target must be regular, executable, and not writable by another user or group",
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


def policy_priority(rule: dict[str, object]) -> int:
    priority = rule.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise PreflightError("invalid_policy", "every policy priority must be an integer")
    return priority


def catch_all_deny(rules: list[dict[str, object]]) -> dict[str, object]:
    unconditional_keys = {"toolName", "decision", "priority", "description"}
    candidates = [
        rule
        for rule in rules
        if rule.get("decision") == "deny"
        and policy_tool_names(rule) == {"*"}
        and set(rule) <= unconditional_keys
        and isinstance(rule.get("priority", 0), int)
        and not isinstance(rule.get("priority", 0), bool)
    ]
    if len(candidates) != 1:
        raise PreflightError(
            "invalid_policy",
            "policy requires exactly one unconditional integer-priority catch-all deny",
        )
    return candidates[0]


def is_narrow_allow(rule: dict[str, object], deny_priority: int) -> bool:
    names = policy_tool_names(rule)
    mcp_name = rule.get("mcpName")
    narrow_names = bool(names) and all("*" not in name for name in names)
    narrow_mcp = isinstance(mcp_name, str) and bool(mcp_name) and "*" not in mcp_name
    return rule.get("decision") == "allow" and (narrow_names or narrow_mcp) and policy_priority(rule) > deny_priority


def validate_policy_rules(rules: list[dict[str, object]]) -> None:
    default_deny = catch_all_deny(rules)
    deny_priority = policy_priority(default_deny)
    for rule in rules:
        if rule.get("decision") not in {"allow", "deny", "ask_user"}:
            raise PreflightError("invalid_policy", "policy decision is unsupported")
        if "*" in policy_tool_names(rule) and rule is not default_deny:
            raise PreflightError("invalid_policy", "only the unconditional deny may match every tool")
        mcp_name = rule.get("mcpName")
        if isinstance(mcp_name, str) and "*" in mcp_name:
            raise PreflightError("invalid_policy", "wildcard MCP rules are forbidden")
        policy_priority(rule)
        if rule.get("decision") == "allow" and not is_narrow_allow(rule, deny_priority):
            raise PreflightError("invalid_policy", "every allow must be narrow and above the catch-all deny")
    if not any(is_narrow_allow(rule, deny_priority) for rule in rules):
        raise PreflightError("invalid_policy", "policy requires a narrow allow above the catch-all deny")


def validate_policy(path: Path, run_dir: Path, gemini_home: Path) -> tuple[str, Path]:
    try:
        content = read_owner_only_file(path, "invalid_policy")
        parsed = tomllib.loads(content.decode())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PreflightError("invalid_policy", f"cannot parse policy TOML: {error}") from error
    rules = parsed.get("rule")
    if not isinstance(rules, list) or not rules or not all(isinstance(rule, dict) for rule in rules):
        raise PreflightError("invalid_policy", "policy must contain [[rule]] tables")
    validate_policy_rules(rules)
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
    durable_policy = gemini_home / ".gemini" / "control-policy.toml"
    persist_exact_private(durable_policy, content, "runtime_mismatch")
    return hashlib.sha256(content).hexdigest(), durable_policy


def build_system_settings(provider: str, image: str, auth_type: str) -> bytes:
    settings = {
        "admin": {
            "extensions": {"enabled": False},
            "mcp": {"enabled": False},
            "skills": {"enabled": False},
        },
        "advanced": {"ignoreLocalEnv": True},
        "billing": {"overageStrategy": "never"},
        "general": {"enableAutoUpdate": False},
        "hooksConfig": {"enabled": False},
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
                "image": image,
                # Live runs use Gemini's internal proxy network. Validation adds
                # --network=none through SANDBOX_FLAGS. Keeping this immutable
                # allows preflight and execution to share one durable runtime.
                "networkAccess": True,
            },
        },
    }
    settings["security"]["auth"] = {"selectedType": auth_type}
    return (json.dumps(settings, indent=2, sort_keys=True) + "\n").encode()


def prepare_runtime(
    state_dir: Path,
    provider: str,
    goal: Goal,
    cwd: Path,
) -> tuple[dict[str, str], Path, Path]:
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
    user_settings_dir = gemini_home / ".gemini"
    user_settings_dir.mkdir(mode=0o700, exist_ok=True)
    if user_settings_dir.stat().st_uid != os.getuid() or user_settings_dir.stat().st_mode & 0o077:
        raise PreflightError("invalid_path", ".gemini must be owner-only")
    system_settings = user_settings_dir / "system-settings.json"
    settings_content = build_system_settings(provider, goal.sandbox_image, goal.auth_type)
    persist_exact_private(system_settings, settings_content, "runtime_mismatch")
    user_settings = user_settings_dir / "settings.json"
    persist_exact_private(user_settings, settings_content, "runtime_mismatch")
    persist_exact_private(
        user_settings_dir / "trustedFolders.json",
        (json.dumps({str(cwd.resolve()): "TRUST_FOLDER"}, indent=2, sort_keys=True) + "\n").encode(),
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
            "SANDBOX_ENV": (f"GEMINI_CLI_HOME={gemini_home},GEMINI_CLI_SYSTEM_SETTINGS_PATH={system_settings}"),
            "TMPDIR": str(tmp_dir),
            "HOME": str(gemini_home),
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
    probe_label: str = "Gemini",
) -> str:
    result = run_process(
        [str(executable), *arguments],
        cwd,
        environment,
        stdout_path,
        stderr_path,
        remaining_seconds(deadline),
    )
    if result.timed_out:
        raise PreflightError("timed_out", f"{probe_label} probe timed out: {' '.join(arguments)}")
    if result.interrupted_signal is not None:
        raise PreflightError("interrupted", f"{probe_label} probe was interrupted")
    if result.launch_error:
        raise PreflightError("capability_probe_failed", f"cannot launch {probe_label}: {result.launch_error}")
    if result.process_group_state_after_harvest != "absent" or result.descendant_state_after_harvest != "absent":
        raise PreflightError("capability_probe_failed", f"{probe_label} probe cleanup was incomplete")
    if result.returncode != 0:
        raise PreflightError("capability_probe_failed", f"{probe_label} probe exited {result.returncode}")
    return stdout_path.read_text(errors="replace")


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
            f"{args.sandbox_provider} sandbox",
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
                "Docker sandbox",
            )
    except PreflightError as error:
        raise PreflightError("sandbox_unavailable", str(error)) from error
    if args.sandbox_provider == "runsc":
        assert docker_path is not None
        return str(resolve_controlled_executable(Path(docker_path), "Docker", "sandbox_unavailable"))
    return str(provider)


def lane_label(state_dir: Path) -> str:
    return hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()


def configure_provider_guard(
    environment: dict[str, str],
    args: argparse.Namespace,
    goal: Goal,
    git_state: GitState,
    provider: Path,
    label: str,
    live_mode: bool,
) -> None:
    helper_source = Path(__file__).with_name("provider_guard.py")
    proxy_source = Path(__file__).with_name("api_egress_proxy.mjs")
    try:
        guard_content = helper_source.read_bytes()
        proxy_content = proxy_source.read_bytes()
    except OSError as error:
        raise PreflightError("runtime_mismatch", f"cannot read bundled runtime helper: {error}") from error
    guard_dir = args.state_dir / "provider-guard"
    guard_dir.mkdir(mode=0o700, exist_ok=True)
    if guard_dir.stat().st_uid != os.getuid() or guard_dir.stat().st_mode & 0o077:
        raise PreflightError("runtime_mismatch", "provider guard directory must be owner-only")
    guard = guard_dir / ("docker" if args.sandbox_provider == "runsc" else args.sandbox_provider)
    persist_exact_executable(guard, guard_content)
    allowed_paths: list[str] = []
    for relative in goal.allowed_paths:
        candidate = (args.cwd / relative).resolve()
        if not path_is_within(candidate, args.cwd.resolve()) or not candidate.exists():
            raise PreflightError("invalid_goal", f"allowed path must already exist inside the checkout: {relative}")
        allowed_paths.append(str(candidate))
    environment.update(
        {
            "ORCHESTRATE_GEMINI_ALLOWED_PATHS": json.dumps(allowed_paths, separators=(",", ":")),
            "ORCHESTRATE_GEMINI_CONTAINER_LABEL": container_filter(label),
            "ORCHESTRATE_GEMINI_LIVE_MODE": "1" if live_mode else "0",
            "ORCHESTRATE_GEMINI_PROVIDER_REAL": str(provider),
            "ORCHESTRATE_GEMINI_SANDBOX_IMAGE": goal.sandbox_image,
            "ORCHESTRATE_GEMINI_STATE_DIR": str(args.state_dir.resolve()),
            "ORCHESTRATE_GEMINI_WORKSPACE": str(args.cwd.resolve()),
            "PATH": f"{guard_dir}:{environment.get('PATH', '')}",
        }
    )
    protected_mounts = [
        git_state.common_dir,
        str(Path(environment["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])),
        str(Path(environment["GEMINI_CLI_HOME"]) / ".gemini" / "settings.json"),
        str(Path(environment["GEMINI_CLI_HOME"]) / ".gemini" / "trustedFolders.json"),
        str(Path(environment["GEMINI_CLI_HOME"]) / ".gemini" / "control-policy.toml"),
    ]
    if any("," in mount for mount in protected_mounts):
        raise PreflightError("invalid_path", "control paths containing commas are unsupported")
    environment["SANDBOX_MOUNTS"] = ",".join(f"{mount}:{mount}:ro" for mount in protected_mounts)
    if live_mode:
        proxy_dir = Path(git_state.common_dir) / "orchestrate-gemini"
        proxy_dir.mkdir(mode=0o700, exist_ok=True)
        proxy = proxy_dir / "api-egress-proxy.mjs"
        persist_exact_executable(proxy, proxy_content)
        environment["GEMINI_SANDBOX_PROXY_COMMAND"] = f"node {proxy}"


def container_filter(label: str) -> str:
    return f"io.selamy.orchestrate-gemini.lane={label}"


def provider_capture(
    provider: Path,
    arguments: list[str],
    cwd: Path,
    deadline: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(provider), *arguments],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=min(10.0, remaining_seconds(deadline)),
            env={key: os.environ[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "PATH", "TZ") if os.environ.get(key)},
        )
    except subprocess.TimeoutExpired as error:
        raise PreflightError("timed_out", "sandbox runtime inspection exceeded the deadline") from error


def attest_sandbox_image(provider: Path, image: str, cwd: Path, deadline: float) -> None:
    result = provider_capture(provider, ["image", "inspect", "--format", "{{json .RepoDigests}}", image], cwd, deadline)
    if result.returncode != 0 or image not in result.stdout:
        raise PreflightError("sandbox_image_unavailable", "the digest-pinned sandbox image is not present locally")


def owned_container_ids(provider: Path, label: str, cwd: Path, deadline: float) -> list[str]:
    result = provider_capture(provider, ["ps", "-aq", "--filter", f"label={container_filter(label)}"], cwd, deadline)
    if result.returncode != 0:
        raise PreflightError("sandbox_unavailable", "cannot query owned sandbox containers")
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", container_id) for container_id in ids):
        raise PreflightError("sandbox_recovery_failed", "sandbox runtime returned an invalid container identity")
    return ids


def remove_owned_containers(provider: Path, label: str, cwd: Path, deadline: float) -> list[str]:
    ids = owned_container_ids(provider, label, cwd, deadline)
    if not ids:
        return []
    result = provider_capture(provider, ["rm", "-f", *ids], cwd, deadline)
    if result.returncode != 0 or owned_container_ids(provider, label, cwd, deadline):
        raise PreflightError("sandbox_recovery_failed", "could not remove all lane-owned containers")
    return ids


def remove_stale_runtime_containers(provider: Path, cwd: Path, deadline: float) -> list[str]:
    result = provider_capture(
        provider,
        [
            "ps",
            "-aq",
            "--filter",
            "label=io.selamy.orchestrate-gemini.lane",
            "--filter",
            f"label=io.selamy.orchestrate-gemini.owner-uid={os.getuid()}",
        ],
        cwd,
        deadline,
    )
    if result.returncode != 0:
        raise PreflightError("sandbox_recovery_failed", "cannot enumerate stale controller containers")
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if ids:
        removal = provider_capture(provider, ["rm", "-f", *ids], cwd, deadline)
        if removal.returncode != 0:
            raise PreflightError("sandbox_recovery_failed", "cannot remove stale controller containers")
    proxy = provider_capture(
        provider,
        ["inspect", "--format", "{{json .Config.Labels}}", "gemini-cli-sandbox-proxy"],
        cwd,
        deadline,
    )
    if proxy.returncode == 0:
        raise PreflightError(
            "sandbox_collision",
            "Gemini's global proxy container name is occupied by a non-owned runtime",
        )
    return ids


@contextmanager
def global_execution_lease() -> Iterator[None]:
    runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/orchestrate-gemini-{os.getuid()}"))
    runtime_root.mkdir(mode=0o700, exist_ok=True)
    metadata = runtime_root.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise PreflightError("transport_lock_invalid", "global runtime lock directory must be owner-only")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(runtime_root / "gemini-v0.51-execution.lock", flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PreflightError("transport_locked", "another Gemini v0.51 container execution is active") from error
        yield


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
    if version != SUPPORTED_GEMINI_VERSION:
        raise PreflightError(
            "capability_mismatch",
            f"Gemini version must be exactly {SUPPORTED_GEMINI_VERSION}; found {version or 'empty output'}",
        )
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
    term_deadline = min(time.monotonic() + TERMINATION_GRACE_SECONDS, cleanup_deadline)
    inventory = snapshot_before(term_deadline) or ProcessInventory({}, False)
    remember_process_tree(root_pid, inventory.processes, tracked_processes)
    # Capture descendants while the root still owns them. Signaling the root
    # first can reparent a detached child before its identity is recorded.
    group_signaled = signal_process_group(process_group_id, signal.SIGTERM)
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


def recover_stale_worker(state_dir: Path) -> dict[str, Any] | None:
    lease_path = state_dir / "lease.json"
    if not lease_path.is_file():
        return None
    try:
        lease = json.loads(lease_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PreflightError("stale_worker_unresolved", f"cannot read prior worker lease: {error}") from error
    if lease.get("worker_state") == "absent" or "worker_pid" not in lease:
        return None
    pid = lease.get("worker_pid")
    process_group_id = lease.get("process_group_id")
    started_at = lease.get("worker_started_at")
    if not isinstance(pid, int) or not isinstance(process_group_id, int):
        raise PreflightError("stale_worker_unresolved", "prior worker lease has invalid process identity")
    inventory = process_snapshot(CLEANUP_GRACE_SECONDS)
    info = inventory.processes.get(pid)
    if info is None and inventory.complete:
        lease["worker_state"] = "absent"
        lease["reconciled_at"] = utc_now()
        write_atomic_private(lease_path, lease)
        return {"worker_pid": pid, "action": "already_absent"}
    if not isinstance(started_at, str) or info is None or info.started_at != started_at:
        raise PreflightError("stale_worker_unresolved", "cannot prove the stale worker process identity")
    tracked = {pid: ProcessIdentity(started_at)}
    remember_process_tree(pid, inventory.processes, tracked)
    _harvested, group_state, descendant_state, alive = harvest_process_tree(
        pid,
        process_group_id,
        tracked,
        time.monotonic() + CLEANUP_GRACE_SECONDS,
    )
    if group_state != "absent" or descendant_state != "absent":
        raise PreflightError("stale_worker_unresolved", f"stale worker survivors remain: {alive}")
    lease["worker_state"] = "absent"
    lease["reconciled_at"] = utc_now()
    write_atomic_private(lease_path, lease)
    return {"worker_pid": pid, "action": "harvested"}


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
    output_paths: tuple[Path, Path],
) -> tuple[int, bool, int | None]:
    last_output_sizes = tuple(path.stat().st_size for path in output_paths)
    try:
        while True:
            returncode = process.poll()
            if returncode is not None:
                return returncode, False, None
            inventory = snapshot_before(process_deadline)
            if inventory is None:
                return 124, True, None
            remember_process_tree(process.pid, inventory.processes, tracked_processes)
            output_sizes = tuple(path.stat().st_size for path in output_paths)
            if on_heartbeat and output_sizes != last_output_sizes:
                on_heartbeat()
                last_output_sizes = output_sizes
            now = time.monotonic()
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
        with termination_signal_handlers():
            try:
                if on_start:
                    on_start(process.pid, process.pid)
                returncode, timed_out, interrupted_signal = monitor_process(
                    process,
                    process_deadline,
                    tracked_processes,
                    on_heartbeat,
                    (stdout_path, stderr_path),
                )
            finally:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(signum, signal.SIG_IGN)
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


def git_output(
    cwd: Path,
    *arguments: str,
    text: bool = True,
    deadline: float | None = None,
) -> str | bytes:
    timeout = 10.0 if deadline is None else min(10.0, remaining_seconds(deadline))
    git = shutil.which("git")
    if git is None:
        raise PreflightError("git_invalid", "Git executable is unavailable")
    environment = provider_environment()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_EXTERNAL_DIFF": os.devnull,
        }
    )
    try:
        result = subprocess.run(
            [
                git,
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(cwd),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=text,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise PreflightError("timed_out", "Git inspection exceeded the attempt deadline") from error
    if result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode(errors="replace")
        raise PreflightError("git_invalid", stderr.strip() or "Git command failed")
    return result.stdout


def inspect_git(cwd: Path, goal: Goal, resume_requested: bool = False, deadline: float | None = None) -> GitState:
    if not cwd.is_dir():
        raise PreflightError("git_invalid", "checkout directory is missing")
    head = str(git_output(cwd, "rev-parse", "HEAD", deadline=deadline)).strip()
    common_raw = str(
        git_output(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", deadline=deadline)
    ).strip()
    common = Path(common_raw).resolve()
    if not path_is_within(common, cwd.resolve()):
        raise PreflightError("shared_git_dir", "Git common directory is outside the checkout")
    status_bytes = git_output(
        cwd,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        text=False,
        deadline=deadline,
    )
    assert isinstance(status_bytes, bytes)
    if not resume_requested and head != goal.base_sha:
        raise PreflightError("base_mismatch", "checkout HEAD does not match goal base_sha")
    if not resume_requested and status_bytes:
        raise PreflightError("git_dirty", "checkout must be clean before dispatch")
    if resume_requested:
        paths = changed_paths(cwd, goal, deadline)
        escaped = [path for path in paths if not path_allowed(path, goal.allowed_paths)]
        if escaped:
            raise PreflightError("scope_violation", f"resume checkout escaped goal scope: {', '.join(escaped)}")
    state = "in_scope_changes" if status_bytes or head != goal.base_sha else "clean"
    return GitState(head=head, common_dir=str(common), status=state)


def authorize_generation(goal: Goal) -> None:
    if not goal.allow_paid_generation or os.environ.get(PAID_ACK_ENV) != PAID_ACK_VALUE:
        raise PreflightError(
            "billing_not_authorized",
            f"live generation requires goal authorization and {PAID_ACK_ENV}={PAID_ACK_VALUE}",
        )
    if goal.auth_type != "gemini-api-key":
        raise PreflightError(
            "unsupported_auth",
            "this v0.51 workflow supports live runs only with paid Gemini API-key authentication",
        )


def prepare_live_credential(args: argparse.Namespace, prepared: PreparedAttempt) -> str:
    if any(os.environ.get(name) for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY")):
        raise PreflightError(
            "unsafe_auth_environment",
            "unset API-key environment variables and use only --credential-env-file",
        )
    if args.credential_env_file is None:
        raise PreflightError("credential_unavailable", "live API-key execution requires --credential-env-file")
    content = read_owner_only_file(args.credential_env_file, "invalid_credential", 16 * 1024)
    try:
        text = content.decode()
    except UnicodeError as error:
        raise PreflightError("invalid_credential", "credential env file must be UTF-8") from error
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith("GEMINI_API_KEY=") or lines[0] == "GEMINI_API_KEY=":
        raise PreflightError("invalid_credential", "credential env file must contain only GEMINI_API_KEY=<value>")
    durable = args.state_dir / "runtime-secret.env"
    persist_exact_private(durable, content if content.endswith(b"\n") else content + b"\n", "credential_mismatch")
    prepared.environment.update(
        {
            "GEMINI_API_KEY": API_KEY_PLACEHOLDER,
            "ORCHESTRATE_GEMINI_CREDENTIAL_ENV_FILE": str(durable),
        }
    )
    return str(durable)


def validate_prompt_source(path: Path) -> bytes:
    content = read_owner_only_file(path, "invalid_prompt")
    if not content:
        raise PreflightError("invalid_prompt", "prompt must contain between 1 byte and 1 MiB")
    return content


def stage_prompt(
    git_state: GitState,
    goal: Goal,
    attempt: int,
    content: bytes,
) -> Path:
    staging_dir = Path(git_state.common_dir) / "orchestrate-gemini"
    staging_dir.mkdir(mode=0o700, exist_ok=True)
    if staging_dir.stat().st_uid != os.getuid() or staging_dir.stat().st_mode & 0o077:
        raise PreflightError("invalid_path", "prompt staging directory must be owner-only")
    staged = staging_dir / f"{goal.goal_id}-{attempt}-{os.getpid()}.prompt"
    write_private(staged, content)
    return staged


def execution_request_digest(
    args: argparse.Namespace,
    prepared: PreparedAttempt,
    status: dict[str, Any],
    validation_fixture_digest: str | None,
) -> str:
    payload = {
        "approval_mode": args.approval_mode,
        "gemini_executable": str(prepared.gemini_executable),
        "gemini_version": status.get("gemini_version"),
        "goal_sha256": prepared.goal_digest,
        "model": args.model,
        "policy_sha256": status.get("policy_sha256"),
        "sandbox_image": prepared.goal.sandbox_image,
        "validation_fixture_sha256": validation_fixture_digest,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resume_session(
    args: argparse.Namespace,
    request_digest: str,
    git_state: GitState,
    validation_mode: bool,
) -> str | None:
    if args.resume_from is None:
        return None
    if validation_mode:
        raise PreflightError("invalid_resume", "validation-only sessions are not resumable delivery sessions")
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
        or prior.get("classification") != "succeeded"
        or prior.get("execution_request_sha256") != request_digest
        or not same_checkout
        or not is_nonempty_string(session_id)
    ):
        raise PreflightError("invalid_resume", "prior run is not a matching terminal session")
    process = prior.get("process")
    if not isinstance(process, dict) or (
        process.get("process_group_state_after_harvest") != "absent"
        or process.get("descendant_state_after_harvest") != "absent"
    ):
        raise PreflightError("invalid_resume", "prior run lacks complete worker-harvest evidence")
    return str(session_id)


def worker_command(
    args: argparse.Namespace,
    gemini_executable: Path,
    staged_prompt: Path,
    durable_policy: Path,
    resume_id: str | None,
    validation_responses: Path | None,
) -> list[str]:
    command = [
        str(gemini_executable),
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


def prepare_validation_responses(args: argparse.Namespace, prepared: PreparedAttempt) -> tuple[Path, str] | None:
    source = args.validation_fake_responses
    if source is None:
        return None
    content = read_owner_only_file(source, "invalid_validation_fixture")
    if not content:
        raise PreflightError("invalid_validation_fixture", "fake-response fixture must contain 1 byte to 1 MiB")
    retained = args.run_dir / "validation-fake-responses.json"
    write_private(retained, content)
    durable = Path(prepared.environment["GEMINI_CLI_HOME"]) / ".gemini" / "validation-fake-responses.json"
    persist_exact_private(durable, content, "runtime_mismatch")
    return durable, hashlib.sha256(content).hexdigest()


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


def validate_event_shape(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    allowed_fields = {
        "init": {"type", "timestamp", "session_id", "model"},
        "message": {"type", "timestamp", "role", "content", "delta"},
        "tool_use": {"type", "timestamp", "tool_name", "tool_id", "parameters"},
        "tool_result": {"type", "timestamp", "tool_id", "status", "output", "error"},
        "error": {"type", "timestamp", "severity", "message"},
        "result": {"type", "timestamp", "status", "error", "stats"},
    }
    if event_type not in allowed_fields or not set(event) <= allowed_fields[event_type]:
        raise PreflightError("invalid_output", "Gemini emitted an unknown or malformed stream event")
    timestamp = event.get("timestamp")
    if not is_nonempty_string(timestamp):
        raise PreflightError("invalid_output", "Gemini stream event lacks a timestamp")
    try:
        datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError as error:
        raise PreflightError("invalid_output", "Gemini stream timestamp is invalid") from error
    required_strings = {
        "init": ("session_id", "model"),
        "message": ("role", "content"),
        "tool_use": ("tool_name", "tool_id"),
        "tool_result": ("tool_id", "status"),
        "error": ("severity", "message"),
        "result": ("status",),
    }
    if any(not is_nonempty_string(event.get(field)) for field in required_strings[event_type]):
        raise PreflightError("invalid_output", f"Gemini {event_type} event lacks required string fields")
    if event_type == "message" and event.get("role") not in {"user", "assistant"}:
        raise PreflightError("invalid_output", "Gemini message role is invalid")
    if event_type == "error" and event.get("severity") not in {"warning", "error"}:
        raise PreflightError("invalid_output", "Gemini error severity is invalid")
    if event_type in {"tool_result", "result"} and event.get("status") not in {"success", "error"}:
        raise PreflightError("invalid_output", "Gemini result status is invalid")
    if event_type == "tool_use" and not isinstance(event.get("parameters"), dict):
        raise PreflightError("invalid_output", "Gemini tool_use parameters are invalid")


def stream_terminals(events: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    init_events = [event for event in events if event.get("type") == "init"]
    result_events = [event for event in events if event.get("type") == "result"]
    if len(init_events) != 1 or len(result_events) != 1:
        raise PreflightError("invalid_output", "Gemini stream requires exactly one init and one result event")
    if events[0].get("type") != "init" or events[-1].get("type") != "result":
        raise PreflightError("invalid_output", "Gemini stream must begin with init and end with result")
    return init_events[0], result_events[0]


def validate_stream_content(
    events: list[dict[str, Any]],
    result: dict[str, Any],
    requested_model: str,
    validation_mode: bool,
) -> None:
    if any(event.get("type") == "error" and event.get("severity") == "error" for event in events):
        raise PreflightError("gemini_stream_error", "Gemini emitted a fatal stream error")
    if result.get("status") != "success":
        raise PreflightError("gemini_status_error", "Gemini terminal result was not successful")
    responses = [
        event.get("content") for event in events if event.get("type") == "message" and event.get("role") == "assistant"
    ]
    if not any(is_nonempty_string(response) for response in responses):
        raise PreflightError("no_output", "Gemini emitted no non-empty assistant response")
    tool_ids = {str(event["tool_id"]) for event in events if event.get("type") == "tool_use"}
    result_ids = {str(event["tool_id"]) for event in events if event.get("type") == "tool_result"}
    if tool_ids != result_ids:
        raise PreflightError("invalid_output", "Gemini stream has unmatched tool lifecycle events")
    stats = result.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if not validation_mode and (not isinstance(models, dict) or set(models) != {requested_model}):
        raise PreflightError(
            "model_mismatch",
            "result statistics do not prove exclusive use of the requested model",
        )


def parse_stream(path: Path, requested_model: str, validation_mode: bool) -> dict[str, str]:
    events = load_stream_events(path)
    for event in events:
        validate_event_shape(event)
    init, result = stream_terminals(events)
    reported_model = init.get("model")
    session_id = init.get("session_id")
    if reported_model != requested_model:
        raise PreflightError("model_mismatch", "Gemini reported a different configured model than requested")
    if not is_nonempty_string(session_id):
        raise PreflightError("invalid_output", "Gemini init event lacks a session ID")
    validate_stream_content(events, result, requested_model, validation_mode)
    return {"session_id": str(session_id), "reported_model": str(reported_model)}


def nul_paths(cwd: Path, *arguments: str, deadline: float | None = None) -> set[str]:
    output = git_output(cwd, *arguments, text=False, deadline=deadline)
    assert isinstance(output, bytes)
    return {part.decode(errors="surrogateescape") for part in output.split(b"\0") if part}


def changed_paths(cwd: Path, goal: Goal, deadline: float | None = None) -> list[str]:
    try:
        git_output(cwd, "merge-base", "--is-ancestor", goal.base_sha, "HEAD", deadline=deadline)
    except PreflightError as error:
        if error.classification == "timed_out":
            raise
        raise PreflightError("git_boundary_violation", "post-run HEAD is not descended from the immutable base")
    paths: set[str] = set()
    paths.update(
        nul_paths(cwd, "diff", "--no-renames", "--name-only", "-z", f"{goal.base_sha}..HEAD", deadline=deadline)
    )
    paths.update(nul_paths(cwd, "diff", "--no-renames", "--name-only", "-z", "HEAD", deadline=deadline))
    paths.update(nul_paths(cwd, "diff", "--cached", "--no-renames", "--name-only", "-z", deadline=deadline))
    paths.update(nul_paths(cwd, "ls-files", "--others", "--exclude-standard", "-z", deadline=deadline))
    return sorted(paths)


def path_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    return any(path == allowed or path.startswith(f"{allowed}/") for allowed in allowed_paths)


def hash_file(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise PreflightError("timed_out", "filesystem snapshot exceeded the attempt deadline")
            digest.update(chunk)
    return digest.hexdigest()


def filesystem_snapshot(root: Path, deadline: float) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for current_root, directories, files in os.walk(root, topdown=True, followlinks=False):
        if time.monotonic() >= deadline:
            raise PreflightError("timed_out", "filesystem snapshot exceeded the attempt deadline")
        current = Path(current_root)
        if current == root:
            directories[:] = [name for name in directories if name != ".git"]
        for name in sorted(directories + files):
            path = current / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            mode = metadata.st_mode & 0o7777
            if path.is_symlink():
                snapshot[relative] = f"symlink:{mode:o}:{os.readlink(path)}"
            elif path.is_file():
                snapshot[relative] = f"file:{mode:o}:{metadata.st_size}:{hash_file(path, deadline)}"
            elif path.is_dir():
                snapshot[relative] = f"dir:{mode:o}"
            else:
                snapshot[relative] = f"special:{mode:o}:{metadata.st_rdev}"
    return snapshot


def git_metadata_snapshot(common_dir: Path, deadline: float) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    candidates = [
        "HEAD",
        "config",
        "config.worktree",
        "index",
        "packed-refs",
        "shallow",
        "refs",
        "hooks",
        "info",
        "worktrees",
    ]
    for candidate in candidates:
        root = common_dir / candidate
        if not root.exists() and not root.is_symlink():
            continue
        paths = [root] if not root.is_dir() else [root, *sorted(root.rglob("*"))]
        for path in paths:
            if time.monotonic() >= deadline:
                raise PreflightError("timed_out", "Git metadata snapshot exceeded the attempt deadline")
            relative = path.relative_to(common_dir).as_posix()
            metadata = path.lstat()
            mode = metadata.st_mode & 0o7777
            if path.is_symlink():
                snapshot[relative] = f"symlink:{mode:o}:{os.readlink(path)}"
            elif path.is_file():
                snapshot[relative] = f"file:{mode:o}:{metadata.st_size}:{hash_file(path, deadline)}"
            elif path.is_dir():
                snapshot[relative] = f"dir:{mode:o}"
    return snapshot


def enforce_filesystem_scope(
    before: dict[str, str],
    after: dict[str, str],
    goal: Goal,
    run_dir: Path,
    artifact_name: str,
) -> list[str]:
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    write_private(run_dir / artifact_name, json.dumps(changed, indent=2) + "\n")
    escaped = [path for path in changed if not path_allowed(path, goal.allowed_paths)]
    if escaped:
        error = PreflightError("scope_violation", f"filesystem changes escaped goal scope: {', '.join(escaped)}")
        error.changed_paths = changed  # type: ignore[attr-defined]
        raise error
    return changed


def enforce_scope(
    cwd: Path,
    goal: Goal,
    run_dir: Path,
    artifact_name: str = "changed-paths.json",
    deadline: float | None = None,
) -> list[str]:
    paths = changed_paths(cwd, goal, deadline)
    write_private(run_dir / artifact_name, json.dumps(paths, indent=2) + "\n")
    escaped = [path for path in paths if not path_allowed(path, goal.allowed_paths)]
    if escaped:
        error = PreflightError("scope_violation", f"changed paths escaped the goal scope: {', '.join(escaped)}")
        error.changed_paths = paths  # type: ignore[attr-defined]
        raise error
    return paths


def provider_environment() -> dict[str, str]:
    return {
        name: os.environ[name] for name in ("LANG", "LC_ALL", "LC_CTYPE", "PATH", "TERM", "TZ") if os.environ.get(name)
    }


def run_verification(
    goal: Goal,
    cwd: Path,
    run_dir: Path,
    deadline: float,
    provider: Path,
    label: str,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index, command in enumerate(goal.verification_commands, start=1):
        timeout = min(float(command.timeout_seconds), remaining_seconds(deadline))
        stdout_name = f"verification-{index:03}.stdout"
        stderr_name = f"verification-{index:03}.stderr"
        runtime_command = [
            str(provider),
            "run",
            "--rm",
            "--init",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--label",
            container_filter(label),
            "--label",
            f"io.selamy.orchestrate-gemini.owner-uid={os.getuid()}",
            "--label",
            "io.selamy.orchestrate-gemini.role=verifier",
            "--volume",
            f"{cwd.resolve()}:{cwd.resolve()}:ro",
            "--workdir",
            str(cwd.resolve()),
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m",
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            goal.sandbox_image,
            *command.argv,
        ]
        try:
            result = run_process(
                runtime_command,
                cwd,
                provider_environment(),
                run_dir / stdout_name,
                run_dir / stderr_name,
                timeout,
            )
        finally:
            remove_owned_containers(provider, label, cwd, time.monotonic() + CLEANUP_GRACE_SECONDS)
        item = {
            "argv": list(command.argv),
            "container_image": goal.sandbox_image,
            "network": "none",
            "process": asdict(result),
            "returncode": result.returncode,
            "stderr": stderr_name,
            "stdout": stdout_name,
            "timed_out": result.timed_out,
            "timeout_seconds": command.timeout_seconds,
        }
        evidence.append(item)
        failed = (
            result.timed_out
            or result.interrupted_signal is not None
            or result.launch_error is not None
            or result.returncode != 0
            or result.process_group_state_after_harvest != "absent"
            or result.descendant_state_after_harvest != "absent"
        )
        if failed:
            error = PreflightError("verification_failed", f"verification command {index} failed")
            error.verification = evidence  # type: ignore[attr-defined]
            raise error
    return evidence


def prepare_attempt(args: argparse.Namespace, deadline: float, status: dict[str, Any]) -> PreparedAttempt:
    goal = parse_goal(args.goal_file)
    prompt_content = validate_prompt_source(args.prompt_file)
    write_private(args.run_dir / "prompt.txt", prompt_content)
    attempt = prior_attempt_count(args.state_dir, args.run_dir) + 1
    status.update({"attempt": attempt, "goal_id": goal.goal_id})
    if attempt > goal.max_attempts:
        raise PreflightError("attempt_exhausted", "goal attempt budget is exhausted")
    digest = persist_goal(args.state_dir, goal)
    git_state = inspect_git(
        args.cwd,
        goal,
        resume_requested=args.resume_from is not None,
        deadline=deadline,
    )
    filesystem_before = filesystem_snapshot(args.cwd, deadline)
    git_metadata_before = git_metadata_snapshot(Path(git_state.common_dir), deadline)
    live_mode = args.validation_fake_responses is None and not args.preflight_only
    environment, system_settings, gemini_home = prepare_runtime(
        args.state_dir,
        args.sandbox_provider,
        goal,
        args.cwd,
    )
    policy_digest, durable_policy = validate_policy(args.policy_file, args.run_dir, gemini_home)
    version, resolved_executable = probe_capabilities(args, environment, deadline)
    status.update(
        {
            "goal_sha256": digest,
            "git_before": asdict(git_state),
            "gemini_version": version,
            "gemini_executable": str(args.gemini),
            "gemini_executable_resolved": str(resolved_executable),
            "policy_sha256": policy_digest,
            "runtime": {
                "environment_names": sorted(environment),
                "gemini_home": environment["GEMINI_CLI_HOME"],
                "sandbox_provider": args.sandbox_provider,
                "system_settings": str(system_settings),
                "tmpdir": environment["TMPDIR"],
            },
        }
    )
    sandbox_executable = Path(probe_sandbox_provider(args, environment, deadline))
    label = lane_label(args.state_dir)
    recovered_containers = remove_owned_containers(sandbox_executable, label, args.cwd, deadline)
    (args.state_dir / "runtime-secret.env").unlink(missing_ok=True)
    attest_sandbox_image(sandbox_executable, goal.sandbox_image, args.cwd, deadline)
    environment["SANDBOX_FLAGS"] = (
        f"{SANDBOX_FLAGS} --label {container_filter(label)} --label io.selamy.orchestrate-gemini.attempt={attempt}"
    )
    if not live_mode:
        environment["SANDBOX_FLAGS"] += " --network none"
    configure_provider_guard(
        environment,
        args,
        goal,
        git_state,
        sandbox_executable,
        label,
        live_mode,
    )
    status["sandbox_executable"] = str(sandbox_executable)
    status["recovered_container_ids"] = recovered_containers
    status["sandbox_image"] = goal.sandbox_image
    lease_payload = {
        "attempt": attempt,
        "goal_id": goal.goal_id,
        "goal_sha256": digest,
        "pid": os.getpid(),
        "started_at": status["started_at"],
        "heartbeat_at": utc_now(),
    }
    write_atomic_private(args.state_dir / "lease.json", lease_payload)
    return PreparedAttempt(
        goal,
        attempt,
        digest,
        git_state,
        environment,
        durable_policy,
        lease_payload,
        resolved_executable,
        prompt_content,
        sandbox_executable,
        label,
        filesystem_before,
        git_metadata_before,
    )


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


def verify_and_record_checkout(
    args: argparse.Namespace,
    prepared: PreparedAttempt,
    deadline: float,
    status: dict[str, Any],
) -> None:
    if git_metadata_snapshot(Path(prepared.git_state.common_dir), deadline) != prepared.git_metadata_before:
        raise PreflightError("git_metadata_violation", "worker changed read-only Git control metadata")
    filesystem_after_worker = filesystem_snapshot(args.cwd, deadline)
    filesystem_changes = enforce_filesystem_scope(
        prepared.filesystem_before,
        filesystem_after_worker,
        prepared.goal,
        args.run_dir,
        "filesystem-changes.json",
    )
    enforce_scope(args.cwd, prepared.goal, args.run_dir, deadline=deadline)
    verification = run_verification(
        prepared.goal,
        args.cwd,
        args.run_dir,
        deadline,
        prepared.sandbox_executable,
        prepared.lane_label,
    )
    paths = enforce_scope(
        args.cwd,
        prepared.goal,
        args.run_dir,
        "changed-paths-after-verification.json",
        deadline,
    )
    final_filesystem_changes = enforce_filesystem_scope(
        prepared.filesystem_before,
        filesystem_snapshot(args.cwd, deadline),
        prepared.goal,
        args.run_dir,
        "filesystem-changes-after-verification.json",
    )
    if git_metadata_snapshot(Path(prepared.git_state.common_dir), deadline) != prepared.git_metadata_before:
        raise PreflightError("git_metadata_violation", "verification changed Git control metadata")
    status.update(
        {
            "changed_paths": paths,
            "filesystem_changes": final_filesystem_changes,
            "filesystem_changes_after_worker": filesystem_changes,
            "git_after": {
                "head": str(git_output(args.cwd, "rev-parse", "HEAD", deadline=deadline)).strip(),
                "status": str(git_output(args.cwd, "status", "--short", deadline=deadline)),
            },
            "verification": verification,
        }
    )


def execute_attempt(
    args: argparse.Namespace,
    prepared: PreparedAttempt,
    deadline: float,
    status_path: Path,
    status: dict[str, Any],
) -> int:
    validation = prepare_validation_responses(args, prepared)
    validation_responses = validation[0] if validation else None
    if validation:
        status["validation_fixture_sha256"] = validation[1]
    else:
        authorize_generation(prepared.goal)
        status["credential_transport"] = "owner-only-env-file-via-provider-guard"
        status["credential_env_file"] = prepare_live_credential(args, prepared)
    request_digest = execution_request_digest(args, prepared, status, validation[1] if validation else None)
    status["execution_request_sha256"] = request_digest
    resume_id = resume_session(args, request_digest, prepared.git_state, validation_responses is not None)
    staged_prompt = stage_prompt(
        prepared.git_state,
        prepared.goal,
        prepared.attempt,
        prepared.prompt_content,
    )
    command = worker_command(
        args,
        prepared.gemini_executable,
        staged_prompt,
        prepared.durable_policy,
        resume_id,
        validation_responses,
    )
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
        snapshot = process_snapshot()
        worker_info = snapshot.processes.get(worker_pid)
        if worker_info is None:
            raise PreflightError("launch_failed", "cannot capture worker process identity")
        status.update(
            {
                "classification": "running",
                "heartbeat_at": utc_now(),
                "process_group_id": process_group_id,
                "worker_pid": worker_pid,
            }
        )
        prepared.lease_payload["worker_started_at"] = worker_info.started_at
        prepared.lease_payload["worker_state"] = "running"
        write_atomic_private(status_path, status)
        update_lease(worker_pid, process_group_id)

    def output_advanced() -> None:
        now = utc_now()
        status.update({"heartbeat_at": now, "last_output_at": now})
        write_atomic_private(status_path, status)
        update_lease()

    try:
        result = run_process(
            command,
            args.cwd,
            prepared.environment,
            args.run_dir / "stdout.jsonl",
            args.run_dir / "stderr.log",
            remaining_seconds(deadline),
            on_start=worker_started,
            on_heartbeat=output_advanced,
        )
    finally:
        staged_prompt.unlink(missing_ok=True)
        removed = remove_owned_containers(
            prepared.sandbox_executable,
            prepared.lane_label,
            args.cwd,
            time.monotonic() + CLEANUP_GRACE_SECONDS,
        )
        if removed:
            status["cleaned_container_ids"] = removed
        prepared.lease_payload["worker_state"] = "absent"
        prepared.lease_payload["reconciled_at"] = utc_now()
        write_atomic_private(args.state_dir / "lease.json", prepared.lease_payload)
    status["process"] = asdict(result)
    validate_process_result(result)
    status.update(parse_stream(args.run_dir / "stdout.jsonl", args.model, validation_responses is not None))
    verify_and_record_checkout(args, prepared, deadline, status)
    status["validation_mode"] = validation_responses is not None
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
        with global_execution_lease():
            stale = remove_stale_runtime_containers(
                prepared.sandbox_executable,
                args.cwd,
                time.monotonic() + CLEANUP_GRACE_SECONDS,
            )
            if stale:
                status["globally_recovered_container_ids"] = stale
                write_atomic_private(status_path, status)
            return execute_attempt(args, prepared, deadline, status_path, status)
    except PreflightError as error:
        if hasattr(error, "changed_paths"):
            status["changed_paths"] = error.changed_paths
        if hasattr(error, "verification"):
            status["verification"] = error.verification
        return finish_status(status_path, status, error.classification, str(error))
    except SupervisorInterrupted as error:
        status["interrupted_signal"] = error.signum
        return finish_status(status_path, status, "interrupted", str(error))
    except Exception as error:  # noqa: BLE001 - terminal evidence must survive wrapper faults.
        return finish_status(status_path, status, "internal_error", f"{type(error).__name__}: {error}")
    finally:
        (args.state_dir / "runtime-secret.env").unlink(missing_ok=True)


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
    os.fchmod(lease_descriptor, 0o600)
    with os.fdopen(lease_descriptor, "r+") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return finish_status(status_path, status, "lane_locked", "another writer holds the lane lease")
        with termination_signal_handlers():
            try:
                recovered_worker = recover_stale_worker(args.state_dir)
                if recovered_worker:
                    status["recovered_worker"] = recovered_worker
                    write_atomic_private(status_path, status)
            except PreflightError as error:
                return finish_status(status_path, status, error.classification, str(error))
            return run_locked_attempt(args, deadline, status_path, status)


if __name__ == "__main__":
    raise SystemExit(main())
