#!/usr/bin/env python3
"""Skill and quality workflow validator."""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

BANNED_PREFIXES = frozenset(
    {
        "true",
        ":",
        "!",
        "set",
        "echo",
        "printf",
        "env",
        "command",
        "builtin",
        "nohup",
        "time",
        "nice",
        "timeout",
        "strace",
        "eval",
        "exec",
        "shopt",
        "exit",
        "return",
    }
)

SENSITIVE_TARGETS = frozenset(
    {
        "pytest",
        "ruff",
        "validate_skills.py",
        "validate_marketplace.py",
        "tf_module_layout.py",
    }
)

BANNED_SHELL_OPERATORS = ("||", "&&", ";", "|", "&", "`", "$(", ">", "<")

BANNED_ENV_PREFIXES = (
    "PYTEST",
    "COVERAGE",
    "RUFF",
    "PYTHON",
    "PIP",
    "PATH",
    "LD_",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "GIT",
)

PINNED_TOOL_REQUIREMENTS: dict[str, str] = {
    "pytest": "pytest==8.3.4",
    "pytest-cov": "pytest-cov==6.0.0",
    "ruff": "ruff==0.15.21",
}

GATED_PACKAGES = frozenset({"ruff", "pytest", "pytest-cov", "complexipy", "vulture"})
DISALLOWED_PIP_PACKAGES = frozenset({"complexipy", "vulture", "python-hcl2"})


def strip_github_expressions(text: str) -> str:
    """Remove ${{ ... }} expressions to inspect underlying commands."""
    return re.sub(r"\$\{\{.*?\}\}", "__GH_EXPR__", text)


def _check_working_directory_key(name: str, mapping: dict[str, Any]) -> list[str]:
    wd = mapping.get("working-directory") or mapping.get("working_directory")
    if wd:
        if wd == "tools/hclblocks" and mapping.get("run", "").strip() == "go vet ./...":
            return []
        return [f"{name}: disallowed working-directory override"]
    return []


def _check_defaults_run(name: str, run_defaults: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    shell = run_defaults.get("shell")
    if shell is not None and shell != "bash -eo pipefail":
        errors.append(f"{name}: disallowed defaults.run.shell override {shell!r}")
    if "working-directory" in run_defaults or "working_directory" in run_defaults:
        errors.append(f"{name}: disallowed defaults.run.working-directory override")
    return errors


def _check_defaults_configuration(name: str, mapping: dict[str, Any]) -> list[str]:
    defaults = mapping.get("defaults")
    if defaults is None:
        return []
    if not isinstance(defaults, dict):
        return [f"{name}: disallowed defaults override"]
    errors: list[str] = []
    if "working-directory" in defaults or "working_directory" in defaults:
        errors.append(f"{name}: disallowed defaults.working-directory override")
    run_defaults = defaults.get("run")
    if isinstance(run_defaults, dict):
        errors.extend(_check_defaults_run(name, run_defaults))
    return errors


def validate_shell_configuration(name: str, mapping: dict[str, Any]) -> list[str]:
    """Ensure no custom shell or working-directory overrides are present."""
    if not isinstance(mapping, dict):
        return []
    errors = _check_working_directory_key(name, mapping)
    errors.extend(_check_defaults_configuration(name, mapping))
    shell = mapping.get("shell")
    if shell is not None and shell != "bash -eo pipefail":
        errors.append(f"{name}: disallowed shell override {shell!r}")
    return errors


def validate_env_configuration(
    scope_name: str,
    mapping: dict[str, Any],
    allowed_keys: frozenset[str] = frozenset(),
) -> list[str]:
    """Ensure environment mappings do not contain bypass variables or unallowlisted keys."""
    env = mapping.get("env")
    if env is None:
        return []
    if not isinstance(env, dict):
        return [f"{scope_name}: disallowed env type {type(env).__name__}"]
    if not env:
        return []
    errors: list[str] = []
    for key in env.keys():
        k_str = str(key)
        if k_str not in allowed_keys:
            errors.append(f"{scope_name}: disallowed env variable {k_str!r}")
        elif k_str != "COVERAGE_SESSION_ID" and any(
            k_str == prefix or k_str.startswith(f"{prefix}_") for prefix in BANNED_ENV_PREFIXES
        ):
            errors.append(f"{scope_name}: banned env variable {k_str!r}")
    return errors


ALLOWED_COMMAND_STARTERS = frozenset({"python", "python3", "pytest", "ruff", "pip", "git", "gh"})


def _check_sensitive_targets(tokens: list[str], raw_line: str, errors: list[str]) -> None:
    for t in tokens:
        for target in SENSITIVE_TARGETS:
            if target in t:
                errors.append(f"Banned prefix or wrapper hiding sensitive target {target!r}: {raw_line!r}")
                return


def check_statement_tokens(tokens: list[str], raw_line: str) -> list[str]:
    """Check command tokens for banned prefixes and hidden sensitive targets."""
    errors: list[str] = []
    head = tokens[0]
    if head in BANNED_PREFIXES:
        errors.append(f"Statement starts with banned prefix {head!r}: {raw_line!r}")

    if head not in ALLOWED_COMMAND_STARTERS:
        _check_sensitive_targets(tokens[1:], raw_line, errors)
    return errors


def check_banned_prefixes_and_operators(line: str) -> list[str]:
    """Check a single command line for shell operators and banned prefixes."""
    errors: list[str] = []
    stripped = strip_github_expressions(line)
    for op in BANNED_SHELL_OPERATORS:
        if op in stripped:
            errors.append(f"Command line contains banned operator {op!r}: {line!r}")

    try:
        tokens = shlex.split(line, comments=True)
    except ValueError as exc:
        errors.append(f"Failed to parse shell tokens: {exc}")
        return errors

    if not tokens:
        return errors

    errors.extend(check_statement_tokens(tokens, line))
    return errors


def _parse_cov_fail_under(token: str) -> tuple[float | None, str | None]:
    val_str = token.split("=", 1)[1] if "=" in token else token
    try:
        return float(val_str), None
    except ValueError:
        return None, f"Invalid --cov-fail-under value: {val_str!r}"


def _process_cov_fail_under_arg(arg: str, next_arg: str | None) -> tuple[int, float | None, str | None]:
    if arg.startswith("--cov-fail-under="):
        val, err = _parse_cov_fail_under(arg)
        return (1, val, err)
    if arg == "--cov-fail-under":
        if next_arg is None:
            return (1, None, "Missing value for --cov-fail-under")
        val, err = _parse_cov_fail_under(next_arg)
        return (2, val, err)
    return (0, None, None)


def _check_pytest_arg(arg: str, next_arg: str | None) -> tuple[int, str | None, float | None, str | None]:
    """Inspect one argument or option pair.

    Returns (consumed, kind, cov_fail_under, error).
    """
    if arg in ("--cov=tools", "--cov-branch", "--cov-report=term-missing", "--cov-report=json:coverage.json"):
        return (1, arg, None, None)
    if arg in ("tests", "tests/"):
        return (1, "pos", None, None)
    if arg.startswith("--cov-fail-under"):
        consumed, val, err = _process_cov_fail_under_arg(arg, next_arg)
        return (consumed, "cov_floor", val, err)
    return (1, "disallowed", None, f"Disallowed token/flag in pytest command: {arg!r}")


def _extract_pytest_argv(tokens: list[str]) -> list[str] | None:
    if len(tokens) >= 3 and tokens[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
        return tokens[2:]
    if tokens and tokens[0] == "pytest":
        return tokens
    return None


def _check_pytest_coverage_requirements(kinds: set[str], cov_fail_under: float | None, errors: list[str]) -> None:
    if "--cov=tools" not in kinds:
        errors.append("pytest command missing '--cov=tools'")
    if "--cov-branch" not in kinds:
        errors.append("pytest command missing '--cov-branch'")
    if "--cov-report=json:coverage.json" not in kinds:
        errors.append("pytest command missing '--cov-report=json:coverage.json'")
    if cov_fail_under is None:
        errors.append("pytest command missing '--cov-fail-under'")
    elif cov_fail_under < 90:
        errors.append(f"pytest coverage floor {cov_fail_under} < 90")


def validate_pytest_command(tokens: list[str]) -> list[str]:
    """Validate pytest command tokens strictly."""
    cmd_tokens = _extract_pytest_argv(tokens)
    if not cmd_tokens:
        return [f"Expected pytest command, got {tokens}"]

    remaining = cmd_tokens[1:]
    kinds: set[str] = set()
    cov_fail_under: float | None = None
    errors: list[str] = []

    idx = 0
    while idx < len(remaining):
        next_arg = remaining[idx + 1] if idx + 1 < len(remaining) else None
        consumed, kind, floor_val, err = _check_pytest_arg(remaining[idx], next_arg)
        idx += consumed
        if err:
            errors.append(err)
        if kind:
            kinds.add(kind)
        if floor_val is not None:
            cov_fail_under = floor_val

    _check_pytest_coverage_requirements(kinds, cov_fail_under, errors)
    return errors


def validate_ruff_check_command(tokens: list[str]) -> list[str]:
    """Require ruff check to be invoked canonically with no extra flags/targets."""
    valid_invocations = [
        ["python", "-m", "ruff", "check", "."],
        ["python3", "-m", "ruff", "check", "."],
        ["ruff", "check", "."],
    ]
    if tokens not in valid_invocations:
        return [f"Disallowed ruff check invocation: {tokens}. Must be 'python -m ruff check .'"]
    return []


def validate_ruff_format_command(tokens: list[str]) -> list[str]:
    """Require ruff format to be invoked canonically with no extra flags/targets."""
    valid_prefixes = [
        ["python", "-m", "ruff", "format"],
        ["python3", "-m", "ruff", "format"],
        ["ruff", "format"],
    ]
    matched_prefix: list[str] | None = None
    for prefix in valid_prefixes:
        if tokens[: len(prefix)] == prefix:
            matched_prefix = prefix
            break

    if matched_prefix is None:
        return [f"Disallowed ruff format invocation prefix: {tokens}"]

    rest = tokens[len(matched_prefix) :]
    if rest in (["--check", "--diff", "."], ["--diff", "--check", "."]):
        return []
    return [f"Disallowed ruff format arguments: {rest}. Must be '--check --diff .'"]


def is_pip_install_tokens(tokens: list[str]) -> bool:
    """Return whether tokens represent a pip install invocation."""
    if len(tokens) >= 2 and tokens[0] in ("pip", "pip3") and tokens[1] == "install":
        return True
    return bool(
        len(tokens) >= 4
        and tokens[0] in ("python", "python3")
        and tokens[1] == "-m"
        and tokens[2] in ("pip", "pip3")
        and tokens[3] == "install"
    )


def _check_single_pip_token(token: str, errors: list[str]) -> None:
    pkg_name = re.split(r"[=<>!~]", token)[0].strip()
    if pkg_name in GATED_PACKAGES:
        expected = PINNED_TOOL_REQUIREMENTS.get(pkg_name)
        if expected is not None and token != expected:
            errors.append(f"Disallowed pip pin for {pkg_name!r}: got {token!r}, expected {expected!r}")
    if pkg_name in DISALLOWED_PIP_PACKAGES:
        errors.append(f"Disallowed pip installation of {pkg_name!r} in workflow: {token!r}")


def check_pip_install_tokens(tokens: list[str], is_primary_install_step: bool) -> list[str]:
    """Verify that pip install tokens adhere to exact pins and single-install discipline."""
    errors: list[str] = []
    if not is_pip_install_tokens(tokens):
        return errors
    if not is_primary_install_step:
        return [f"Disallowed secondary pip install step: {tokens}"]

    for token in tokens:
        _check_single_pip_token(token, errors)
    return errors


def validate_primary_pip_install_step(tokens: list[str]) -> list[str]:
    """Verify that the primary pip install step installs requirements-dev.txt."""
    if tokens == ["python", "-m", "pip", "install", "-r", "requirements-dev.txt"]:
        return []
    return [f"Primary dependency install step must be 'python -m pip install -r requirements-dev.txt', got {tokens}"]


def _find_pip_install_line(lines: list[str]) -> list[str] | None:
    for line in lines:
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        if is_pip_install_tokens(tokens):
            return tokens
    return None


def validate_dependency_install_step(step: dict[str, Any]) -> list[str]:
    """Verify that dependency install step pins ruff, pytest, and pytest-cov."""
    run_cmd = step.get("run", "")
    lines = [line.strip() for line in run_cmd.splitlines() if line.strip() and not line.strip().startswith("#")]
    tokens = _find_pip_install_line(lines)
    if tokens is None:
        return ["Dependency install step missing 'pip install' command"]
    errs = check_pip_install_tokens(tokens, is_primary_install_step=True)
    errs.extend(validate_primary_pip_install_step(tokens))
    return errs


def _check_ruff_token(tokens: list[str], gates_found: dict[str, bool], errors: list[str]) -> None:
    if "format" in tokens:
        rf_errs = validate_ruff_format_command(tokens)
        if rf_errs:
            errors.extend(rf_errs)
        else:
            gates_found["ruff_format"] = True
    elif "check" in tokens:
        rc_errs = validate_ruff_check_command(tokens)
        if rc_errs:
            errors.extend(rc_errs)
        else:
            gates_found["ruff_check"] = True


def _check_script_token(tokens: list[str], gates_found: dict[str, bool]) -> None:
    tok_tuple = tuple(tokens)
    if tok_tuple in (
        ("python", "tools/validate_skills.py"),
        ("python3", "tools/validate_skills.py"),
        ("python", "-B", "tools/validate_skills.py"),
        ("python3", "-B", "tools/validate_skills.py"),
    ):
        gates_found["validate_skills"] = True
    elif tok_tuple in (
        ("python", "tools/validate_marketplace.py"),
        ("python3", "tools/validate_marketplace.py"),
        ("python", "-B", "tools/validate_marketplace.py"),
        ("python3", "-B", "tools/validate_marketplace.py"),
    ):
        gates_found["validate_marketplace"] = True
    elif tok_tuple in (
        ("python", "tools/tf_module_layout.py", "--self-test"),
        ("python3", "tools/tf_module_layout.py", "--self-test"),
        ("python", "-B", "tools/tf_module_layout.py", "--self-test"),
        ("python3", "-B", "tools/tf_module_layout.py", "--self-test"),
    ):
        gates_found["tf_module_layout"] = True
    elif tok_tuple in (
        ("go", "vet", "./..."),
        ("go", "vet", "./tools/hclblocks/..."),
    ):
        gates_found["go_vet"] = True
    elif tok_tuple in (
        ("python", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"),
        ("python3", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"),
        ("python", "-B", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"),
        ("python3", "-B", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"),
    ):
        gates_found["unittest_discover"] = True
    elif tok_tuple in (
        ("python", "-m", "tests.test_coverage_threshold", "coverage.json"),
        ("python3", "-m", "tests.test_coverage_threshold", "coverage.json"),
        ("python", "-B", "-m", "tests.test_coverage_threshold", "coverage.json"),
        ("python3", "-B", "-m", "tests.test_coverage_threshold", "coverage.json"),
    ):
        gates_found["coverage_threshold"] = True


def check_gate_tokens(tokens: list[str], gates_found: dict[str, bool], errors: list[str]) -> None:
    """Classify tokens into one of the required gates."""
    head = tokens[0]
    if head in ("pytest", "python", "python3") and "pytest" in tokens:
        py_errs = validate_pytest_command(tokens)
        if py_errs:
            errors.extend(py_errs)
        else:
            gates_found["pytest"] = True
    elif "ruff" in tokens:
        _check_ruff_token(tokens, gates_found, errors)
    else:
        _check_script_token(tokens, gates_found)


def _handle_pip_tokens(
    tokens: list[str],
    is_primary_pip_step: bool,
    gates_found: dict[str, bool],
    errors: list[str],
) -> None:
    pip_errs = check_pip_install_tokens(tokens, is_primary_pip_step)
    if pip_errs:
        errors.extend(pip_errs)
    if not is_primary_pip_step:
        return
    prim_errs = validate_primary_pip_install_step(tokens)
    if prim_errs:
        errors.extend(prim_errs)
        return
    gates_found["pip_ruff_pin"] = True
    gates_found["pip_pytest_pin"] = True
    gates_found["pip_pytest_cov_pin"] = True


def _parse_line_tokens(line: str) -> list[str] | None:
    trimmed = line.strip()
    if not trimmed or trimmed.startswith("#"):
        return None
    try:
        return shlex.split(trimmed, comments=True)
    except ValueError:
        return None


def _process_single_run_line(
    trimmed: str,
    is_primary_pip_step: bool,
    gates_found: dict[str, bool],
    errors: list[str],
) -> None:
    line_errs = check_banned_prefixes_and_operators(trimmed)
    if line_errs:
        errors.extend(line_errs)
        return
    tokens = _parse_line_tokens(trimmed)
    if not tokens:
        return
    if is_pip_install_tokens(tokens):
        _handle_pip_tokens(tokens, is_primary_pip_step, gates_found, errors)
    else:
        check_gate_tokens(tokens, gates_found, errors)


def _process_step_run_lines(
    run_cmd: str,
    is_primary_pip_step: bool,
    gates_found: dict[str, bool],
    errors: list[str],
) -> None:
    for raw_line in run_cmd.splitlines():
        trimmed = raw_line.strip()
        if trimmed and not trimmed.startswith("#"):
            _process_single_run_line(trimmed, is_primary_pip_step, gates_found, errors)


def _step_contains_pip_install(step: Any) -> bool:
    if not isinstance(step, dict):
        return False
    run_cmd = step.get("run", "")
    for raw_line in run_cmd.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        if is_pip_install_tokens(tokens):
            return True
    return False


def _find_pip_install_steps(steps: list[Any]) -> list[dict[str, Any]]:
    return [s for s in steps if _step_contains_pip_install(s)]


def _check_step_guards(step: dict[str, Any], step_name: str, job_name: str, errors: list[str]) -> None:
    if step.get("if") is not None:
        errors.append(f"step {step_name!r} in '{job_name}' must not have an if: condition")

    coe = step.get("continue-on-error")
    if (
        coe is not None
        and coe is not False
        and str(coe).strip().lower() not in ("false", "${{ false }}", '"false"', "'false'")
    ):
        errors.append(f"step {step_name!r} in '{job_name}' must not have continue-on-error: {coe}")


def _get_verify_step_allowed_env(job_name: str, step_name: str, run_cmd: str) -> frozenset[str]:
    if job_name != "verify":
        return frozenset()
    if "tools/release_gate.py" in run_cmd or step_name == "Enforce Release Gate":
        return frozenset({"GH_TOKEN", "GITHUB_TOKEN"})
    if "go vet" in run_cmd:
        return frozenset({"GOTOOLCHAIN", "GOWORK", "GOFLAGS"})
    if "pytest" in run_cmd:
        return frozenset({"COVERAGE_SESSION_ID"})
    return frozenset()


def _get_tests_step_allowed_env(run_raw_val: Any) -> frozenset[str]:
    if not run_raw_val:
        return frozenset()
    run_str = str(run_raw_val)
    if "go vet" in run_str:
        return frozenset({"GOTOOLCHAIN", "GOWORK", "GOFLAGS"})
    if any(k in run_str for k in ("pytest", "test_coverage_threshold")):
        return frozenset({"COVERAGE_SESSION_ID"})
    return frozenset()


REQUIRED_GO_VET_ENV: dict[str, str] = {
    "GOTOOLCHAIN": "local",
    "GOWORK": "off",
    "GOFLAGS": "-mod=readonly",
}


def _check_go_vet_step(step: dict[str, Any], step_name: str, job_name: str, errors: list[str]) -> None:
    wd = step.get("working-directory") or step.get("working_directory")
    if wd != "tools/hclblocks":
        errors.append(f"step {step_name!r} in '{job_name}' go vet must specify working-directory: 'tools/hclblocks'")
    step_env = step.get("env")
    if step_env != REQUIRED_GO_VET_ENV:
        errors.append(
            f"step {step_name!r} in '{job_name}' go vet env must be exactly {REQUIRED_GO_VET_ENV!r}, got: {step_env!r}"
        )


def _inspect_job_step(
    step: Any,
    job_name: str,
    primary_pip_step: dict[str, Any] | None,
    gates_found: dict[str, bool],
    errors: list[str],
) -> None:
    if not isinstance(step, dict):
        return
    step_name: str = str(step.get("name") or step.get("id") or "unnamed step")
    errors.extend(validate_shell_configuration(f"step {step_name!r} in '{job_name}'", step))
    _check_step_guards(step, step_name, job_name, errors)

    run_cmd = step.get("run", "")
    allowed_env = _get_verify_step_allowed_env(job_name, step_name, run_cmd)
    errors.extend(validate_env_configuration(f"step {step_name!r} in '{job_name}'", step, allowed_keys=allowed_env))

    if not run_cmd:
        return

    if "go vet" in run_cmd:
        _check_go_vet_step(step, step_name, job_name, errors)

    is_primary = step is primary_pip_step
    _process_step_run_lines(run_cmd, is_primary, gates_found, errors)
    _check_step_coverage_command_order(run_cmd, step_name, job_name, errors)


def _is_pinned_action(uses_val: Any, action_name: str) -> bool:
    if not isinstance(uses_val, str):
        return False
    val = uses_val.strip()
    return bool(re.fullmatch(rf"{re.escape(action_name)}@([0-9a-fA-F]{{40}})", val))


QUALITY_JOB_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"actions/checkout@v7.0.0", "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"}
)

ALLOWED_USES_ACTIONS: frozenset[str] = frozenset(
    {
        "actions/checkout@v7.0.0",
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
        "actions/setup-go@d35c59abb061a4a6fb18e82ac0862c26744d6ab5",
    }
)

CANONICAL_QUALITY_RUN_ARGVS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("python", "-m", "pip", "install", "-r", "requirements-dev.txt"),
        ("python", "scripts/lint_skills.py"),
        ("python", "scripts/privacy_scan.py"),
        ("python", "scripts/test_privacy_scan.py"),
        ("python", "scripts/security_scan.py"),
        ("python", "scripts/test_security_scan.py"),
        ("python", "scripts/public_api_stability.py"),
        ("python", "scripts/test_pr_workflow_cancellation.py"),
        ("python", "scripts/test_semantic_release.py"),
        ("python", "scripts/validate_pr_title.py", "--title", "$PR_TITLE", "--body", "$PR_BODY"),
    }
)

CANONICAL_TESTS_RUN_SEQUENCE: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "pip", "install", "-r", "requirements-dev.txt"),
    ("go", "vet", "./..."),
    ("python", "-m", "ruff", "check", "."),
    ("python", "-m", "ruff", "format", "--check", "--diff", "."),
    ("python", "tools/validate_skills.py"),
    ("python", "tools/validate_marketplace.py"),
    ("python", "tools/tf_module_layout.py", "--self-test"),
    ("python", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"),
    (
        "pytest",
        "--cov=tools",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=json:coverage.json",
        "--cov-fail-under=90",
    ),
    ("python", "-m", "tests.test_coverage_threshold", "coverage.json"),
)

CANONICAL_TESTS_RUN_ARGVS: frozenset[tuple[str, ...]] = frozenset(CANONICAL_TESTS_RUN_SEQUENCE)


class OrderedArgvSet(list[tuple[str, ...]]):
    def add(self, item: tuple[str, ...]) -> None:
        self.append(item)


def is_tracked_by_git_status(path: Path | str) -> tuple[bool, str | None]:
    """Return (is_tracked, failure_detail). Fails closed (True) on git or process errors."""
    target = Path(path)
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(target)],
            capture_output=True,
            text=True,
            check=False,
            cwd=target.parent if target.parent.is_dir() else None,
        )
        if proc.returncode == 0:
            return True, None
        if proc.returncode == 1:
            return False, None
        return True, f"cannot determine git tracking (git exit {proc.returncode})"
    except (FileNotFoundError, OSError) as exc:
        return True, f"cannot determine git tracking ({exc})"


def is_tracked_by_git(path: Path | str) -> bool:
    """Return True if path is tracked by git (fails closed on fatal git errors or process errors)."""
    tracked, _ = is_tracked_by_git_status(path)
    return tracked


def check_no_tracked_coverage_json(repo_root: Path) -> list[str]:
    """Ensure coverage.json is not tracked in git."""
    errors: list[str] = []
    for filename in ("coverage.json", ".coverage_session_start"):
        target_path = repo_root / filename
        tracked, detail = is_tracked_by_git_status(target_path)
        if is_tracked_by_git(target_path) or tracked:
            if detail:
                errors.append(f"{filename}: {detail}")
            else:
                errors.append(f"{filename} must not be tracked in git repository")
    return errors


def _validate_tests_step_order(run_argvs_order: list[tuple[str, ...]], job_name: str, errors: list[str]) -> None:
    expected_order = list(CANONICAL_TESTS_RUN_SEQUENCE)
    if run_argvs_order != expected_order:
        errors.append(f"job '{job_name}' steps out of order: expected canonical step sequence")
    pytest_idx = -1
    verify_idx = -1
    for idx, cmd in enumerate(run_argvs_order):
        if cmd and cmd[0] == "pytest":
            pytest_idx = idx
        elif len(cmd) >= 3 and cmd[:3] == ("python", "-m", "tests.test_coverage_threshold"):
            verify_idx = idx

    if pytest_idx != -1 and verify_idx != -1 and verify_idx != pytest_idx + 1:
        errors.append(
            f"job '{job_name}' step 'Verify coverage threshold' must immediately follow 'Run tests with coverage'"
        )


def _is_coverage_threshold_tokens(tokens: list[str]) -> bool:
    if len(tokens) < 3:
        return False
    return tokens[:3] in (
        ["python", "-m", "tests.test_coverage_threshold"],
        ["python3", "-m", "tests.test_coverage_threshold"],
        ["python", "-B", "-m", "tests.test_coverage_threshold"],
        ["python3", "-B", "-m", "tests.test_coverage_threshold"],
    )


is_coverage_check_tokens = _is_coverage_threshold_tokens


def _find_coverage_command_indices(lines: list[str]) -> tuple[int, int]:
    pytest_idx = -1
    verify_idx = -1
    for idx, line in enumerate(lines):
        tokens = _line_tokens(line)
        if tokens and tokens[0] == "pytest":
            pytest_idx = idx
        elif _is_coverage_threshold_tokens(tokens):
            verify_idx = idx
    return pytest_idx, verify_idx


def _check_step_coverage_command_order(run_cmd: str, step_name: str, job_name: str, errors: list[str]) -> None:
    lines = [line.strip() for line in run_cmd.splitlines() if line.strip() and not line.strip().startswith("#")]
    p_idx, v_idx = _find_coverage_command_indices(lines)
    if p_idx == -1 or v_idx == -1:
        return
    if v_idx != p_idx + 1:
        errors.append(f"step {step_name!r} in '{job_name}': coverage threshold check must immediately follow pytest")


def _validate_setup_go_inputs(step_with: Any, uses: str, step_name: str, job_name: str, errors: list[str]) -> None:
    if not _is_pinned_action(uses, "actions/setup-go"):
        errors.append(
            f"step {step_name!r} in '{job_name}' actions/setup-go must be pinned by commit SHA, got: {uses!r}"
        )
    if step_with != {"go-version": "1.26.10"}:
        errors.append(
            f"step {step_name!r} in '{job_name}' actions/setup-go 'with:' must be exactly "
            f"{{'go-version': '1.26.10'}}, got: {step_with!r}"
        )


def _validate_uses_step_inputs(
    step: dict[str, Any],
    uses: str,
    step_name: str,
    job_name: str,
    errors: list[str],
) -> None:
    step_with = step.get("with")
    if uses == "actions/checkout@v7.0.0":
        if step_with is not None:
            errors.append(f"step {step_name!r} in '{job_name}' disallowed 'with:' on actions/checkout: {step_with!r}")
    elif uses == "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" or _is_pinned_action(
        uses, "actions/setup-python"
    ):
        if step_with != {"python-version": "3.12"}:
            errors.append(
                f"step {step_name!r} in '{job_name}' actions/setup-python 'with:' must be exactly "
                f"{{'python-version': '3.12'}}, got: {step_with!r}"
            )
    elif uses.startswith("actions/setup-go@") or _is_pinned_action(uses, "actions/setup-go"):
        _validate_setup_go_inputs(step_with, uses, step_name, job_name, errors)


def _check_uses_action(
    step: dict[str, Any],
    step_name: str,
    job_name: str,
    uses: str,
    uses_seen: set[str],
    errors: list[str],
) -> None:
    is_allowed = (
        uses in ALLOWED_USES_ACTIONS
        or uses.startswith("actions/setup-go@")
        or _is_pinned_action(uses, "actions/setup-go")
    )
    if not is_allowed:
        errors.append(f"step {step_name!r} in '{job_name}' unrecognized uses action: {uses!r}")
    elif uses in uses_seen:
        errors.append(f"step {step_name!r} in '{job_name}' duplicate uses step: {uses!r}")
    else:
        uses_seen.add(uses)
        _validate_uses_step_inputs(step, uses, step_name, job_name, errors)


def _check_step_action_or_run(
    step: dict[str, Any],
    step_name: str,
    job_name: str,
    uses: str | None,
    run_cmd: str | None,
    uses_seen: set[str],
    errors: list[str],
) -> bool:
    if uses is not None and run_cmd is not None:
        errors.append(f"step {step_name!r} in '{job_name}' cannot specify both 'uses' and 'run'")
        return False
    if uses is None and run_cmd is None:
        errors.append(f"step {step_name!r} in '{job_name}' must specify 'uses' or 'run'")
        return False
    if uses is not None:
        _check_uses_action(step, step_name, job_name, uses, uses_seen, errors)
        return False
    return True


def _check_canonical_run_step(
    step_name: str,
    job_name: str,
    run_cmd: str,
    allowed_argvs: frozenset[tuple[str, ...]],
    run_argvs_seen: OrderedArgvSet | set[tuple[str, ...]],
    errors: list[str],
) -> tuple[str, ...] | None:
    line_errs = check_banned_prefixes_and_operators(run_cmd)
    if line_errs:
        errors.extend(line_errs)
        return None
    active_lines = [line.strip() for line in run_cmd.splitlines() if line.strip() and not line.strip().startswith("#")]
    if len(active_lines) != 1:
        errors.append(f"step {step_name!r} in '{job_name}' must have exactly 1 command line, got {len(active_lines)}")
        return None
    cmd_tokens = _line_tokens(active_lines[0])
    cmd_tuple = tuple(cmd_tokens)
    if cmd_tuple not in allowed_argvs:
        errors.append(f"step {step_name!r} in '{job_name}' unrecognized run command: {active_lines[0]!r}")
        return None
    if cmd_tuple in run_argvs_seen:
        errors.append(f"step {step_name!r} in '{job_name}' duplicate run command: {active_lines[0]!r}")
        return None
    run_argvs_seen.add(cmd_tuple)
    return cmd_tuple


def _inspect_tests_step(
    step: Any,
    job_name: str,
    uses_seen: set[str],
    run_argvs_seen: OrderedArgvSet | set[tuple[str, ...]],
    primary_pip_step: dict[str, Any] | None,
    gates_found: dict[str, bool],
    errors: list[str],
) -> None:
    if not isinstance(step, dict):
        errors.append(f"job '{job_name}' step must be a mapping")
        return
    raw_name = step.get("name") or step.get("id")
    step_name = str(raw_name) if raw_name is not None else "unnamed step"
    errors.extend(validate_shell_configuration(f"step {step_name!r} in '{job_name}'", step))
    allowed_env = _get_tests_step_allowed_env(step.get("run"))
    errors.extend(validate_env_configuration(f"step {step_name!r} in '{job_name}'", step, allowed_keys=allowed_env))
    _check_step_guards(step, step_name, job_name, errors)

    uses_raw = step.get("uses")
    uses: str | None = str(uses_raw) if uses_raw is not None else None
    run_raw = step.get("run")
    run_cmd: str | None = str(run_raw) if run_raw is not None else None
    if not _check_step_action_or_run(step, step_name, job_name, uses, run_cmd, uses_seen, errors):
        return

    assert run_cmd is not None
    if "go vet" in run_cmd:
        _check_go_vet_step(step, step_name, job_name, errors)
    _check_canonical_run_step(step_name, job_name, run_cmd, CANONICAL_TESTS_RUN_ARGVS, run_argvs_seen, errors)
    is_primary = step is primary_pip_step
    _process_step_run_lines(run_cmd, is_primary, gates_found, errors)
    _check_step_coverage_command_order(run_cmd, step_name, job_name, errors)


def _check_missing_uses_actions(uses_seen: set[str], job_name: str, errors: list[str]) -> None:
    for action in ALLOWED_USES_ACTIONS:
        if action.startswith("actions/setup-go@"):
            if not any(_is_pinned_action(u, "actions/setup-go") for u in uses_seen):
                errors.append(f"job '{job_name}' missing required action: actions/setup-go (pinned by commit SHA)")
        elif action not in uses_seen:
            errors.append(f"job '{job_name}' missing required action: {action}")


def _validate_tests_job_steps(
    steps: list[Any],
    job_name: str,
    primary_pip_step: dict[str, Any] | None,
    gates_found: dict[str, bool],
    errors: list[str],
) -> None:
    uses_seen: set[str] = set()
    run_argvs_seen = OrderedArgvSet()
    for step in steps:
        _inspect_tests_step(step, job_name, uses_seen, run_argvs_seen, primary_pip_step, gates_found, errors)

    _check_missing_uses_actions(uses_seen, job_name, errors)
    for cmd_tuple in CANONICAL_TESTS_RUN_ARGVS:
        if cmd_tuple not in run_argvs_seen:
            errors.append(f"job '{job_name}' missing required canonical run command: {' '.join(cmd_tuple)}")

    _validate_tests_step_order(list(run_argvs_seen), job_name, errors)


def _check_missing_gates(gates_found: dict[str, bool], job_name: str, errors: list[str]) -> None:
    for gate_name, found in gates_found.items():
        if not found:
            errors.append(f"job '{job_name}' missing required gate: {gate_name}")


ALLOWED_JOB_LEVEL_KEYS: frozenset[str] = frozenset({"name", "runs-on", "steps", "permissions", "timeout-minutes"})


def validate_job_level_keys(job: dict[str, Any], job_name: str) -> list[str]:
    """Verify closed allowlist of job-level keys, runs-on label, timeout range, and permissions."""
    errors: list[str] = []
    disallowed = sorted(set(job.keys()) - ALLOWED_JOB_LEVEL_KEYS)
    if disallowed:
        errors.append(f"job '{job_name}' contains disallowed job-level keys: {', '.join(disallowed)}")
    runs_on = job.get("runs-on")
    if runs_on != "ubuntu-latest":
        errors.append(f"job '{job_name}' runs-on must be 'ubuntu-latest', got: {runs_on!r}")
    if "timeout-minutes" in job:
        timeout = job.get("timeout-minutes")
        if not (isinstance(timeout, int) and not isinstance(timeout, bool) and 5 <= timeout <= 60):
            errors.append(f"job '{job_name}' timeout-minutes must be an integer between 5 and 60, got: {timeout!r}")
    if "permissions" in job:
        perms = job.get("permissions")
        if perms != {"contents": "read"}:
            errors.append(f"job '{job_name}' permissions must be {{'contents': 'read'}}, got: {perms!r}")
    return errors


def _check_job_coe(job: dict[str, Any], job_name: str, errors: list[str]) -> None:
    job_coe = job.get("continue-on-error")
    if (
        job_coe is not None
        and job_coe is not False
        and str(job_coe).strip().lower() not in ("false", "${{ false }}", '"false"', "'false'")
    ):
        errors.append(f"job '{job_name}' must not have continue-on-error: {job_coe}")


def _init_gates_found(job_name: str) -> dict[str, bool]:
    gates_found = {
        "pip_ruff_pin": False,
        "pip_pytest_pin": False,
        "pip_pytest_cov_pin": False,
        "go_vet": False,
        "ruff_check": False,
        "ruff_format": False,
        "pytest": False,
        "validate_skills": False,
        "validate_marketplace": False,
        "tf_module_layout": False,
    }
    if job_name in ("tests", "test"):
        gates_found["unittest_discover"] = False
        gates_found["coverage_threshold"] = False
    return gates_found


def _check_verify_job_steps(steps: list[Any], errors: list[str]) -> None:
    setup_go_steps = [s for s in steps if isinstance(s, dict) and _is_pinned_action(s.get("uses"), "actions/setup-go")]
    if not setup_go_steps:
        errors.append("job 'verify' must include pinned actions/setup-go")
    elif setup_go_steps[0].get("with") != {"go-version": "1.26.10"}:
        errors.append("job 'verify' actions/setup-go must specify go-version: '1.26.10'")


def _check_deprecated_install_step(steps: list[Any], job_name: str, errors: list[str]) -> None:
    for s in steps:
        if isinstance(s, dict) and "scripts/install_hcl_parser.py" in str(s.get("run", "")):
            errors.append(f"job '{job_name}' contains deprecated install_hcl_parser step")


def _check_verify_job_permissions(job: dict[str, Any], errors: list[str]) -> None:
    perms = job.get("permissions")
    if perms != {"contents": "read", "checks": "read"}:
        errors.append(f"job 'verify' permissions must be {{'contents': 'read', 'checks': 'read'}}, got: {perms!r}")


def _check_release_job_permissions(job: dict[str, Any], errors: list[str]) -> None:
    perms = job.get("permissions")
    if perms != {"contents": "write"}:
        errors.append(f"job 'release' permissions must be {{'contents': 'write'}}, got: {perms!r}")
    runs_on = job.get("runs-on")
    if runs_on != "ubuntu-latest":
        errors.append(f"job 'release' runs-on must be 'ubuntu-latest', got: {runs_on!r}")
    if "container" in job:
        errors.append("job 'release' must not declare a container")
    if "services" in job:
        errors.append("job 'release' must not declare services")


def _validate_job_header_and_permissions(job: dict[str, Any], job_name: str, errors: list[str]) -> None:
    if job_name in ("tests", "test"):
        errors.extend(validate_job_level_keys(job, job_name))
    elif job_name == "verify":
        _check_verify_job_permissions(job, errors)
    elif job_name == "release":
        _check_release_job_permissions(job, errors)
    _check_job_coe(job, job_name, errors)


def validate_job_gates(job: dict[str, Any], job_name: str) -> list[str]:
    """Verify that target job contains all gates + pins and no banned prefixes or bypasses."""
    errors: list[str] = list(validate_shell_configuration(f"job '{job_name}'", job))
    errors.extend(validate_env_configuration(f"job '{job_name}'", job, allowed_keys=frozenset()))
    _validate_job_header_and_permissions(job, job_name, errors)

    steps = job.get("steps", [])
    if not isinstance(steps, list):
        return [f"job '{job_name}' steps must be a list"]

    gates_found = _init_gates_found(job_name)

    pip_steps = _find_pip_install_steps(steps)
    if len(pip_steps) > 1:
        errors.append(f"job '{job_name}' has {len(pip_steps)} pip install steps; at most 1 allowed")
    primary_pip_step = pip_steps[0] if pip_steps else None

    if job_name in ("tests", "test"):
        _validate_tests_job_steps(steps, job_name, primary_pip_step, gates_found, errors)
    else:
        for step in steps:
            _inspect_job_step(step, job_name, primary_pip_step, gates_found, errors)

    if job_name == "verify":
        _check_verify_job_steps(steps, errors)

    _check_deprecated_install_step(steps, job_name, errors)
    _check_missing_gates(gates_found, job_name, errors)
    return errors


MANDATORY_QUALITY_JOB_SCRIPTS = (
    "lint_skills.py",
    "privacy_scan.py",
    "test_privacy_scan.py",
    "security_scan.py",
    "test_security_scan.py",
    "public_api_stability.py",
    "test_pr_workflow_cancellation.py",
    "test_semantic_release.py",
)

CANONICAL_QUALITY_JOB_COMMANDS: dict[tuple[str, ...], str] = {
    cmd: script
    for script in MANDATORY_QUALITY_JOB_SCRIPTS
    for cmd in (
        ("python", f"scripts/{script}"),
        ("python3", f"scripts/{script}"),
        ("python", "-B", f"scripts/{script}"),
        ("python3", "-B", f"scripts/{script}"),
    )
}


def _line_tokens(raw_line: str) -> list[str]:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return []
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return []


def _is_valid_script_invocation(run_cmd: str, script_name: str) -> bool:
    """Return True if run_cmd contains a valid canonical invocation of script_name."""
    for raw_line in run_cmd.splitlines():
        tokens = _line_tokens(raw_line)
        if CANONICAL_QUALITY_JOB_COMMANDS.get(tuple(tokens)) == script_name:
            return True
    return False


def _is_pr_title_tokens(tokens: list[str]) -> bool:
    if len(tokens) < 2 or tokens[0] not in ("python", "python3"):
        return False
    target = tokens[2] if len(tokens) > 2 and tokens[1] == "-B" else tokens[1]
    return target in ("scripts/validate_pr_title.py", "validate_pr_title.py")


def _invokes_pr_title_script(run_cmd: str) -> bool:
    """Return True if run_cmd invokes scripts/validate_pr_title.py."""
    for raw_line in run_cmd.splitlines():
        tokens = _line_tokens(raw_line)
        if _is_pr_title_tokens(tokens):
            return True
    return False


def _is_pr_title_step(step_name: str, run_cmd: str) -> bool:
    """Return True if step exclusively validates PR title without touching mandatory scripts."""
    if any(s in run_cmd or s in step_name for s in MANDATORY_QUALITY_JOB_SCRIPTS):
        return False
    return _invokes_pr_title_script(run_cmd)


def _check_pr_title_if(step_if: Any, step_name: str, job_name: str, errors: list[str]) -> None:
    if step_if is None:
        return
    step_if_str = str(step_if).strip()
    if step_if is False or step_if_str.lower() in ("false", "${{ false }}", '"false"', "'false'"):
        errors.append(f"step {step_name!r} in '{job_name}' must not have if: false")
    elif "pull_request" not in step_if_str:
        errors.append(f"step {step_name!r} in '{job_name}' if: condition must reference pull_request")


def _check_step_coe_guard(step: dict[str, Any], step_name: str, job_name: str, errors: list[str]) -> None:
    coe = step.get("continue-on-error")
    if (
        coe is not None
        and coe is not False
        and str(coe).strip().lower() not in ("false", "${{ false }}", '"false"', "'false'")
    ):
        errors.append(f"step {step_name!r} in '{job_name}' must not have continue-on-error: {coe}")


def _check_quality_step_conditions(
    step: dict[str, Any],
    step_name: str,
    job_name: str,
    run_cmd: str,
    errors: list[str],
) -> None:
    is_pr_title = _is_pr_title_step(step_name, run_cmd)
    step_if = step.get("if")
    if is_pr_title:
        _check_pr_title_if(step_if, step_name, job_name, errors)
    elif step_if is not None:
        errors.append(f"step {step_name!r} in '{job_name}' must not have an if: condition")

    _check_step_coe_guard(step, step_name, job_name, errors)


def _record_mandatory_scripts(
    step: dict[str, Any],
    run_cmd: str,
    scripts_found: dict[str, bool],
) -> None:
    step_if = step.get("if")
    coe = step.get("continue-on-error")
    coe_active = (
        coe is not None
        and coe is not False
        and str(coe).strip().lower() not in ("false", "${{ false }}", '"false"', "'false'")
    )
    if step_if is not None or coe_active:
        return
    for s in scripts_found:
        if _is_valid_script_invocation(run_cmd, s):
            scripts_found[s] = True


def _inspect_quality_step(
    step: Any,
    job_name: str,
    uses_seen: set[str],
    run_argvs_seen: set[tuple[str, ...]],
    scripts_found: dict[str, bool],
    errors: list[str],
) -> None:
    if not isinstance(step, dict):
        errors.append(f"job '{job_name}' step must be a mapping")
        return
    raw_name = step.get("name") or step.get("id")
    step_name = str(raw_name) if raw_name is not None else "unnamed step"
    errors.extend(validate_shell_configuration(f"step {step_name!r} in '{job_name}'", step))
    uses_raw = step.get("uses")
    uses: str | None = str(uses_raw) if uses_raw is not None else None
    run_raw = step.get("run")
    run_cmd: str | None = str(run_raw) if run_raw is not None else None
    if not _check_step_action_or_run(step, step_name, job_name, uses, run_cmd, uses_seen, errors):
        return

    assert run_cmd is not None
    is_pr_title = _is_pr_title_step(step_name, run_cmd)
    allowed_env = frozenset({"PR_TITLE", "PR_BODY"}) if is_pr_title else frozenset()
    errors.extend(validate_env_configuration(f"step {step_name!r} in '{job_name}'", step, allowed_keys=allowed_env))

    _check_quality_step_conditions(step, step_name, job_name, run_cmd, errors)

    cmd_tuple = _check_canonical_run_step(
        step_name, job_name, run_cmd, CANONICAL_QUALITY_RUN_ARGVS, run_argvs_seen, errors
    )
    if cmd_tuple is not None:
        _record_mandatory_scripts(step, run_cmd, scripts_found)


def _check_quality_job_completeness(
    uses_seen: set[str],
    run_argvs_seen: set[tuple[str, ...]],
    scripts_found: dict[str, bool],
    job_name: str,
    errors: list[str],
) -> None:
    for action in QUALITY_JOB_ALLOWED_ACTIONS:
        if action not in uses_seen:
            errors.append(f"job '{job_name}' missing required action: {action}")

    for script, found in scripts_found.items():
        if not found:
            errors.append(f"job '{job_name}' missing required step: {script}")
            errors.append(f"job '{job_name}' missing required script: {script}")

    for cmd_tuple in CANONICAL_QUALITY_RUN_ARGVS:
        if cmd_tuple not in run_argvs_seen:
            errors.append(f"job '{job_name}' missing required canonical run command: {' '.join(cmd_tuple)}")


def validate_quality_job(job: dict[str, Any], job_name: str = "quality") -> list[str]:
    """Verify that quality job contains all mandatory scripts and unconditional steps."""
    errors: list[str] = list(validate_shell_configuration(f"job '{job_name}'", job))
    errors.extend(validate_env_configuration(f"job '{job_name}'", job, allowed_keys=frozenset()))
    errors.extend(validate_job_level_keys(job, job_name))

    job_coe = job.get("continue-on-error")
    if (
        job_coe is not None
        and job_coe is not False
        and str(job_coe).strip().lower() not in ("false", "${{ false }}", '"false"', "'false'")
    ):
        errors.append(f"job '{job_name}' must not have continue-on-error: {job_coe}")

    steps = job.get("steps", [])
    if not isinstance(steps, list):
        return [f"job '{job_name}' steps must be a list"]

    uses_seen: set[str] = set()
    run_argvs_seen: set[tuple[str, ...]] = set()
    scripts_found = {s: False for s in MANDATORY_QUALITY_JOB_SCRIPTS}
    for step in steps:
        _inspect_quality_step(step, job_name, uses_seen, run_argvs_seen, scripts_found, errors)

    _check_quality_job_completeness(uses_seen, run_argvs_seen, scripts_found, job_name, errors)
    return errors


def _load_workflow_mapping(workflow_input: dict[str, Any] | str | Path) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(workflow_input, dict):
        return (workflow_input, [])
    p = Path(workflow_input)
    content = p.read_text(encoding="utf-8") if p.is_file() else str(workflow_input)
    try:
        doc = yaml.safe_load(content)
        if not isinstance(doc, dict):
            return (None, ["Quality workflow must be a mapping"])
        return (doc, [])
    except Exception as exc:
        return (None, [f"Failed to parse YAML: {exc}"])


def _check_quality_job_ifs(jobs: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for jname in ("quality", "tests", "test"):
        job = jobs.get(jname)
        if isinstance(job, dict) and job.get("if") is not None:
            errors.append(f"required job '{jname}' in quality workflow must not declare an if: condition")
    return errors


def _validate_quality_workflow_jobs(jobs: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    quality_job = jobs.get("quality")
    tests_job = jobs.get("tests") or jobs.get("test")

    if not isinstance(quality_job, dict):
        errors.append("quality workflow missing target tests/quality job: 'quality' job must be a mapping")
    else:
        errors.extend(validate_quality_job(quality_job, "quality"))

    if not isinstance(tests_job, dict):
        errors.append("quality workflow missing target tests/quality job: 'tests' job must be a mapping")
    else:
        target_name = "tests" if "tests" in jobs else "test"
        errors.extend(validate_job_gates(tests_job, target_name))

    return errors


CANONICAL_INSTALL_CMD = "python -m pip install -r requirements-dev.txt"

LOCAL_TOP_LEVEL: frozenset[str] = frozenset({"scripts", "skills", "tests", "tools"})

MODULE_TO_DIST: dict[str, str] = {
    "yaml": "pyyaml",
    "pytest": "pytest",
    "_pytest": "pytest",
    "pytest_cov": "pytest-cov",
    "ruff": "ruff",
}

EXPECTED_DEV_REQUIREMENTS: frozenset[str] = frozenset(
    {
        "pytest==8.3.4",
        "pytest-cov==6.0.0",
        "pyyaml==6.0.1",
        "ruff==0.15.21",
    }
)

PRIVILEGED_JOB_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        (".github/workflows/release.yaml", "release"),
    }
)

PRIVILEGED_REUSABLE_JOB_ALLOWLIST: frozenset[tuple[str, str, str]] = frozenset(
    {
        (
            ".github/workflows/diagram-freshness.yml",
            "diagram-freshness",
            "selamy-labs/.github/.github/workflows/diagram-freshness.yml@main",
        ),
        (
            ".github/workflows/secret-scan.yml",
            "secret-scan",
            "selamy-labs/.github/.github/workflows/secret-scan.yml@main",
        ),
        (
            ".github/workflows/vulture-check.yml",
            "vulture-check",
            "selamy-labs/.github/.github/workflows/vulture-check.yml@main",
        ),
    }
)

PRIVILEGED_JOB_ALLOWED_SCRIPTS: frozenset[str] = frozenset({"scripts/semantic_release.py"})

PRIVILEGED_SCRIPT_ALLOWED_LOCAL_MODULES: frozenset[str] = frozenset({"tools.release_gate"})

ALLOWED_POST_INSTALL_USES: frozenset[str] = frozenset(
    {
        "actions/setup-go@d35c59abb061a4a6fb18e82ac0862c26744d6ab5",
    }
)

KNOWN_GITHUB_PERMISSION_SCOPES: frozenset[str] = frozenset(
    {
        "actions",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
    }
)


def _check_req_dev_line(line: str) -> str | None:
    """Validate a single non-comment line in requirements-dev.txt."""
    if line.startswith("-"):
        return f"disallowed option or flag in requirements-dev.txt: {line!r}"
    if "==" not in line:
        return f"requirements-dev.txt: requirement must use exact '==' pin: {line!r}"
    if line not in EXPECTED_DEV_REQUIREMENTS:
        return f"requirements-dev.txt: unexpected or drifted requirement: {line!r}"
    return None


def validate_requirements_dev_content(content: str) -> list[str]:
    """Validate requirements-dev.txt content matches EXPECTED_DEV_REQUIREMENTS exactly."""
    errors: list[str] = []
    seen: set[str] = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        err = _check_req_dev_line(line)
        if err is not None:
            errors.append(err)
        else:
            seen.add(line)
    for req in sorted(EXPECTED_DEV_REQUIREMENTS - seen):
        errors.append(f"requirements-dev.txt: missing expected requirement: {req!r}")
    return errors


def validate_requirements_dev_file(path: Path) -> list[str]:
    """Validate requirements-dev.txt file against exact pins."""
    if not path.is_file():
        return [f"requirements-dev.txt not found at {path}"]
    return validate_requirements_dev_content(path.read_text(encoding="utf-8"))


def _check_install_step_guards(step: dict[str, Any], job_name: str) -> list[str]:
    """Ensure canonical install step has no conditional, shell, directory, or coe overrides."""
    errors: list[str] = []
    if step.get("if") is not None:
        errors.append(f"job '{job_name}' canonical install step must not have an 'if:' condition")
    if step.get("working-directory") or step.get("working_directory"):
        errors.append(f"job '{job_name}' canonical install step must not have 'working-directory'")
    if step.get("shell") is not None:
        errors.append(f"job '{job_name}' canonical install step must not have 'shell'")
    coe = step.get("continue-on-error")
    if (
        coe is not None
        and coe is not False
        and str(coe).strip().lower() not in ("false", "${{ false }}", '"false"', "'false'")
    ):
        errors.append(f"job '{job_name}' canonical install step must not have 'continue-on-error'")
    if step.get("env"):
        errors.append(f"job '{job_name}' canonical install step must not declare 'env'")
    return errors


def _is_effective_setup_python(step: dict[str, Any]) -> bool:
    """Check if step is an unconditional actions/setup-python pinned by 40-hex SHA."""
    uses = step.get("uses")
    if not isinstance(uses, str):
        return False
    if step.get("if") is not None or step.get("continue-on-error") is not None:
        return False
    clean_uses = uses.split("#")[0].strip()
    return bool(re.fullmatch(r"^actions/setup-python@[0-9a-fA-F]{40}$", clean_uses))


def _check_pre_install_step(step: dict[str, Any], job_name: str) -> list[str]:
    """Ensure pre-install actions are restricted to checkout, setup-python, or setup-go."""
    uses = step.get("uses")
    if not isinstance(uses, str):
        return []
    clean_uses = uses.split("#")[0].strip()
    if clean_uses.startswith("actions/setup-python@"):
        if not re.fullmatch(r"^actions/setup-python@[0-9a-fA-F]{40}$", clean_uses):
            return [f"job '{job_name}' actions/setup-python must be pinned by 40-hex SHA, got: {uses}"]
    elif not (
        clean_uses.startswith("actions/checkout@")
        or clean_uses == "actions/setup-go@d35c59abb061a4a6fb18e82ac0862c26744d6ab5"
    ):
        return [f"job '{job_name}' has disallowed pre-install action: {uses}"]
    return []


def _check_post_install_step(step: dict[str, Any], job_name: str) -> list[str]:
    """Validate an individual step that occurs after canonical install."""
    uses = step.get("uses")
    if not isinstance(uses, str):
        return []
    if "setup-python" in uses:
        return [f"job '{job_name}' setup-python must appear before canonical install step ({uses})"]
    if "checkout" in uses:
        return [f"job '{job_name}' checkout must appear before canonical install step ({uses})"]
    if uses not in ALLOWED_POST_INSTALL_USES:
        return [f"job '{job_name}' has disallowed action after canonical install: {uses}"]
    return []


def _check_uses_ordering(steps: list[Any], first_run_idx: int, job_name: str) -> list[str]:
    """Ensure setup-python precedes install and only allowlisted actions follow install."""
    errors: list[str] = []
    has_effective_setup_python = False
    for s in steps[:first_run_idx]:
        if isinstance(s, dict):
            errors.extend(_check_pre_install_step(s, job_name))
            if _is_effective_setup_python(s):
                has_effective_setup_python = True

    if not has_effective_setup_python:
        errors.append(f"job '{job_name}' requires actions/setup-python before canonical install step")

    for s in steps[first_run_idx + 1 :]:
        if isinstance(s, dict):
            errors.extend(_check_post_install_step(s, job_name))
    return errors


def _imported_roots_from_node(node: ast.AST) -> list[str]:
    """Extract root module names from an Import or ImportFrom node."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".")[0]]
    return []


def _scan_ast_imports(tree: ast.AST) -> list[str]:
    """Extract all imported root package names from AST across all scopes."""
    names: list[str] = []
    for node in ast.walk(tree):
        names.extend(_imported_roots_from_node(node))
    return names


def _is_relative_import_from(node: ast.AST) -> bool:
    """Return True if node is an ImportFrom with relative import or missing module."""
    return isinstance(node, ast.ImportFrom) and (node.level != 0 or not node.module)


def _check_ast_tree_stdlib_only(tree: ast.AST, label: str) -> list[str]:
    """Verify an AST tree contains only stdlib imports and no relative imports."""
    errors: list[str] = []
    for node in ast.walk(tree):
        if _is_relative_import_from(node):
            errors.append(f"{label} has disallowed relative import")
        for name in _imported_roots_from_node(node):
            if name not in sys.stdlib_module_names:
                errors.append(f"{label} has non-stdlib import: {name!r}")
    return errors


def _check_package_init_stdlib_only(init_path: Path) -> list[str]:
    """Validate package initializer on helper import path for stdlib-only imports."""
    if not init_path.is_file():
        return []
    try:
        init_tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    except Exception as exc:
        return [f"package initializer {init_path.name!r}: syntax or read error: {exc}"]
    return _check_ast_tree_stdlib_only(init_tree, f"package initializer {init_path.name!r}")


def _check_helper_stdlib_only(helper_rel: str, helper_path: Path) -> list[str]:
    """Ensure a local helper script uses only stdlib imports and no local/third-party imports."""
    try:
        source = helper_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(helper_path))
    except Exception as exc:
        return [f"local helper {helper_rel!r}: syntax or read error: {exc}"]
    errors = _check_ast_tree_stdlib_only(tree, f"local helper {helper_rel!r}")
    errors.extend(_check_package_init_stdlib_only(helper_path.parent / "__init__.py"))
    return errors


def _check_import_from_node(node: ast.ImportFrom, script_rel: str, repo_root: Path) -> list[str]:
    """Validate an ImportFrom AST node against stdlib or approved local helpers."""
    if node.level != 0 or not node.module:
        return [f"privileged script {script_rel!r} has disallowed relative import: level {node.level}"]
    mod_full = node.module
    mod_root = mod_full.split(".")[0]
    if mod_root in sys.stdlib_module_names:
        return []
    if mod_full not in PRIVILEGED_SCRIPT_ALLOWED_LOCAL_MODULES:
        return [f"privileged script {script_rel!r} has non-stdlib import: {mod_full!r}"]
    helper_rel = mod_full.replace(".", "/") + ".py"
    helper_path = repo_root / helper_rel
    if not helper_path.is_file():
        return [f"privileged script {script_rel!r} references missing local helper: {helper_rel}"]
    return _check_helper_stdlib_only(helper_rel, helper_path)


def _check_single_import_node(node: ast.AST, script_rel: str, repo_root: Path) -> list[str]:
    """Validate an individual AST import node against stdlib or approved helpers."""
    if isinstance(node, ast.Import):
        errors: list[str] = []
        for alias in node.names:
            mod_name = alias.name.split(".")[0]
            if mod_name not in sys.stdlib_module_names:
                errors.append(f"privileged script {script_rel!r} has non-stdlib import: {alias.name!r}")
        return errors
    if isinstance(node, ast.ImportFrom):
        return _check_import_from_node(node, script_rel, repo_root)
    return []


def _item_shadowed_stdlib_name(item: Path) -> str | None:
    """Return stdlib module name if item is a .py file or package matching stdlib."""
    if item.is_file() and item.name.endswith(".py"):
        return item.stem if item.stem in sys.stdlib_module_names else None
    if item.is_dir() and (item / "__init__.py").is_file():
        return item.name if item.name in sys.stdlib_module_names else None
    return None


def _check_single_item_shadow(item: Path, rel_loc: str, repo_root: Path) -> str | None:
    """Return error message if item shadows a stdlib module or package."""
    name = _item_shadowed_stdlib_name(item)
    if name:
        kind = "package" if item.is_dir() else "file"
        rel_path = item.relative_to(repo_root).as_posix()
        return (
            f"tracked {kind} '{rel_path}' in '{rel_loc}' "
            f"shadows standard library module {name!r} (is shadowed by tracked file '{name}.py')"
        )
    return None


def _check_directory_shadowing(loc: Path, repo_root: Path) -> list[str]:
    """Check a single directory for files or packages shadowing stdlib."""
    if not loc.is_dir():
        return []
    rel_loc = loc.relative_to(repo_root).as_posix() if loc != repo_root else "."
    errors: list[str] = []
    for item in sorted(loc.iterdir()):
        err = _check_single_item_shadow(item, rel_loc, repo_root)
        if err:
            errors.append(err)
    return errors


def _check_stdlib_shadowing(repo_root: Path) -> list[str]:
    """Reject any top-level module (X.py) or package (X/__init__.py) in repo root, scripts/, or tools/
    whose name matches a Python standard library module.
    """
    errors: list[str] = []
    for loc in (repo_root, repo_root / "scripts", repo_root / "tools"):
        errors.extend(_check_directory_shadowing(loc, repo_root))
    return errors


def _check_script_top_level_stdlib_only(script_rel: str, repo_root: Path) -> list[str]:
    """Ensure all AST imports in script (at any scope) are in sys.stdlib_module_names or approved local modules."""
    script_path = repo_root / script_rel
    if not script_path.is_file():
        return [f"allowlisted script {script_rel!r} not found at {script_path}"]
    try:
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(script_path))
    except Exception as exc:
        return [f"{script_rel}: syntax or read error: {exc}"]
    errors: list[str] = _check_stdlib_shadowing(repo_root)
    for node in ast.walk(tree):
        errors.extend(_check_single_import_node(node, script_rel, repo_root))
    return errors


def _extract_script_invocations(cmd_str: str) -> list[str]:
    """Extract python script paths invoked in cmd_str."""
    scripts: list[str] = []
    for line in cmd_str.splitlines():
        toks = _line_tokens(line)
        if not toks or toks[0] not in ("python", "python3"):
            continue
        args = toks[1:]
        if args and args[0] == "-B":
            args = args[1:]
        if args and args[0].endswith(".py"):
            scripts.append(args[0].lstrip("./"))
    return scripts


def _check_privileged_step(
    cmd_str: str,
    workflow_rel: str,
    job_name: str,
    root: Path,
) -> list[str]:
    """Check a single run command in a privileged job."""
    errors: list[str] = []
    for script in _extract_script_invocations(cmd_str):
        if script not in PRIVILEGED_JOB_ALLOWED_SCRIPTS:
            errors.append(
                f"privileged job '{job_name}' runs unapproved script {script!r}; "
                f"allowed: {sorted(PRIVILEGED_JOB_ALLOWED_SCRIPTS)}"
            )
        else:
            errors.extend(_check_script_top_level_stdlib_only(script, root))
    return errors


def _check_privileged_job(
    workflow_rel: str,
    job_name: str,
    steps: list[Any],
    repo_root: Path | None,
) -> list[str]:
    """Validate privileged job runs no installers and only allowed stdlib-only scripts."""
    root = repo_root or Path(__file__).resolve().parents[1]
    errors: list[str] = _check_stdlib_shadowing(root)
    for step in steps:
        if isinstance(step, dict) and step.get("run"):
            cmd_str = str(step["run"]).strip()
            errors.extend(_check_privileged_step(cmd_str, workflow_rel, job_name, root))
    return errors


def _is_valid_permission_entry(key: Any, val: Any) -> bool:
    """Return True if a single permission key-value pair is a known scope with read or none."""
    if not isinstance(key, str) or not isinstance(val, str):
        return False
    if key not in KNOWN_GITHUB_PERMISSION_SCOPES:
        return False
    return val in ("read", "none")


def _is_read_only_permissions_map(perms: Any) -> bool:
    """Return True if perms is a non-empty mapping where all entries are valid read-only scopes."""
    if not isinstance(perms, dict) or not perms:
        return False
    return all(_is_valid_permission_entry(k, v) for k, v in perms.items())


def _is_privileged_permissions(perms: Any) -> bool:
    """Check if permissions object grants any write permission or fails closed on unknown/omitted permissions."""
    if perms == "read-all":
        return False
    if _is_read_only_permissions_map(perms):
        return False
    return True


def _effective_job_permissions(job: dict[str, Any], workflow_doc: dict[str, Any] | None) -> Any:
    """Resolve effective permissions for a job (job overrides workflow)."""
    if "permissions" in job:
        return job.get("permissions")
    if workflow_doc and "permissions" in workflow_doc:
        return workflow_doc.get("permissions")
    return None


def _is_job_privileged(job: dict[str, Any], workflow_doc: dict[str, Any] | None) -> bool:
    """Return True if job has any effective write permission."""
    eff_perms = _effective_job_permissions(job, workflow_doc)
    return _is_privileged_permissions(eff_perms)


def _check_job_env(env: Any, scope: str, job_name: str) -> list[str]:
    """Reject PIP_*, PYTHON*, and PATH environment variables."""
    if not isinstance(env, dict):
        return []
    errors: list[str] = []
    for key in env:
        if isinstance(key, str) and (key.startswith("PIP_") or key.startswith("PYTHON") or key == "PATH"):
            errors.append(f"{scope} '{job_name}' has disallowed environment variable: {key!r}")
    return errors


def _check_job_defaults_run(defaults: Any, scope: str, job_name: str) -> list[str]:
    """Reject defaults.run (shell, working-directory) in jobs with canonical install."""
    if not isinstance(defaults, dict):
        return []
    run_defaults = defaults.get("run")
    if not isinstance(run_defaults, dict):
        return []
    errors: list[str] = []
    if "shell" in run_defaults or "working-directory" in run_defaults or "working_directory" in run_defaults:
        errors.append(f"{scope} '{job_name}' must not define defaults.run (shell/working-directory)")
    return errors


def _check_job_and_workflow_context(
    job_name: str,
    job: dict[str, Any] | None,
    workflow_doc: dict[str, Any] | None,
) -> list[str]:
    """Validate job- and workflow-level environment and defaults."""
    errors: list[str] = []
    if job:
        errors.extend(_check_job_defaults_run(job.get("defaults"), "job", job_name))
        errors.extend(_check_job_env(job.get("env"), "job", job_name))
    if workflow_doc:
        errors.extend(_check_job_defaults_run(workflow_doc.get("defaults"), "workflow", job_name))
        errors.extend(_check_job_env(workflow_doc.get("env"), "workflow", job_name))
    return errors


def _check_all_steps_env(steps: list[Any], job_name: str) -> list[str]:
    """Check restricted environment variables across all steps in a job."""
    errors: list[str] = []
    for s in steps:
        if isinstance(s, dict) and "env" in s:
            errors.extend(_check_job_env(s.get("env"), "step", job_name))
    return errors


def _check_actions_only_job(steps: list[Any], job_name: str) -> list[str]:
    """Validate an actions-only job (non-privileged job with no run steps)."""
    errors: list[str] = []
    for s in steps:
        if isinstance(s, dict):
            errors.extend(_check_pre_install_step(s, job_name))
    errors.append(
        f"job '{job_name}' has no run steps; "
        f"non-privileged jobs require canonical install step {CANONICAL_INSTALL_CMD!r}"
    )
    return errors


def _check_regular_job_install_rule(
    job_name: str,
    steps: list[Any],
    job: dict[str, Any] | None = None,
    workflow_doc: dict[str, Any] | None = None,
) -> list[str]:
    """Validate Rule B for a regular (non-privileged) workflow job."""
    errors: list[str] = _check_job_and_workflow_context(job_name, job, workflow_doc)
    errors.extend(_check_all_steps_env(steps, job_name))

    run_steps = [
        (idx, s)
        for idx, s in enumerate(steps)
        if isinstance(s, dict) and s.get("run") is not None and str(s.get("run", "")).strip()
    ]
    if not run_steps:
        errors.extend(_check_actions_only_job(steps, job_name))
        return errors

    first_run_idx, first_step = run_steps[0]
    first_run = str(first_step.get("run", "")).strip()
    if first_run != CANONICAL_INSTALL_CMD:
        errors.append(f"job '{job_name}' first run step must be exactly {CANONICAL_INSTALL_CMD!r}, got: {first_run!r}")
    else:
        errors.extend(_check_install_step_guards(first_step, job_name))
        errors.extend(_check_uses_ordering(steps, first_run_idx, job_name))

    return errors


def _is_allowlisted_job(workflow_rel: str, job_name: str, is_privileged: bool) -> bool:
    """Return True if job is allowlisted as privileged."""
    if (workflow_rel, job_name) in PRIVILEGED_JOB_ALLOWLIST:
        return True
    return not workflow_rel and job_name == "release" and is_privileged


def _check_allowlisted_job(
    job_name: str,
    job: dict[str, Any],
    workflow_rel: str,
    repo_root: Path | None,
) -> list[str]:
    """Validate an allowlisted privileged job."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        return [f"privileged job '{job_name}' in '{workflow_rel}' must define steps"]
    wf_name = workflow_rel or ".github/workflows/release.yaml"
    return _check_privileged_job(wf_name, job_name, steps, repo_root)


def _check_reusable_workflow_job(
    job_name: str,
    job: dict[str, Any],
    workflow_rel: str,
    workflow_doc: dict[str, Any] | None,
) -> list[str]:
    """Validate a reusable workflow job against exact triple allowlist."""
    uses_target = str(job.get("uses", "")).strip()
    triple = (workflow_rel, job_name, uses_target)
    if triple not in PRIVILEGED_REUSABLE_JOB_ALLOWLIST:
        return [
            f"job '{job_name}' in '{workflow_rel}' invokes unallowlisted reusable workflow '{uses_target}'; "
            f"is not in PRIVILEGED_REUSABLE_JOB_ALLOWLIST and is not in PRIVILEGED_JOB_ALLOWLIST"
        ]
    eff_perms = _effective_job_permissions(job, workflow_doc)
    if _is_privileged_permissions(eff_perms):
        return [
            f"reusable workflow job '{job_name}' in '{workflow_rel}' has write permissions "
            f"but is not in PRIVILEGED_JOB_ALLOWLIST"
        ]
    return []


def _check_job_install_rule(
    job_name: str,
    job: dict[str, Any],
    workflow_rel: str = "",
    repo_root: Path | None = None,
    workflow_doc: dict[str, Any] | None = None,
) -> list[str]:
    """Validate install rule for a single workflow job."""
    if not isinstance(job, dict):
        return []

    if "uses" in job:
        return _check_reusable_workflow_job(job_name, job, workflow_rel, workflow_doc)

    is_privileged = _is_job_privileged(job, workflow_doc)
    is_allowlisted = _is_allowlisted_job(workflow_rel, job_name, is_privileged)

    if is_privileged and not is_allowlisted:
        return [f"job '{job_name}' in '{workflow_rel}' has write permissions but is not in PRIVILEGED_JOB_ALLOWLIST"]

    if is_allowlisted:
        return _check_allowlisted_job(job_name, job, workflow_rel, repo_root)

    steps = job.get("steps")
    if not isinstance(steps, list):
        return [f"job '{job_name}' in '{workflow_rel}' must define steps"]

    return _check_regular_job_install_rule(job_name, steps, job=job, workflow_doc=workflow_doc)


def validate_workflow_install_steps(
    doc: dict[str, Any],
    workflow_rel: str = "",
    repo_root: Path | None = None,
) -> list[str]:
    """Validate Rule B: the canonical install runs first and unconditionally in every non-privileged job,
    and privileged jobs run no Python with third-party dependencies.
    """
    if not isinstance(doc, dict):
        return ["workflow document must be a mapping"]
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    errors: list[str] = []
    for job_name, job in jobs.items():
        errors.extend(
            _check_job_install_rule(
                str(job_name),
                job,
                workflow_rel=workflow_rel,
                repo_root=repo_root,
                workflow_doc=doc,
            )
        )
    return errors


def validate_all_workflows_install_steps(repo_root: Path | None = None) -> list[str]:
    """Scan and validate Rule B across every workflow under .github/workflows/."""
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    errors: list[str] = []
    for wf_path in sorted(wf_dir.glob("*.y*ml")):
        doc, load_errs = _load_workflow_mapping(wf_path)
        if doc is None:
            errors.extend(load_errs)
            continue
        try:
            rel_path = wf_path.relative_to(root).as_posix()
        except ValueError:
            rel_path = wf_path.as_posix()
        errors.extend(validate_workflow_install_steps(doc, workflow_rel=rel_path, repo_root=root))
    return errors


def _read_pinned_distributions(requirements_path: Path) -> set[str]:
    """Read distribution names from requirements-dev.txt."""
    if not requirements_path.is_file():
        return set()
    dists: set[str] = set()
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if _check_req_dev_line(trimmed) is not None:
            continue
        dist_name = trimmed.split("==", 1)[0].strip().lower()
        if dist_name:
            dists.add(dist_name)
    return dists


def _is_allowed_import(name: str, pinned_dists: set[str]) -> bool:
    """Return True if top-level import name satisfies Rule C."""
    if name in sys.stdlib_module_names:
        return True
    if name in LOCAL_TOP_LEVEL:
        return True
    dist = MODULE_TO_DIST.get(name)
    if dist is not None and dist.lower() in pinned_dists:
        return True
    return False


def _validate_single_python_file(
    py_path: Path,
    repo_root: Path,
    pinned_dists: set[str],
) -> list[str]:
    """Validate imports in a single python file under Rule C."""
    try:
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_path))
    except Exception as exc:
        return [f"{py_path.name}: syntax or read error: {exc}"]

    imported_names = _scan_ast_imports(tree)
    errors: list[str] = []
    try:
        rel_path = py_path.relative_to(repo_root).as_posix()
    except ValueError:
        rel_path = py_path.as_posix()
    for name in imported_names:
        if not _is_allowed_import(name, pinned_dists):
            errors.append(
                f"{rel_path}: disallowed top-level import {name!r} "
                "(not in stdlib, LOCAL_TOP_LEVEL, or mapped in requirements-dev.txt)"
            )
    return errors


def get_tracked_python_files(repo_root: Path) -> list[Path]:
    """Collect tracked python files under repo_root excluding tests/fixtures/**."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw_paths = proc.stdout.strip().splitlines()
            return [repo_root / p for p in raw_paths if not p.startswith("tests/fixtures/")]
    except Exception:
        pass

    return [p for p in repo_root.rglob("*.py") if not p.relative_to(repo_root).as_posix().startswith("tests/fixtures/")]


def validate_tracked_python_imports(repo_root: Path | None = None) -> list[str]:
    """Rule C: AST-scan tracked python files to ensure imports are stdlib, local, or mapped."""
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    req_file = root / "requirements-dev.txt"
    pinned_dists = _read_pinned_distributions(req_file)

    files = get_tracked_python_files(root)
    errors: list[str] = []
    for f in sorted(files):
        errors.extend(_validate_single_python_file(f, root, pinned_dists))
    return errors


def validate_quality_workflow(workflow_input: dict[str, Any] | str | Path) -> list[str]:
    """Validate quality workflow contract."""
    doc, load_errs = _load_workflow_mapping(workflow_input)
    if doc is None:
        return load_errs

    errors = list(validate_shell_configuration("quality workflow", doc))
    errors.extend(validate_env_configuration("quality workflow", doc, allowed_keys=frozenset()))
    wf_perms = doc.get("permissions")
    if wf_perms != {"contents": "read"}:
        errors.append(f"quality workflow permissions must be {{'contents': 'read'}}, got: {wf_perms!r}")
    repo_root = Path(__file__).resolve().parents[1]
    if isinstance(workflow_input, str | Path):
        p = Path(workflow_input)
        if p.is_file():
            repo_root = p.resolve().parents[2]
            errors.extend(check_no_tracked_coverage_json(repo_root))
    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return ["Quality workflow jobs must be a mapping"]

    extra_jobs = set(jobs.keys()) - {"quality", "tests", "test"}
    if extra_jobs:
        errors.append(
            f"quality workflow jobs must contain only 'quality' and 'tests', got unexpected: {sorted(extra_jobs)}"
        )

    errors.extend(_check_quality_job_ifs(jobs))
    errors.extend(_validate_quality_workflow_jobs(jobs))
    errors.extend(
        validate_workflow_install_steps(doc, workflow_rel=".github/workflows/quality.yaml", repo_root=repo_root)
    )
    return errors


def validate_skills_directory(repo_root: Path | None = None) -> tuple[list[str], int]:
    """Validate skill naming and structure in repository."""
    root = repo_root or Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.naming import validate_all_skills

    return validate_all_skills(root)


def main(argv: list[str] | None = None) -> int:
    """Validate all skills in repo and quality workflow contract."""
    root = Path(__file__).resolve().parents[1]
    skill_errors, count = validate_skills_directory(root)
    workflow_path = root / ".github" / "workflows" / "quality.yaml"
    wf_errors = validate_quality_workflow(workflow_path) if workflow_path.is_file() else ["quality.yaml not found"]
    all_wf_errors = validate_all_workflows_install_steps(root)
    req_errors = validate_requirements_dev_file(root / "requirements-dev.txt")
    import_errors = validate_tracked_python_imports(root)
    cov_errors = check_no_tracked_coverage_json(root)

    all_errors = list(skill_errors) + wf_errors + all_wf_errors + req_errors + import_errors + cov_errors
    if all_errors:
        for err in all_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print(f"Validated {count} skills and quality workflow successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
