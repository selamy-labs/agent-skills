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
import subprocess
import sys
import time
import tomllib
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
STANDARD_ADMIN_POLICY_DIRS = (
    Path("/etc/gemini-cli/policies"),
    Path("/Library/Application Support/GeminiCli/policies"),
)


class PreflightError(Exception):
    """A classified failure safe to expose in terminal evidence."""

    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.classification = classification


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
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--resume-from", type=lambda value, p=parser: absolute_path(p, value))
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if not args.preflight_only:
        parser.error("headless dispatch is not implemented yet")
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
    return 0 if classification == "preflight_succeeded" else 1


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


def policy_tool_names(rule: dict[str, object]) -> set[str]:
    names = rule.get("toolName")
    if isinstance(names, str):
        return {names}
    if isinstance(names, list) and all(isinstance(name, str) for name in names):
        return set(names)
    return set()


def validate_policy(path: Path, run_dir: Path) -> str:
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
    return hashlib.sha256(content).hexdigest()


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
                "networkAccess": True,
            },
        },
    }
    return (json.dumps(settings, indent=2, sort_keys=True) + "\n").encode()


def prepare_runtime(state_dir: Path, provider: str) -> tuple[dict[str, str], Path]:
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
    system_settings = state_dir / "system-settings.json"
    persist_exact_private(system_settings, build_system_settings(provider), "runtime_mismatch")
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
    return environment, system_settings


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


def probe_capabilities(args: argparse.Namespace, environment: dict[str, str], deadline: float) -> str:
    ensure_owner_controlled_file(args.gemini, "capability_probe_failed")
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
    return version


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
        try:
            goal = parse_goal(args.goal_file)
            attempt = prior_attempt_count(args.state_dir, args.run_dir) + 1
            status.update({"attempt": attempt, "goal_id": goal.goal_id})
            if attempt > goal.max_attempts:
                raise PreflightError("attempt_exhausted", "goal attempt budget is exhausted")
            digest = persist_goal(args.state_dir, goal)
            git_state = inspect_git(args.cwd, goal)
            policy_digest = validate_policy(args.policy_file, args.run_dir)
            environment, system_settings = prepare_runtime(args.state_dir, args.sandbox_provider)
            version = probe_capabilities(args, environment, deadline)
            status.update(
                {
                    "goal_sha256": digest,
                    "git_before": asdict(git_state),
                    "gemini_version": version,
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
            write_atomic_private(
                args.state_dir / "lease.json",
                {
                    "attempt": attempt,
                    "goal_id": goal.goal_id,
                    "goal_sha256": digest,
                    "pid": os.getpid(),
                    "started_at": status["started_at"],
                    "heartbeat_at": utc_now(),
                },
            )
            return finish_status(status_path, status, "preflight_succeeded")
        except PreflightError as error:
            return finish_status(status_path, status, error.classification, str(error))
        except Exception as error:  # noqa: BLE001 - terminal evidence must survive wrapper faults.
            return finish_status(status_path, status, "internal_error", f"{type(error).__name__}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
