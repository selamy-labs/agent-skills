#!/usr/bin/env python3
"""Skill and quality workflow validator."""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import sys
import tomllib
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
    """Verify that the primary pip install step pins all required tools."""
    errors: list[str] = []
    for _pkg_name, expected_token in PINNED_TOOL_REQUIREMENTS.items():
        if not any(t == expected_token for t in tokens):
            errors.append(f"Dependency install step missing {expected_token!r} pin")
    return errors


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


QUALITY_JOB_ALLOWED_ACTIONS: frozenset[str] = frozenset({"actions/checkout@v7.0.0", "actions/setup-python@v6.3.0"})

ALLOWED_USES_ACTIONS: frozenset[str] = frozenset(
    {
        "actions/checkout@v7.0.0",
        "actions/setup-python@v6.3.0",
        "actions/setup-go@d35c59abb061a4a6fb18e82ac0862c26744d6ab5",
    }
)

CANONICAL_QUALITY_RUN_ARGVS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("pip", "install", "pyyaml==6.0.1"),
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
    (
        "pip",
        "install",
        "pytest==8.3.4",
        "pytest-cov==6.0.0",
        "ruff==0.15.21",
        "pyyaml==6.0.1",
    ),
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
    elif uses == "actions/setup-python@v6.3.0":
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


WORKFLOW_PACKAGE_TO_MODULES: dict[str, set[str]] = {
    "pyyaml": {"yaml"},
    "pytest": {"pytest", "_pytest"},
    "pytest-cov": {"pytest_cov"},
    "ruff": {"ruff"},
    "complexipy": {"complexipy"},
    "vulture": {"vulture"},
    "mypy": {"mypy"},
}

ACCEPTED_COMMAND_SEGMENTS: frozenset[str] = frozenset(
    {
        'gh release create "${{ steps.plan.outputs.next_tag }}" --notes-file release-notes.md',
        'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"',
        'git config user.name "github-actions[bot]"',
        'git merge-base --is-ancestor "${{ github.event.workflow_run.head_sha || github.sha }}" origin/main',
        'git push origin "${{ steps.plan.outputs.next_tag }}"',
        'git tag -a "${{ steps.plan.outputs.next_tag }}" -F release-notes.md',
        "go vet ./...",
        "pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 pyyaml==6.0.1",
        "pip install pyyaml==6.0.1",
        "pytest --cov=tools --cov-branch --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=90",
        "python -m ruff check .",
        "python -m ruff format --check --diff .",
        "python -m tests.test_coverage_threshold coverage.json",
        "python -m unittest discover -s skills/portfolio-delivery-control/tests -v",
        "python scripts/lint_skills.py",
        "python scripts/privacy_scan.py",
        "python scripts/public_api_stability.py",
        "python scripts/security_scan.py",
        "python scripts/semantic_release.py --notes-file release-notes.md",
        "python scripts/test_pr_workflow_cancellation.py",
        "python scripts/test_privacy_scan.py",
        "python scripts/test_security_scan.py",
        "python scripts/test_semantic_release.py",
        'python scripts/validate_pr_title.py --title "$PR_TITLE" --body "$PR_BODY"',
        "python tools/tf_module_layout.py --self-test",
        "python tools/validate_marketplace.py",
        "python tools/validate_skills.py",
        "python3 -B scripts/lint_skills.py",
        "python3 -B scripts/privacy_scan.py",
        "python3 -B scripts/public_api_stability.py",
        "python3 -B scripts/security_scan.py",
        "python3 -B scripts/test_pr_workflow_cancellation.py",
        "python3 -B scripts/test_privacy_scan.py",
        "python3 -B scripts/test_security_scan.py",
        "python3 -B scripts/test_semantic_release.py",
        (
            'python3 -B tools/release_gate.py --repo "${{ github.repository }}" '
            '--sha "${{ github.event.workflow_run.head_sha || github.sha }}" '
            '--expected-app-slug "github-actions" '
            '--check-suite-id "${{ github.event.workflow_run.check_suite_id }}" '
            "--wait-seconds 600 --poll-interval-seconds 10"
        ),
    }
)


def _extract_segments_from_step(step: Any) -> list[str]:
    if not isinstance(step, dict) or not isinstance(step.get("run"), str):
        return []
    lines = []
    for line in step["run"].splitlines():
        trimmed = line.strip()
        if trimmed and not trimmed.startswith("#"):
            lines.append(" ".join(trimmed.split()))
    return lines


def _extract_segments_from_job(job: Any) -> list[str]:
    if not isinstance(job, dict):
        return []
    segs: list[str] = []
    for step in job.get("steps", []):
        segs.extend(_extract_segments_from_step(step))
    return segs


def _extract_segments_from_workflow_file(wf_file: Path) -> list[str]:
    content = yaml.safe_load(wf_file.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        return []
    segs: list[str] = []
    for job in content.get("jobs", {}).values():
        segs.extend(_extract_segments_from_job(job))
    return segs


def extract_workflow_command_segments(repo_root: Path) -> set[str]:
    wf_dir = repo_root / ".github" / "workflows"
    segments: set[str] = set()
    for wf_file in sorted(wf_dir.glob("*.y*ml")):
        segments.update(_extract_segments_from_workflow_file(wf_file))
    return segments


def _get_enclosing_package_inits(file_path: Path, repo_root: Path) -> list[Path]:
    inits: list[Path] = []
    curr = file_path.parent
    while curr != repo_root:
        try:
            curr.relative_to(repo_root)
        except ValueError:
            break
        init = curr / "__init__.py"
        if init.is_file():
            inits.append(init)
        curr = curr.parent
    return inits


def _check_candidate_file(base: Path, rel: Path) -> Path | None:
    init_file = base / rel / "__init__.py"
    if init_file.is_file():
        return init_file
    py_file = base / f"{rel}.py"
    if py_file.is_file():
        return py_file
    return None


def _resolve_leaf_in_base(base: Path, curr: Path, parts: list[str]) -> list[Path]:
    init_file = curr / "__init__.py"
    if init_file.is_file():
        return [init_file]
    py_file = base / f"{Path(*parts)}.py"
    if py_file.is_file():
        return [py_file]
    return []


def _resolve_chain_in_base(base: Path, parts: list[str]) -> list[Path]:
    chain: list[Path] = []
    curr = base
    for i, part in enumerate(parts):
        curr = curr / part
        if i == len(parts) - 1:
            leaf = _resolve_leaf_in_base(base, curr, parts)
            return chain + leaf if leaf else []
        init_file = curr / "__init__.py"
        if init_file.is_file():
            chain.append(init_file)
        elif not curr.is_dir():
            return []
    return chain


def _collect_ancestor_bases(file_path: Path, repo_root: Path) -> list[Path]:
    bases: list[Path] = []
    if file_path == repo_root:
        return bases
    curr = file_path.parent
    while curr != repo_root:
        try:
            curr.relative_to(repo_root)
        except ValueError:
            break
        if curr not in bases:
            bases.append(curr)
        curr = curr.parent
    return bases


def _get_search_bases(
    file_path: Path,
    repo_root: Path,
    entry_roots: list[Path] | None = None,
) -> list[Path]:
    bases: list[Path] = []
    if entry_roots:
        for r in entry_roots:
            if r not in bases:
                bases.append(r)
    for b in _collect_ancestor_bases(file_path, repo_root):
        if b not in bases:
            bases.append(b)
    if repo_root not in bases:
        bases.append(repo_root)
    return bases


def _resolve_dotted_module_to_files(
    mod: str,
    search_bases: list[Path],
) -> list[Path]:
    parts = mod.split(".")
    candidates: list[Path] = []
    for base in search_bases:
        chain = _resolve_chain_in_base(base, parts)
        for c in chain:
            if c not in candidates:
                candidates.append(c)
    return candidates


def _has_local_candidate(top: str, search_bases: list[Path]) -> bool:
    for base in search_bases:
        if (base / top / "__init__.py").is_file():
            return True
        if (base / f"{top}.py").is_file():
            return True
        if (base / top).is_dir():
            return True
    return False


def _scan_resolved_local_files(
    locals_: list[Path],
    repo_root: Path,
    visited: set[Path],
    entry_roots: list[Path],
) -> tuple[set[str], str | None]:
    all_third_party: set[str] = set()
    for local in locals_:
        tp, err = _get_transitive_script_third_party_imports(local, repo_root, visited, entry_roots)
        if err is not None:
            return set(), err
        all_third_party.update(tp)
    return all_third_party, None


def _resolve_relative_base(
    file_path: Path,
    level: int,
    module: str | None,
) -> tuple[Path, list[Path], str | None]:
    base = file_path.parent
    for _ in range(max(0, level - 1)):
        base = base.parent
    intermediates: list[Path] = []
    if module:
        for part in module.split("."):
            base = base / part
            init_file = base / "__init__.py"
            if init_file.is_file():
                intermediates.append(init_file)
            elif not base.is_dir() and not base.with_suffix(".py").is_file():
                return base, [], f"cannot resolve relative import chain in '{file_path}': module '{module}' not found"
    return base, intermediates, None


def _find_alias_candidates(base: Path, names: list[ast.alias]) -> list[Path]:
    targets: list[Path] = []
    for alias in names:
        cand = _check_candidate_file(base, Path(alias.name))
        if cand is not None:
            targets.append(cand)
    return targets


def _find_fallback_base_candidate(base: Path) -> Path | None:
    direct = _check_candidate_file(base.parent, Path(base.name))
    if direct is not None:
        return direct
    py_file = base.with_suffix(".py")
    if py_file.is_file():
        return py_file
    init_file = base / "__init__.py"
    if init_file.is_file():
        return init_file
    return None


def _resolve_relative_leaf(base: Path, names: list[ast.alias]) -> list[Path]:
    targets = _find_alias_candidates(base, names)
    if targets:
        return targets
    fallback = _find_fallback_base_candidate(base)
    if fallback is not None:
        return [fallback]
    return []


def _extract_relative_import_targets(
    node: ast.ImportFrom,
    file_path: Path,
    repo_root: Path,
) -> tuple[list[Path], str | None]:
    base, intermediates, err = _resolve_relative_base(file_path, node.level, node.module)
    if err is not None:
        return [], err
    targets = _resolve_relative_leaf(base, node.names)
    if not targets:
        return [], f"cannot resolve relative import chain in '{file_path}': level={node.level}, module={node.module}"
    targets.extend(intermediates)
    targets.extend(_get_enclosing_package_inits(file_path, repo_root))
    return targets, None


def _collect_import_node_targets(node: ast.Import, named_targets: set[str]) -> None:
    for alias in node.names:
        named_targets.add(alias.name)


def _collect_import_from_targets(
    node: ast.ImportFrom,
    file_path: Path,
    repo_root: Path,
    named_targets: set[str],
    file_targets: set[Path],
    search_bases: list[Path],
) -> str | None:
    if node.level == 0 and node.module:
        mod_files = _resolve_dotted_module_to_files(node.module, search_bases)
        if mod_files:
            file_targets.update(mod_files)
        elif not (repo_root / node.module).is_dir():
            named_targets.add(node.module)

        for alias in node.names:
            full = f"{node.module}.{alias.name}"
            cands = _resolve_dotted_module_to_files(full, search_bases)
            if cands:
                file_targets.update(cands)
            elif not mod_files and not (repo_root / node.module).is_dir():
                named_targets.add(full)
    elif node.level > 0:
        rel_files, err = _extract_relative_import_targets(node, file_path, repo_root)
        if err is not None:
            return err
        file_targets.update(rel_files)
    return None


def _parse_ast_imports(
    tree: ast.AST,
    file_path: Path,
    repo_root: Path,
    search_bases: list[Path],
) -> tuple[set[str], set[Path], str | None]:
    named_targets: set[str] = set()
    file_targets: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _collect_import_node_targets(node, named_targets)
        elif isinstance(node, ast.ImportFrom):
            err = _collect_import_from_targets(node, file_path, repo_root, named_targets, file_targets, search_bases)
            if err is not None:
                return set(), set(), err
    return named_targets, file_targets, None


def _get_script_ast_imports(
    file_path: Path,
    repo_root: Path,
    search_bases: list[Path],
) -> tuple[set[str], set[Path], str | None]:
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except Exception as exc:
        return set(), set(), f"unparseable script '{file_path}': {exc}"
    return _parse_ast_imports(tree, file_path, repo_root, search_bases)


KNOWN_THIRD_PARTY_MODULES: frozenset[str] = frozenset(
    {
        "yaml",
        "pyyaml",
        "pytest",
        "_pytest",
        "pytest_cov",
        "ruff",
        "complexipy",
        "vulture",
        "mypy",
    }
)


def _classify_module_import(
    mod: str,
    file_path: Path,
    repo_root: Path,
    visited: set[Path],
    entry_roots: list[Path],
) -> tuple[set[str], str | None]:
    top = mod.split(".")[0]
    search_bases = _get_search_bases(file_path, repo_root, entry_roots)
    has_local = _has_local_candidate(top, search_bases)
    stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
    if not has_local and (top in stdlib or top == "__future__"):
        return set(), None

    locals_ = _resolve_dotted_module_to_files(mod, search_bases)
    third_party: set[str] = set()
    if top in KNOWN_THIRD_PARTY_MODULES:
        third_party.add(top)

    if locals_:
        local_tp, err = _scan_resolved_local_files(locals_, repo_root, visited, entry_roots)
        if err is not None:
            return set(), err
        third_party.update(local_tp)
        return third_party, None

    if has_local and "." in mod:
        return set(), f"cannot resolve local import chain '{mod}'"

    if top not in stdlib and top != "__future__":
        third_party.add(top)

    return third_party, None


def _get_transitive_script_third_party_imports(
    file_path: Path,
    repo_root: Path,
    visited: set[Path] | None = None,
    entry_roots: list[Path] | None = None,
) -> tuple[set[str], str | None]:
    if visited is None:
        visited = set()
    if file_path in visited or not file_path.is_file():
        return set(), None
    visited.add(file_path)

    if entry_roots is None:
        entry_roots = [file_path.parent, repo_root]

    search_bases = _get_search_bases(file_path, repo_root, entry_roots)
    named_targets, file_targets, err = _get_script_ast_imports(file_path, repo_root, search_bases)
    if err is not None:
        return set(), err

    all_third_party: set[str] = set()
    for mod in sorted(named_targets):
        third_party, c_err = _classify_module_import(mod, file_path, repo_root, visited, entry_roots)
        if c_err is not None:
            return set(), c_err
        all_third_party.update(third_party)

    for ft in sorted(file_targets):
        tp, f_err = _get_transitive_script_third_party_imports(ft, repo_root, visited, entry_roots)
        if f_err is not None:
            return set(), f_err
        all_third_party.update(tp)

    return all_third_party, None


def _is_print_call_allowed(call_node: ast.Call) -> bool:
    if not isinstance(call_node.func, ast.Name) or call_node.func.id != "print":
        return False
    if call_node.keywords:
        return False
    for a in call_node.args:
        if not isinstance(a, ast.Constant):
            return False
    return True


def _is_allowed_c_expr(expr_val: ast.expr) -> bool:
    if isinstance(expr_val, ast.Constant):
        return True
    if isinstance(expr_val, ast.Call):
        return _is_print_call_allowed(expr_val)
    return False


def _is_print_rebound(names: list[ast.alias]) -> bool:
    for alias in names:
        if alias.asname == "print" or alias.name == "print":
            return True
    return False


def _check_python_c_import(stmt: ast.Import | ast.ImportFrom) -> str | None:
    if isinstance(stmt, ast.ImportFrom) and stmt.level > 0:
        return "relative imports in python -c are not allowed"
    if _is_print_rebound(stmt.names):
        return "rebinding of print builtin in python -c is forbidden"
    return None


def _check_python_c_stmt(stmt: ast.stmt) -> str | None:
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return _check_python_c_import(stmt)
    if isinstance(stmt, ast.Pass):
        return None
    if isinstance(stmt, ast.Expr) and _is_allowed_c_expr(stmt.value):
        return None
    return "unanalysable statement or call in python -c"


def _check_python_c_ast_allowlist(tree: ast.AST) -> str | None:
    if not isinstance(tree, ast.Module):
        return "python -c AST must be a Module"
    for stmt in tree.body:
        err = _check_python_c_stmt(stmt)
        if err is not None:
            return err
    return None


def _check_c_named_targets(
    named_targets: set[str],
    inline_path: Path,
    root: Path,
    job_name: str,
    step_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> bool:
    stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
    for mod in sorted(named_targets):
        needed, err = _classify_module_import(mod, inline_path, root, set(), [root])
        if err is not None:
            errors.append(f"job '{job_name}' step '{step_name}': unanalysable import in python -c code: {err}")
            return False
        for req in sorted(needed):
            if req not in installed_modules and req not in stdlib:
                errors.append(
                    f"job '{job_name}' step '{step_name}': inline python code imports '{req}' before it is installed"
                )
    return True


def _check_c_file_targets(
    file_targets: set[Path],
    root: Path,
    job_name: str,
    step_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> bool:
    stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
    for ft in sorted(file_targets):
        tp, f_err = _get_transitive_script_third_party_imports(ft, root)
        if f_err is not None:
            errors.append(f"job '{job_name}' step '{step_name}': {f_err}")
            return False
        for req in sorted(tp):
            if req not in installed_modules and req not in stdlib:
                errors.append(
                    f"job '{job_name}' step '{step_name}': inline python code imports '{req}' before it is installed"
                )
    return True


def _scan_python_c_imports(
    code: str,
    root: Path,
    job_name: str,
    step_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> None:
    try:
        tree = ast.parse(code)
    except Exception as exc:
        errors.append(f"job '{job_name}' step '{step_name}': unanalysable python -c code: {exc}")
        return

    ast_err = _check_python_c_ast_allowlist(tree)
    if ast_err is not None:
        errors.append(f"job '{job_name}' step '{step_name}': {ast_err}")
        return

    inline_path = root / "__inline__.py"
    search_bases = _get_search_bases(inline_path, root)
    named_targets, file_targets, p_err = _parse_ast_imports(tree, inline_path, root, search_bases)
    if p_err is not None:
        errors.append(f"job '{job_name}' step '{step_name}': {p_err}")
        return

    if not _check_c_named_targets(named_targets, inline_path, root, job_name, step_name, installed_modules, errors):
        return
    _check_c_file_targets(file_targets, root, job_name, step_name, installed_modules, errors)


def _validate_discovered_test_file(
    test_file: Path,
    root: Path,
    job_name: str,
    step_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> bool:
    third_party, err = _get_transitive_script_third_party_imports(test_file, root)
    if err is not None:
        errors.append(f"job '{job_name}' step '{step_name}': {err}")
        return False
    stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
    for req in sorted(third_party):
        if req not in installed_modules and req not in stdlib:
            errors.append(
                f"job '{job_name}' step '{step_name}': unittest discovery imports '{req}' before it is installed"
            )
    return True


def _scan_unittest_discover_imports(
    root: Path,
    start_dir: str,
    pattern: str,
    job_name: str,
    step_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> None:
    target_dir = root / start_dir if not Path(start_dir).is_absolute() else Path(start_dir)
    if not target_dir.is_dir():
        errors.append(
            f"job '{job_name}' step '{step_name}': cannot resolve unittest discover start directory '{start_dir}'"
        )
        return
    for test_file in sorted(target_dir.rglob(pattern)):
        if test_file.is_file() and test_file.suffix == ".py":
            if not _validate_discovered_test_file(test_file, root, job_name, step_name, installed_modules, errors):
                return


def _get_configured_testpaths(root: Path) -> list[str]:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            testpaths = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths")
            if isinstance(testpaths, list) and all(isinstance(p, str) for p in testpaths):
                return testpaths
        except Exception:
            pass
    return ["tests", "scripts", "skills/portfolio-delivery-control/tests"]


def _get_target_ancestor_dirs(target: Path, root: Path) -> list[Path]:
    curr = target if target.is_dir() else target.parent
    dirs: list[Path] = []
    while True:
        dirs.append(curr)
        if curr == root:
            break
        try:
            curr.relative_to(root)
            curr = curr.parent
        except ValueError:
            break
    if root not in dirs:
        dirs.append(root)
    return dirs


def _collect_ancestor_conftests(target: Path, root: Path) -> list[Path]:
    conftests: list[Path] = []
    for d in _get_target_ancestor_dirs(target, root):
        cand = d / "conftest.py"
        if cand.is_file() and cand not in conftests:
            conftests.append(cand)
    return conftests


def _extract_plugin_names_from_expr(expr_val: ast.expr) -> list[str] | None:
    if isinstance(expr_val, (ast.List, ast.Tuple)):
        names: list[str] = []
        for elt in expr_val.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                names.append(elt.value)
            else:
                return None
        return names
    if isinstance(expr_val, ast.Constant) and isinstance(expr_val.value, str):
        return [expr_val.value]
    return None


def _extract_plugins_from_assign(node: ast.Assign) -> tuple[list[str], bool]:
    if any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets):
        val = _extract_plugin_names_from_expr(node.value)
        return (val if val is not None else []), (val is not None)
    return [], True


def _extract_plugins_from_ann_assign(node: ast.AnnAssign) -> tuple[list[str], bool]:
    if isinstance(node.target, ast.Name) and node.target.id == "pytest_plugins":
        if node.value is None:
            return [], True
        val = _extract_plugin_names_from_expr(node.value)
        return (val if val is not None else []), (val is not None)
    return [], True


def _extract_plugins_from_node(node: ast.AST) -> tuple[list[str], bool]:
    if isinstance(node, ast.Assign):
        return _extract_plugins_from_assign(node)
    if isinstance(node, ast.AnnAssign):
        return _extract_plugins_from_ann_assign(node)
    return [], True


def _extract_pytest_plugins(f: Path) -> tuple[list[str], bool]:
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
    except Exception:
        return [], True
    plugins: list[str] = []
    for node in ast.walk(tree):
        names, ok = _extract_plugins_from_node(node)
        if not ok:
            return [], False
        plugins.extend(names)
    return plugins, True


def _collect_all_py_files_under(target: Path, root: Path) -> list[Path]:
    files: list[Path] = []
    if target.is_file() and target.suffix == ".py":
        return [target]
    if target.is_dir():
        for f in sorted(target.rglob("*.py")):
            try:
                rel = f.relative_to(root)
                if "fixtures" in rel.parts:
                    continue
            except ValueError:
                pass
            files.append(f)
    return files


def _check_pytest_plugin(
    plugin: str,
    conftest_file: Path,
    root: Path,
    installed_modules: set[str],
    errors: list[str],
    job_name: str,
    step_name: str,
) -> None:
    top = plugin.split(".")[0]
    stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
    if top not in stdlib and top not in installed_modules:
        errors.append(f"job '{job_name}' step '{step_name}': pytest plugin '{plugin}' is not installed")
        return
    search_bases = _get_search_bases(conftest_file, root)
    locals_ = _resolve_dotted_module_to_files(plugin, search_bases)
    for lf in locals_:
        tp, err = _get_transitive_script_third_party_imports(lf, root)
        if err is not None:
            errors.append(f"job '{job_name}' step '{step_name}': {err}")
        for req in tp:
            if req not in installed_modules and req not in stdlib:
                errors.append(
                    f"job '{job_name}' step '{step_name}': pytest plugin '{plugin}' "
                    f"transitively requires '{req}' before it is installed"
                )


def _check_pytest_prerequisites(
    cmd: str,
    installed_modules: set[str],
    errors: list[str],
    job_name: str,
    step_name: str,
) -> bool:
    if "pytest" not in installed_modules:
        errors.append(f"job '{job_name}' step '{step_name}': executes 'pytest' before it is installed")
        return False
    if "--cov" in cmd and "pytest_cov" not in installed_modules:
        errors.append(f"job '{job_name}' step '{step_name}': pytest --cov requires 'pytest-cov' to be installed")
        return False
    return True


def _collect_all_test_and_conftest_files(root: Path) -> tuple[list[Path], set[Path]]:
    testpaths = _get_configured_testpaths(root)
    collected_files: list[Path] = []
    conftests: set[Path] = set()

    root_conftest = root / "conftest.py"
    if root_conftest.is_file():
        conftests.add(root_conftest)

    for tp_str in testpaths:
        tp_path = root / tp_str
        for cf in _collect_ancestor_conftests(tp_path, root):
            conftests.add(cf)
        for f in _collect_all_py_files_under(tp_path, root):
            collected_files.append(f)
            for cf in _collect_ancestor_conftests(f, root):
                conftests.add(cf)
    return collected_files, conftests


def _validate_conftests_plugins(
    files: list[Path],
    root: Path,
    installed_modules: set[str],
    errors: list[str],
    job_name: str,
    step_name: str,
) -> bool:
    for cf in sorted(files):
        plugins, ok = _extract_pytest_plugins(cf)
        if not ok:
            errors.append(f"job '{job_name}' step '{step_name}': unanalysable pytest_plugins in '{cf}'")
            return False
        for pl in plugins:
            _check_pytest_plugin(pl, cf, root, installed_modules, errors, job_name, step_name)
    return True


def _validate_single_test_file_imports(
    tf: Path,
    root: Path,
    installed_modules: set[str],
    errors: list[str],
    job_name: str,
    step_name: str,
) -> bool:
    tp, err = _get_transitive_script_third_party_imports(tf, root)
    if err is not None:
        errors.append(f"job '{job_name}' step '{step_name}': {err}")
        return False
    for req in sorted(tp):
        stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
        if req not in installed_modules and req not in stdlib:
            errors.append(
                f"job '{job_name}' step '{step_name}': test file '{tf.relative_to(root)}' "
                f"imports '{req}' before it is installed"
            )
    return True


def _validate_test_file_imports(
    all_files: list[Path],
    root: Path,
    installed_modules: set[str],
    errors: list[str],
    job_name: str,
    step_name: str,
) -> None:
    for tf in all_files:
        if not _validate_single_test_file_imports(tf, root, installed_modules, errors, job_name, step_name):
            return


def _validate_pytest_execution(
    cmd: str,
    root: Path,
    step_name: str,
    job_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> None:
    if not _check_pytest_prerequisites(cmd, installed_modules, errors, job_name, step_name):
        return
    collected_files, conftests = _collect_all_test_and_conftest_files(root)
    all_files = sorted(set(collected_files) | conftests)
    if not _validate_conftests_plugins(all_files, root, installed_modules, errors, job_name, step_name):
        return
    _validate_test_file_imports(all_files, root, installed_modules, errors, job_name, step_name)


def _check_step_script_imports(
    script_token: str,
    root: Path,
    step_name: str,
    job_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> None:
    script_path = Path(script_token) if Path(script_token).is_absolute() else (root / script_token)
    if not script_path.exists():
        errors.append(f"job '{job_name}' step '{step_name}': cannot resolve script '{script_token}'")
        return
    third_party, err = _get_transitive_script_third_party_imports(script_path, root)
    if err is not None:
        errors.append(f"job '{job_name}' step '{step_name}': {err}")
        return
    for req in sorted(third_party):
        stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
        if req not in installed_modules and req not in stdlib:
            errors.append(
                f"job '{job_name}' step '{step_name}': script '{script_token}' imports '{req}' before it is installed"
            )


def _check_ruff_execution(
    root: Path,
    step_name: str,
    job_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> None:
    if "ruff" not in installed_modules:
        errors.append(f"job '{job_name}' step '{step_name}': executes 'ruff' before it is installed")
        return
    for cand in (root / "ruff.py", root / "ruff" / "__init__.py", root / "ruff" / "__main__.py"):
        if cand.is_file():
            _check_step_script_imports(str(cand), root, step_name, job_name, installed_modules, errors)


def _check_unittest_execution(
    root: Path,
    step_name: str,
    job_name: str,
    installed_modules: set[str],
    errors: list[str],
) -> None:
    for cand in (root / "unittest.py", root / "unittest" / "__init__.py", root / "unittest" / "__main__.py"):
        if cand.is_file():
            _check_step_script_imports(str(cand), root, step_name, job_name, installed_modules, errors)
    _scan_unittest_discover_imports(
        root,
        "skills/portfolio-delivery-control/tests",
        "*.py",
        job_name,
        step_name,
        installed_modules,
        errors,
    )


def _dispatch_pip_command(cmd: str, installed_modules: set[str]) -> None:
    if cmd == "pip install pyyaml==6.0.1":
        installed_modules.update({"yaml", "pyyaml"})
    elif cmd == "pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 pyyaml==6.0.1":
        installed_modules.update({"pytest", "pytest_cov", "ruff", "yaml", "pyyaml"})


def _dispatch_accepted_script(
    cmd: str,
    step_name: str,
    job_name: str,
    installed_modules: set[str],
    root: Path,
    errors: list[str],
) -> None:
    parts = cmd.split()
    script_token = parts[2] if parts[0] == "python3" else parts[1]
    _check_step_script_imports(script_token, root, step_name, job_name, installed_modules, errors)


def _dispatch_accepted_command(
    cmd: str,
    step_name: str,
    job_name: str,
    installed_modules: set[str],
    root: Path,
    errors: list[str],
) -> None:
    if cmd.startswith("pip install"):
        _dispatch_pip_command(cmd, installed_modules)
    elif cmd.startswith("python -m ruff"):
        _check_ruff_execution(root, step_name, job_name, installed_modules, errors)
    elif cmd.startswith("python -m unittest"):
        _check_unittest_execution(root, step_name, job_name, installed_modules, errors)
    elif cmd.startswith("python -m tests.test_coverage_threshold"):
        _check_step_script_imports(
            "tests/test_coverage_threshold.py", root, step_name, job_name, installed_modules, errors
        )
    elif cmd.startswith("pytest"):
        _validate_pytest_execution(cmd, root, step_name, job_name, installed_modules, errors)
    elif cmd.startswith(("python ", "python3 ")):
        _dispatch_accepted_script(cmd, step_name, job_name, installed_modules, root, errors)


def _check_raw_line_operators(raw_line: str) -> str | None:
    clean_line = strip_github_expressions(raw_line)
    unquoted = re.sub(r"'[^']*'", "''", clean_line)
    unquoted = re.sub(r'"[^"]*"', '""', unquoted)
    for op in ("||", "&&", "|", "&", ";", "<", ">"):
        if op in unquoted:
            return f"unanalysable shell operator {op!r}"
    return None


def _check_raw_line_shell_constructs(raw_line: str) -> str | None:
    if "\\" in raw_line:
        return "unanalysable shell construct: line continuation '\\' is not permitted"
    if "#" in raw_line:
        return "unanalysable shell construct: '#' in line"
    op_err = _check_raw_line_operators(raw_line)
    if op_err is not None:
        return op_err
    if "$(" in raw_line or "`" in raw_line:
        return "unanalysable shell construct: command substitution"
    stripped = raw_line.strip()
    if stripped.startswith(("export ", "env ")) or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
        return "unanalysable shell construct: environment prefix or command 'export'/'env' is not permitted"
    return None


def _process_single_raw_run_line(
    raw_line: str,
    step_name: str,
    job_name: str,
    installed_modules: set[str],
    root: Path,
    errors: list[str],
) -> bool:
    stripped = raw_line.strip()
    if not stripped:
        return True
    if stripped.startswith("#"):
        if raw_line.rstrip().endswith("\\"):
            errors.append(
                f"job '{job_name}' step '{step_name}': unanalysable shell construct: "
                f"line continuation '\\' is not permitted"
            )
            return False
        return True

    shell_err = _check_raw_line_shell_constructs(raw_line)
    if shell_err is not None:
        errors.append(f"job '{job_name}' step '{step_name}': {shell_err}")
        return False

    norm_line = " ".join(stripped.split())
    if norm_line not in ACCEPTED_COMMAND_SEGMENTS:
        errors.append(
            f"job '{job_name}' step '{step_name}': unanalysable step: command segment not in allowlist: {norm_line!r}"
        )
        return False

    _dispatch_accepted_command(norm_line, step_name, job_name, installed_modules, root, errors)
    return True


def _process_workflow_step_run(
    run_cmd: str,
    step_name: str,
    job_name: str,
    installed_modules: set[str],
    root: Path,
    errors: list[str],
) -> None:
    for raw_line in run_cmd.splitlines():
        if not _process_single_raw_run_line(raw_line, step_name, job_name, installed_modules, root, errors):
            return


def _validate_job_step_imports(
    job_name: str,
    job: dict[str, Any],
    root: Path,
    errors: list[str],
) -> None:
    installed_modules: set[str] = set()
    steps = job.get("steps", [])
    if not isinstance(steps, list):
        return
    for i, step in enumerate(steps):
        if isinstance(step, dict):
            step_name = step.get("name", f"step_{i}")
            run_cmd = step.get("run")
            if isinstance(run_cmd, str):
                _process_workflow_step_run(run_cmd, step_name, job_name, installed_modules, root, errors)


def validate_workflow_step_imports(
    workflow_yaml: dict[str, Any],
    repo_root: Path | None = None,
) -> list[str]:
    """Validate that all step commands and imported modules in workflow jobs are pre-installed.

    Known limitation: Dynamic module loading (e.g. importlib.import_module with non-literal
    variables, sys.modules manipulation, __import__ with dynamic arguments) inside accepted
    scripts cannot be resolved statically and is not supported.
    """
    root = repo_root or Path(__file__).resolve().parents[1]
    errors: list[str] = []
    jobs = workflow_yaml.get("jobs", {})
    if not isinstance(jobs, dict):
        return errors
    for job_name, job in jobs.items():
        if isinstance(job, dict):
            _validate_job_step_imports(job_name, job, root, errors)
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
    if isinstance(workflow_input, str | Path):
        p = Path(workflow_input)
        if p.is_file():
            errors.extend(check_no_tracked_coverage_json(p.resolve().parents[2]))
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
    errors.extend(validate_workflow_step_imports(doc))
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

    all_errors = list(skill_errors) + wf_errors + check_no_tracked_coverage_json(root)
    if all_errors:
        for err in all_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print(f"Validated {count} skills and quality workflow successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
