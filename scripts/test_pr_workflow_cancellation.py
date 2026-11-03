#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import re
import shlex
import subprocess
from typing import Any

import yaml

from tools.validate_skills import (
    is_coverage_check_tokens,
    validate_job_gates,
    validate_quality_job,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
EXPECTED_PR_GROUP = "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
EXPECTED_PR_CANCEL = "${{ github.event_name == 'pull_request' }}"

REQUIRED_JOB_IF_SNIPPETS = (
    "github.event.workflow_run.event == 'push'",
    "github.event.workflow_run.conclusion == 'success'",
    "github.event.workflow_run.head_branch == 'main'",
    "github.event.workflow_run.head_repository.full_name == github.repository",
    "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'",
)

REQUIRED_RELEASE_SCANNERS = (
    "privacy_scan.py",
    "test_privacy_scan.py",
    "security_scan.py",
    "test_security_scan.py",
    "public_api_stability.py",
    "test_pr_workflow_cancellation.py",
    "test_semantic_release.py",
)

CANONICAL_SCANNER_COMMANDS: dict[tuple[str, ...], str] = {
    cmd: scanner
    for scanner in REQUIRED_RELEASE_SCANNERS
    for cmd in (
        ("python", f"scripts/{scanner}"),
        ("python3", f"scripts/{scanner}"),
        ("python", "-B", f"scripts/{scanner}"),
        ("python3", "-B", f"scripts/{scanner}"),
    )
}

EXPECTED_DISPATCH_CLAUSES = {
    "github.event_name == 'workflow_dispatch'",
    "github.ref == 'refs/heads/main'",
}

EXPECTED_WORKFLOW_RUN_CLAUSES = {
    "github.event_name == 'workflow_run'",
    "github.event.workflow_run.event == 'push'",
    "github.event.workflow_run.conclusion == 'success'",
    "github.event.workflow_run.head_branch == 'main'",
    "github.event.workflow_run.head_repository.full_name == github.repository",
}

SHELL_KEYWORDS = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "case",
    "esac",
    "for",
    "while",
    "until",
    "select",
    "do",
    "done",
}

BANNED_SHELL_OPERATORS = (
    "||",
    "&&",
    ";",
    "|",
    "&",
    "`",
    "$(",
    ">",
    "<",
)
BANNED_PREFIXES = frozenset(
    {
        "true",
        ":",
        "!",
        "set",
        "shopt",
        "eval",
        "exec",
        "command",
        "builtin",
        "env",
        "if",
        "for",
        "while",
        "until",
        "case",
        "exit",
        "return",
        "echo",
        "printf",
    }
)
ALLOWED_GATE_EXECUTABLES = frozenset({"python", "python3", "pytest", "ruff", "git", "uvx", "pip", "go"})

EXPECTED_RUFF_FORMAT_ARGV = ["python", "-m", "ruff", "format", "--check", "--diff", "."]
EXPECTED_RUFF_LINT_ARGV = ["python", "-m", "ruff", "check", "."]
EXPECTED_RELEASE_GATE_ARGV = [
    "python3",
    "-B",
    "tools/release_gate.py",
    "--repo",
    "${{ github.repository }}",
    "--sha",
    "${{ github.event.workflow_run.head_sha || github.sha }}",
    "--expected-app-slug",
    "github-actions",
    "--check-suite-id",
    "${{ github.event.workflow_run.check_suite_id }}",
    "--wait-seconds",
    "600",
    "--poll-interval-seconds",
    "10",
]
EXPECTED_ANCESTOR_ARGV = [
    "git",
    "merge-base",
    "--is-ancestor",
    "${{ github.event.workflow_run.head_sha || github.sha }}",
    "origin/main",
]

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


def validate_env_configuration(
    scope_name: str,
    mapping: dict[str, Any],
    allowed_keys: frozenset[str] = frozenset(),
) -> None:
    """Ensure environment mappings do not contain bypass variables or unallowlisted keys."""
    env = mapping.get("env")
    if env is None:
        return
    assert isinstance(env, dict), f"{scope_name} env must be a mapping"
    if not allowed_keys:
        assert not env, f"{scope_name} must not declare env mapping: {env}"
    for key in env.keys():
        k_str = str(key)
        assert k_str in allowed_keys, f"{scope_name} has disallowed env variable {k_str!r}"
        if k_str != "COVERAGE_SESSION_ID":
            assert not any(k_str == prefix or k_str.startswith(f"{prefix}_") for prefix in BANNED_ENV_PREFIXES), (
                f"{scope_name} has banned env variable {k_str!r}"
            )


def _has_pip_install(line: str) -> bool:
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError:
        return False
    return is_pip_install_tokens(tokens)


def _check_mapping_workdir(entity_name: str, mapping: dict[str, Any]) -> None:
    wd = mapping.get("working-directory") or mapping.get("working_directory")
    if wd:
        if wd == "tools/hclblocks" and mapping.get("run", "").strip() == "go vet ./...":
            return
        assert False, f"{entity_name} specifies disallowed working-directory override"


def _check_mapping_shell(entity_name: str, shell_val: Any) -> None:
    if shell_val is not None:
        assert shell_val == "bash -eo pipefail", (
            f"{entity_name} specifies illegal shell override: {shell_val!r} (must be omitted or 'bash -eo pipefail')"
        )


def _validate_defaults_configuration(entity_name: str, defaults: Any) -> None:
    if defaults is None:
        return
    assert isinstance(defaults, dict), f"{entity_name} specifies disallowed defaults override"
    _check_mapping_workdir(f"{entity_name} defaults", defaults)
    run_def = defaults.get("run")
    if isinstance(run_def, dict):
        _check_mapping_shell(f"{entity_name} defaults.run.shell", run_def.get("shell"))
        _check_mapping_workdir(f"{entity_name} defaults.run", run_def)


def validate_shell_configuration(entity_name: str, mapping: dict[str, Any]) -> None:
    """Reject custom shell overrides and working-directory overrides."""
    if not isinstance(mapping, dict):
        return
    _check_mapping_workdir(entity_name, mapping)
    _check_mapping_shell(entity_name, mapping.get("shell"))
    _validate_defaults_configuration(entity_name, mapping.get("defaults"))


def parse_and_validate_gate_command_lines(step_name: str, run_cmd: str) -> list[list[str]]:
    """Parse command lines with shlex.split(line, comments=True) and enforce positive canonical validation."""
    parsed_commands: list[list[str]] = []
    raw_lines = [line.strip() for line in run_cmd.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert raw_lines, f"step {step_name!r} has no active command lines"

    for raw_line in raw_lines:
        stripped_line = _strip_github_expressions(raw_line)
        for op in BANNED_SHELL_OPERATORS:
            assert op not in stripped_line, (
                f"step {step_name!r} line contains banned shell operator {op!r}: {raw_line!r}"
            )

        tokens = shlex.split(raw_line, comments=True)
        assert tokens, f"step {step_name!r} produced empty tokens from: {raw_line!r}"

        head = tokens[0]
        assert head not in BANNED_PREFIXES, (
            f"step {step_name!r} command starts with banned prefix/executable {head!r}: {raw_line!r}"
        )
        assert head in ALLOWED_GATE_EXECUTABLES, (
            f"step {step_name!r} command starts with disallowed executable {head!r}: {raw_line!r}"
        )
        parsed_commands.append(tokens)

    return parsed_commands


def _parse_int_safe(val: str) -> int | None:
    try:
        return int(val)
    except ValueError:
        return None


def _match_cov_fail_under_arg(i: int, token: str, tokens: list[str]) -> int | None:
    if token.startswith("--cov-fail-under="):
        return _parse_int_safe(token.split("=", 1)[1])
    if token == "--cov-fail-under" and i + 1 < len(tokens):
        return _parse_int_safe(tokens[i + 1])
    return None


def _extract_cov_fail_under(tokens: list[str]) -> int | None:
    for i, token in enumerate(tokens):
        val = _match_cov_fail_under_arg(i, token, tokens)
        if val is not None:
            return val
    return None


def validate_pytest_coverage_command(tokens: list[str], step_name: str) -> None:
    """Validate pytest invocation includes --cov=tools, --cov-branch, and --cov-fail-under >= 90."""
    assert tokens and tokens[0] == "pytest", f"expected pytest command in {step_name!r}, got {tokens}"
    assert "--cov=tools" in tokens, f"pytest command in {step_name!r} missing '--cov=tools'"
    assert "--cov-branch" in tokens, f"pytest command in {step_name!r} missing '--cov-branch'"
    assert "--cov-report=json:coverage.json" in tokens, (
        f"pytest command in {step_name!r} missing '--cov-report=json:coverage.json'"
    )
    cov_fail_under = _extract_cov_fail_under(tokens)
    assert cov_fail_under is not None, f"pytest command in {step_name!r} missing --cov-fail-under option"
    assert cov_fail_under >= 90, f"pytest command in {step_name!r} coverage floor {cov_fail_under} < 90"


def _strip_github_expressions(text: str) -> str:
    """Remove ${{ ... }} expressions to inspect underlying shell commands."""
    return re.sub(r"\$\{\{.*?\}\}", "__GH_EXPR__", text)


def _has_shell_suppression_or_bypass(line: str) -> bool:
    """Check if a shell command line suppresses failure, runs conditionally, or bypasses execution."""
    code_part = line.split("#")[0].strip()
    if not code_part:
        return False
    if code_part in ("true", "exit 0", ":", "return 0"):
        return True
    stripped = _strip_github_expressions(code_part)
    tokens = stripped.split()
    if any(t.strip(";()") in SHELL_KEYWORDS for t in tokens):
        return True
    for op in ("||", "|", ";", "&", "`", "$("):
        if op in stripped:
            return True
    if tokens and tokens[0] in ("echo", "printf"):
        return True
    return False


def _validate_job_level_if(job_name: str, job: dict[str, Any]) -> None:
    job_if = str(job.get("if", "")).strip()
    assert job_if, f"'{job_name}' job must declare an if: condition"
    assert not re.search(r"\b(true|always\(\)|success\(\)|failure\(\)|cancelled\(\))\b", job_if), (
        f"'{job_name}' job if condition contains illegal bypass expression: {job_if!r}"
    )
    raw_branches = [b.strip() for b in job_if.split("||")]
    cleaned_branches: list[str] = []
    for b in raw_branches:
        cb = b.strip()
        while cb.startswith("(") and cb.endswith(")"):
            cb = cb[1:-1].strip()
        cleaned_branches.append(cb)

    assert len(cleaned_branches) == 2, (
        f"'{job_name}' job if condition must have exactly 2 top-level branches, got {len(cleaned_branches)}: {job_if!r}"
    )

    branch_clause_sets = [{re.sub(r"\s+", " ", c.strip()) for c in b.split("&&")} for b in cleaned_branches]

    has_dispatch = any(bc == EXPECTED_DISPATCH_CLAUSES for bc in branch_clause_sets)
    has_run = any(bc == EXPECTED_WORKFLOW_RUN_CLAUSES for bc in branch_clause_sets)

    assert has_dispatch, (
        f"'{job_name}' job if condition missing required clause in workflow_dispatch branch: {job_if!r}"
    )
    assert has_run, f"'{job_name}' job if condition missing required clause in workflow_run branch: {job_if!r}"


def workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])


def get_on_triggers(doc: dict[str, Any]) -> list[str] | dict[str, Any]:
    # In YAML 1.1, unquoted 'on' is parsed as boolean True
    raw_doc: dict[Any, Any] = doc
    triggers = raw_doc.get("on")
    if triggers is None:
        triggers = raw_doc.get(True)
    if isinstance(triggers, (list, dict)):
        return triggers
    return {}


def triggers_pull_request(doc: dict[str, Any]) -> bool:
    triggers = get_on_triggers(doc)
    if isinstance(triggers, dict):
        return "pull_request" in triggers
    if isinstance(triggers, list):
        return "pull_request" in triggers
    if isinstance(triggers, str):
        return triggers == "pull_request"
    return False


def _validate_command_lines(path_name: str, job_name: str, step_name: str, run_cmd: str) -> None:
    assert "set +e" not in run_cmd, f"{path_name} job {job_name} step {step_name!r} has set +e"
    assert "set +o errexit" not in run_cmd, f"{path_name} job {job_name} step {step_name!r} has set +o errexit"
    active_lines = [line.split("#")[0].strip() for line in run_cmd.splitlines() if line.split("#")[0].strip()]
    assert len(active_lines) > 0, (
        f"{path_name} job {job_name} step {step_name!r} has empty or fully commented run command"
    )
    for line in active_lines:
        assert not _has_shell_suppression_or_bypass(line), (
            f"{path_name} job {job_name} step {step_name!r} masks failure / "
            f"masks command failure or uses shell conditional: {line!r}"
        )


def _is_gate_step(step_name: str, run_cmd: str) -> bool:
    if "pip install" in run_cmd or step_name == "Install dependencies":
        return False
    return any(k in run_cmd for k in ("tools/release_gate.py", "format", "check", "git merge-base", "pytest"))


def _validate_pytest_step(step_name: str, argvs: list[list[str]]) -> None:
    pytest_cmds = [argv for argv in argvs if argv[0] == "pytest"]
    assert pytest_cmds, f"{step_name!r} missing pytest command"
    validate_pytest_coverage_command(pytest_cmds[0], step_name)


def _validate_gate_step_argvs(step_name: str, run_cmd: str) -> None:
    if not _is_gate_step(step_name, run_cmd):
        return

    argvs = parse_and_validate_gate_command_lines(step_name, run_cmd)
    if "tools/release_gate.py" in run_cmd:
        assert EXPECTED_RELEASE_GATE_ARGV in argvs, f"{step_name!r} missing expected release gate command"
    if "format" in run_cmd and "ruff" in run_cmd:
        assert EXPECTED_RUFF_FORMAT_ARGV in argvs, f"{step_name!r} missing expected ruff format gate command"
    if "check" in run_cmd and "ruff" in run_cmd and "format" not in run_cmd:
        assert EXPECTED_RUFF_LINT_ARGV in argvs, f"{step_name!r} missing expected ruff lint gate command"
    if "git merge-base" in run_cmd:
        assert EXPECTED_ANCESTOR_ARGV in argvs, f"{step_name!r} missing expected ancestor check command"
    if "pytest" in run_cmd:
        _validate_pytest_step(step_name, argvs)


def _check_pip_tokens_contract(
    path_name: str,
    job_name: str,
    step_name: str,
    tokens: list[str],
) -> None:
    for _pkg_name, expected_token in PINNED_TOOL_REQUIREMENTS.items():
        assert any(t == expected_token for t in tokens), (
            f"{path_name} job {job_name} primary install missing {expected_token!r}"
        )
    for token in tokens:
        pkg = re.split(r"[=<>!~]", token)[0].strip()
        assert pkg not in DISALLOWED_PIP_PACKAGES, (
            f"{path_name} job {job_name} step {step_name!r} contains disallowed pip package {pkg!r}"
        )
        if pkg in GATED_PACKAGES:
            expected = PINNED_TOOL_REQUIREMENTS.get(pkg)
            assert expected is not None and token == expected, (
                f"{path_name} job {job_name} step {step_name!r} has disallowed pin for {pkg!r}: {token!r}"
            )


def _validate_step_pip_lines(
    path_name: str,
    job_name: str,
    step_name: str,
    step: dict[str, Any],
    primary_pip_step: dict[str, Any] | None,
    run_cmd: str,
) -> None:
    for line in run_cmd.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        try:
            tokens = shlex.split(trimmed, comments=True)
        except ValueError:
            continue
        if is_pip_install_tokens(tokens):
            assert step is primary_pip_step, (
                f"{path_name} job {job_name} step {step_name!r} has disallowed secondary pip install: {tokens}"
            )
            _check_pip_tokens_contract(path_name, job_name, step_name, tokens)


def _check_step_coe(path_name: str, job_name: str, step_name: str, step: dict[str, Any]) -> None:
    coe = step.get("continue-on-error")
    if coe is not None:
        coe_str = str(coe).strip().lower()
        assert coe is False or coe_str in ("false", "${{ false }}", '"false"', "'false'"), (
            f"{path_name} job {job_name} step {step_name!r} must not have continue-on-error: {coe_str}"
        )


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


def _is_valid_quality_script_invocation(run_cmd: str, script_name: str) -> bool:
    for raw_line in run_cmd.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        if CANONICAL_QUALITY_JOB_COMMANDS.get(tuple(tokens)) == script_name:
            return True
    return False


def _is_pr_title_tokens(tokens: list[str]) -> bool:
    if len(tokens) < 2 or tokens[0] not in ("python", "python3"):
        return False
    target = tokens[2] if len(tokens) > 2 and tokens[1] == "-B" else tokens[1]
    return target in ("scripts/validate_pr_title.py", "validate_pr_title.py")


def _invokes_pr_title_script(run_cmd: str) -> bool:
    for raw_line in run_cmd.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        if _is_pr_title_tokens(tokens):
            return True
    return False


def _is_pr_title_step(step_name: str, run_cmd: str) -> bool:
    if any(s in run_cmd or s in step_name for s in MANDATORY_QUALITY_JOB_SCRIPTS):
        return False
    return _invokes_pr_title_script(run_cmd)


def _is_unconditional_step(job_name: str, step_name: str, run_cmd: str) -> bool:
    if (
        job_name in ("tests", "verify")
        or "tools/release_gate.py" in run_cmd
        or ("ruff" in run_cmd and ("format" in run_cmd or "check" in run_cmd))
        or "git merge-base" in run_cmd
        or "pytest" in run_cmd
        or "tools/validate_skills.py" in run_cmd
        or "tools/validate_marketplace.py" in run_cmd
        or "tools/tf_module_layout.py" in run_cmd
        or "unittest discover" in run_cmd
    ):
        return True
    if any(s in run_cmd or s in step_name for s in MANDATORY_QUALITY_JOB_SCRIPTS):
        return True
    if job_name == "quality" and not _is_pr_title_step(step_name, run_cmd):
        return True
    return False


def _check_step_if(path_name: str, job_name: str, step_name: str, step: dict[str, Any], run_cmd: str) -> None:
    step_if = step.get("if")
    if step_if is not None:
        step_if_str = str(step_if).strip().lower()
        assert step_if is not False and step_if_str not in ("false", "${{ false }}", '"false"', "'false'"), (
            f"{path_name} job {job_name} step {step_name!r} must not have if: false"
        )
    if _is_unconditional_step(job_name, step_name, run_cmd):
        assert step_if is None or not str(step_if).strip(), (
            f"{path_name} job {job_name} step {step_name!r} must be unconditional"
        )
    elif job_name == "quality" and _is_pr_title_step(step_name, run_cmd):
        if step_if is not None:
            assert "pull_request" in str(step_if), (
                f"{path_name} job {job_name} step {step_name!r} if: condition must reference pull_request"
            )


def _get_tests_or_verify_env(job_name: str, step_name: str, run_cmd: str) -> frozenset[str]:
    if job_name == "verify" and ("tools/release_gate.py" in run_cmd or step_name == "Enforce Release Gate"):
        return frozenset({"GH_TOKEN", "GITHUB_TOKEN"})
    if "go vet" in run_cmd:
        return frozenset({"GOTOOLCHAIN", "GOWORK", "GOFLAGS"})
    if any(k in run_cmd for k in ("pytest", "test_coverage_threshold")):
        return frozenset({"COVERAGE_SESSION_ID"})
    return frozenset()


def _check_step_env(path_name: str, job_name: str, step_name: str, step: dict[str, Any], run_cmd: str) -> None:
    if job_name in ("tests", "verify"):
        allowed = _get_tests_or_verify_env(job_name, step_name, run_cmd)
        validate_env_configuration(f"{path_name} job {job_name} step {step_name!r}", step, allowed_keys=allowed)
    elif job_name == "quality":
        allowed = frozenset({"PR_TITLE", "PR_BODY"}) if _is_pr_title_step(step_name, run_cmd) else frozenset()
        validate_env_configuration(f"{path_name} job {job_name} step {step_name!r}", step, allowed_keys=allowed)


def _check_step_uses_inputs(
    path_name: str,
    job_name: str,
    step_name: str,
    step: dict[str, Any],
    uses: Any,
) -> None:
    if "quality" not in path_name or uses is None:
        return
    if uses == "actions/checkout@v7.0.0":
        assert step.get("with") is None, (
            f"{path_name} job {job_name} step {step_name!r} disallowed 'with:' on "
            f"actions/checkout: {step.get('with')!r}"
        )
    elif uses == "actions/setup-python@v6.3.0":
        assert step.get("with") == {"python-version": "3.12"}, (
            f"{path_name} job {job_name} step {step_name!r} invalid setup-python 'with:': "
            f"expected {{'python-version': '3.12'}}, got {step.get('with')!r}"
        )
    elif str(uses).startswith("actions/setup-go@") or _is_pinned_action(uses, "actions/setup-go"):
        assert _is_pinned_action(uses, "actions/setup-go"), (
            f"{path_name} job {job_name} step {step_name!r} setup-go must be pinned by commit SHA: {uses!r}"
        )
        assert step.get("with") == {"go-version": "1.26.10"}, (
            f"{path_name} job {job_name} step {step_name!r} invalid setup-go 'with:': "
            f"expected {{'go-version': '1.26.10'}}, got {step.get('with')!r}"
        )


def _validate_step(
    path_name: str,
    job_name: str,
    step: dict[str, Any],
    primary_pip_step: dict[str, Any] | None = None,
) -> None:
    step_name = step.get("name", step.get("id", "unnamed step"))
    validate_shell_configuration(f"{path_name} job {job_name} step {step_name!r}", step)
    run_cmd = step.get("run", "")
    if "go vet" in run_cmd:
        assert step.get("working-directory") == "tools/hclblocks", (
            f"{path_name} job {job_name} step {step_name!r} go vet must specify working-directory: tools/hclblocks"
        )
        assert step.get("env") == {
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOFLAGS": "-mod=readonly",
        }, f"{path_name} job {job_name} step {step_name!r} go vet env must specify local, off, -mod=readonly"
    _check_step_coe(path_name, job_name, step_name, step)
    _check_step_if(path_name, job_name, step_name, step, run_cmd)
    _check_step_env(path_name, job_name, step_name, step, run_cmd)
    _check_step_uses_inputs(path_name, job_name, str(step_name), step, step.get("uses"))
    if run_cmd:
        _validate_command_lines(path_name, job_name, step_name, run_cmd)
        _validate_gate_step_argvs(step_name, run_cmd)
        if job_name in ("tests", "verify"):
            _validate_step_pip_lines(path_name, job_name, step_name, step, primary_pip_step, run_cmd)


def _check_job_flags(path_name: str, job_name: str, job: dict[str, Any]) -> None:
    if "quality" in path_name and (job_name in ("quality", "tests", "test") or "Quality" in str(job.get("name", ""))):
        assert job.get("if") is None, f"{path_name} required job '{job_name}' must not have an if: condition"
    job_coe = job.get("continue-on-error")
    if job_coe is not None:
        job_coe_str = str(job_coe).strip().lower()
        assert job_coe is False or job_coe_str in ("false", "${{ false }}", '"false"', "'false'"), (
            f"{path_name} job {job_name} must not have continue-on-error: {job_coe_str}"
        )
    job_if = job.get("if")
    if job_if is not None:
        job_if_str = str(job_if).strip().lower()
        assert job_if is not False and job_if_str not in ("false", "${{ false }}", '"false"', "'false'"), (
            f"{path_name} job {job_name} must not have if: false"
        )


def _find_job_primary_pip_step(path_name: str, job_name: str, job: dict[str, Any]) -> dict[str, Any] | None:
    if job_name not in ("tests", "verify"):
        return None
    pip_steps = [
        s
        for s in job.get("steps", [])
        if isinstance(s, dict)
        and any(
            _has_pip_install(line)
            for line in s.get("run", "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    ]
    assert len(pip_steps) <= 1, f"{path_name} job {job_name} has {len(pip_steps)} pip install steps; at most 1 allowed"
    return pip_steps[0] if pip_steps else None


def _step_is_unconditional_non_coe(step: dict[str, Any]) -> bool:
    if step.get("if") is not None:
        return False
    coe = step.get("continue-on-error")
    return not (coe is not None and coe is not False and str(coe).strip().lower() not in ("false", "${{ false }}"))


def _scan_step_for_quality_scripts(run_cmd: str, found: set[str]) -> None:
    for script in MANDATORY_QUALITY_JOB_SCRIPTS:
        if _is_valid_quality_script_invocation(run_cmd, script):
            found.add(script)


def _collect_quality_scripts(steps: list[Any]) -> set[str]:
    found: set[str] = set()
    for s in steps:
        if isinstance(s, dict) and _step_is_unconditional_non_coe(s):
            _scan_step_for_quality_scripts(s.get("run", ""), found)
    return found


ALLOWED_JOB_LEVEL_KEYS: frozenset[str] = frozenset({"name", "runs-on", "steps", "permissions", "timeout-minutes"})


def _validate_job_level_keys(path_name: str, job_name: str, job: dict[str, Any]) -> None:
    disallowed = sorted(set(job.keys()) - ALLOWED_JOB_LEVEL_KEYS)
    assert not disallowed, f"{path_name} job '{job_name}' contains disallowed job-level keys: {', '.join(disallowed)}"
    assert job.get("runs-on") == "ubuntu-latest", (
        f"{path_name} job '{job_name}' runs-on must be 'ubuntu-latest', got: {job.get('runs-on')!r}"
    )
    if "timeout-minutes" in job:
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and not isinstance(timeout, bool) and 5 <= timeout <= 60, (
            f"{path_name} job '{job_name}' timeout-minutes must be an integer between 5 and 60, got: {timeout!r}"
        )
    if "permissions" in job:
        perms = job.get("permissions")
        assert perms == {"contents": "read"}, (
            f"{path_name} job '{job_name}' permissions must be {{'contents': 'read'}}, got: {perms!r}"
        )


def _check_quality_job_content(path_name: str, job_name: str, job: dict[str, Any]) -> None:
    if "quality" not in path_name or job_name != "quality":
        return
    quality_errs = validate_quality_job(job, "quality")
    assert not quality_errs, f"{path_name} quality job failed gate validation: {quality_errs}"
    quality_steps = job.get("steps", [])
    found_scripts = _collect_quality_scripts(quality_steps)
    for script in MANDATORY_QUALITY_JOB_SCRIPTS:
        assert script in found_scripts, f"{path_name} quality job missing required script: {script}"


def _validate_job(path_name: str, job_name: str, job: dict[str, Any]) -> None:
    validate_shell_configuration(f"{path_name} job {job_name}", job)
    if job_name in ("quality", "tests", "verify"):
        validate_env_configuration(f"{path_name} job '{job_name}'", job, allowed_keys=frozenset())
    _check_job_flags(path_name, job_name, job)
    if "quality" in path_name and (job_name in ("quality", "tests", "test") or "Quality" in str(job.get("name", ""))):
        _validate_job_level_keys(path_name, job_name, job)
    primary_pip_step = _find_job_primary_pip_step(path_name, job_name, job)
    for step in job.get("steps", []):
        if isinstance(step, dict):
            _validate_step(path_name, job_name, step, primary_pip_step)
    _check_quality_job_content(path_name, job_name, job)


def _validate_quality_workflow_jobs(path_name: str, jobs: dict[str, Any]) -> None:
    assert isinstance(jobs.get("quality"), dict), f"{path_name} missing required 'quality' job"
    tests_job = jobs.get("tests") or jobs.get("test")
    assert isinstance(tests_job, dict), f"{path_name} missing required 'tests' job"
    extra_jobs = set(jobs.keys()) - {"quality", "tests", "test"}
    assert not extra_jobs, (
        f"{path_name} jobs must contain only 'quality' and 'tests', got unexpected: {sorted(extra_jobs)}"
    )
    gate_errors = validate_job_gates(tests_job, "tests")
    assert not gate_errors, f"{path_name} tests job failed gate validation: {gate_errors}"


def _validate_release_workflow_jobs(path_name: str, jobs: dict[str, Any]) -> None:
    verify = jobs.get("verify")
    if isinstance(verify, dict):
        gate_errors = validate_job_gates(verify, "verify")
        assert not gate_errors, f"{path_name} verify job failed gate validation: {gate_errors}"
        _validate_release_verify_job(verify)


def validate_workflow_steps(path_name: str, doc: dict[str, Any]) -> None:
    """Validate that jobs and steps are effective and do not bypass execution."""
    assert isinstance(doc, dict), f"{path_name} must be a mapping"
    validate_shell_configuration(f"{path_name} workflow", doc)
    validate_env_configuration(f"{path_name} workflow", doc, allowed_keys=frozenset())
    jobs = doc.get("jobs", {})
    assert isinstance(jobs, dict), f"{path_name} jobs must be a mapping"
    for job_name, job in jobs.items():
        if isinstance(job, dict):
            _validate_job(path_name, job_name, job)
    if "quality" in path_name:
        wf_perms = doc.get("permissions")
        assert wf_perms == {"contents": "read"}, (
            f"{path_name} permissions must be {{'contents': 'read'}}, got: {wf_perms!r}"
        )
        _validate_quality_workflow_jobs(path_name, jobs)
    elif "release" in path_name:
        _validate_release_workflow_jobs(path_name, jobs)


def test_pull_request_workflows_cancel_obsolete_runs() -> None:
    checked: list[str] = []
    for path in workflow_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not triggers_pull_request(doc):
            continue

        checked.append(path.name)
        concurrency = doc.get("concurrency")
        assert concurrency is not None, f"{path.name} must declare concurrency"
        assert isinstance(concurrency, dict), f"{path.name} concurrency must be a mapping"
        assert concurrency.get("group") == EXPECTED_PR_GROUP, (
            f"{path.name} must group PR runs by workflow and PR number"
        )
        assert concurrency.get("cancel-in-progress") == EXPECTED_PR_CANCEL, (
            f"{path.name} must cancel only pull_request runs"
        )
        validate_workflow_steps(path.name, doc)

    assert checked == ["diagram-freshness.yml", "quality.yaml", "secret-scan.yml", "vulture-check.yml"]


def _validate_release_triggers(triggers: dict[str, Any]) -> None:
    assert isinstance(triggers, dict), "release workflow triggers must be a mapping"
    assert "workflow_run" in triggers, "release workflow must declare workflow_run trigger"
    wf_run = triggers["workflow_run"]
    assert wf_run.get("workflows") == ["Quality"], "workflow_run must trigger on Quality workflow"
    assert wf_run.get("types") == ["completed"], "workflow_run must trigger on completed"
    assert wf_run.get("branches") == ["main"], "workflow_run must trigger on branches: [main]"
    assert "workflow_dispatch" in triggers, "release workflow must declare workflow_dispatch trigger"
    assert "push" not in triggers and "pull_request" not in triggers, (
        "release workflow must not trigger on push or pull_request"
    )


def _validate_release_gate_step_command(gate_step: dict[str, Any]) -> None:
    validate_shell_configuration("release gate step", gate_step)
    assert gate_step.get("if") is None or not str(gate_step.get("if")).strip(), (
        "release gate step must be unconditional"
    )
    assert gate_step.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
        "release gate step must not specify continue-on-error"
    )
    run_cmd = gate_step.get("run", "")
    _validate_command_lines("release.yaml", "verify", "release gate", run_cmd)
    argvs = parse_and_validate_gate_command_lines("release gate", run_cmd)
    assert len(argvs) == 1, "release gate step must contain exactly 1 gate command line"
    assert argvs[0] == EXPECTED_RELEASE_GATE_ARGV, (
        f"release gate command does not match expected exact argv:\n"
        f"Expected: {EXPECTED_RELEASE_GATE_ARGV}\nGot: {argvs[0]}"
    )


def _validate_verify_permissions(verify: dict[str, Any]) -> None:
    assert "permissions" in verify, "verify job must declare permissions"
    verify_perms = verify["permissions"]
    assert isinstance(verify_perms, dict), "verify job must declare permissions mapping"
    assert verify_perms == {"contents": "read", "checks": "read"}, (
        f"verify job permissions must be exactly {{'contents': 'read', 'checks': 'read'}}, got: {verify_perms!r}"
    )


def _validate_verify_checkout(verify_steps: list[Any]) -> None:
    checkout_step = next(s for s in verify_steps if "checkout" in str(s.get("uses", "")))
    with_clause = checkout_step.get("with", {})
    assert with_clause.get("fetch-depth") == 0, "verify job checkout must specify fetch-depth: 0"
    assert with_clause.get("ref") == "${{ github.event.workflow_run.head_sha || github.sha }}", (
        "verify job checkout must use exact ref expression"
    )


def _validate_verify_ancestor(verify_steps: list[Any]) -> None:
    ancestor_step = next(
        s
        for s in verify_steps
        if 'git merge-base --is-ancestor "${{ github.event.workflow_run.head_sha || github.sha }}" origin/main'
        in s.get("run", "")
    )
    assert ancestor_step is not None, "verify job must check commit is ancestor of origin/main"
    assert ancestor_step.get("if") is None or not str(ancestor_step.get("if")).strip(), (
        "ancestor check step must be unconditional"
    )
    assert ancestor_step.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
        "ancestor check step must not specify continue-on-error"
    )
    _validate_command_lines("release.yaml", "verify", "ancestor check", ancestor_step.get("run", ""))


def _validate_verify_go_steps(verify_steps: list[Any]) -> None:
    parser_steps = [
        s for s in verify_steps if isinstance(s, dict) and "scripts/install_hcl_parser.py" in s.get("run", "")
    ]
    assert len(parser_steps) == 0, "verify job must not run scripts/install_hcl_parser.py"
    setup_go_steps = [
        s for s in verify_steps if isinstance(s, dict) and _is_pinned_action(s.get("uses"), "actions/setup-go")
    ]
    assert len(setup_go_steps) == 1, "verify job must use pinned actions/setup-go"
    assert setup_go_steps[0].get("with", {}).get("go-version") == "1.26.10", (
        "verify job actions/setup-go must specify go-version: '1.26.10'"
    )
    go_vet_steps = [s for s in verify_steps if isinstance(s, dict) and "go vet" in s.get("run", "")]
    assert len(go_vet_steps) == 1, "verify job must run go vet"
    assert go_vet_steps[0].get("working-directory") == "tools/hclblocks", (
        "verify job go vet step must specify working-directory: tools/hclblocks"
    )
    assert go_vet_steps[0].get("env") == {
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOFLAGS": "-mod=readonly",
    }, "verify job go vet step env must specify GOTOOLCHAIN=local, GOWORK=off, GOFLAGS=-mod=readonly"


def _validate_verify_pip_steps(verify_steps: list[Any]) -> None:
    pip_steps = [
        s
        for s in verify_steps
        if isinstance(s, dict)
        and any(
            _has_pip_install(line)
            for line in s.get("run", "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    ]
    assert len(pip_steps) == 1, f"verify job has {len(pip_steps)} pip install steps; exactly 1 required"
    assert "pytest==8.3.4" in pip_steps[0].get("run", ""), "verify job must pin pytest==8.3.4"
    assert "pytest-cov==6.0.0" in pip_steps[0].get("run", ""), "verify job must pin pytest-cov==6.0.0"
    assert "ruff==0.15.21" in pip_steps[0].get("run", ""), "verify job must pin ruff==0.15.21"
    _validate_verify_go_steps(verify_steps)


def _find_suite_step_coverage_indices(lines: list[str]) -> tuple[int, int]:
    p_idx = -1
    v_idx = -1
    for idx, line in enumerate(lines):
        try:
            tokens = shlex.split(line, comments=True) if line and not line.startswith("#") else []
        except ValueError:
            tokens = []
        if tokens and tokens[0] == "pytest":
            p_idx = idx
        elif is_coverage_check_tokens(tokens):
            v_idx = idx
    return p_idx, v_idx


def _validate_suite_step_coverage_order(verify_steps: list[Any]) -> None:
    suite_step = next((s for s in verify_steps if isinstance(s, dict) and "lint_skills.py" in s.get("run", "")), None)
    assert suite_step is not None, "release verify job must contain a step running lint_skills.py"
    run_cmd = suite_step.get("run", "")
    lines = [line.strip() for line in run_cmd.splitlines() if line.strip() and not line.strip().startswith("#")]
    p_idx, v_idx = _find_suite_step_coverage_indices(lines)
    assert p_idx != -1, "release verify job suite step must run pytest"
    assert v_idx != -1, "release verify job suite step must run test_coverage_threshold"
    assert v_idx == p_idx + 1, "release verify job suite step: coverage threshold must immediately follow pytest"


def _validate_release_verify_job(verify: dict[str, Any]) -> None:
    validate_shell_configuration("verify job", verify)
    validate_env_configuration("verify job", verify, allowed_keys=frozenset())
    _validate_job_level_if("verify", verify)
    _validate_verify_permissions(verify)

    verify_steps = verify.get("steps", [])
    _validate_verify_checkout(verify_steps)
    _validate_verify_ancestor(verify_steps)
    _validate_verify_pip_steps(verify_steps)

    gate_step = next(s for s in verify_steps if "tools/release_gate.py" in s.get("run", ""))
    _validate_release_gate_step_command(gate_step)
    _validate_suite_step_coverage_order(verify_steps)

    found_scanners = _collect_scanner_commands(verify_steps)
    for scanner in REQUIRED_RELEASE_SCANNERS:
        assert scanner in found_scanners, f"verify job missing required scanner: {scanner}"


def _scan_lines_for_release_scanners(run_cmd: str, found_scanners: set[str]) -> None:
    for raw_line in run_cmd.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        scanner = CANONICAL_SCANNER_COMMANDS.get(tuple(tokens))
        if scanner:
            found_scanners.add(scanner)


def _collect_scanner_commands(verify_steps: list[Any]) -> set[str]:
    found_scanners: set[str] = set()
    for step in verify_steps:
        if isinstance(step, dict) and step.get("run"):
            _scan_lines_for_release_scanners(step["run"], found_scanners)
    return found_scanners


def _is_pinned_action(uses_val: Any, action_name: str) -> bool:
    if not isinstance(uses_val, str):
        return False
    val = uses_val.strip()
    return bool(re.fullmatch(rf"{re.escape(action_name)}@([0-9a-fA-F]{{40}})", val))


def _validate_release_publish_step_0(s0: Any) -> None:
    assert isinstance(s0, dict) and _is_pinned_action(s0.get("uses"), "actions/checkout"), (
        "release job step 0 must use pinned actions/checkout"
    )
    expected_with = {
        "ref": "${{ github.event.workflow_run.head_sha || github.sha }}",
        "fetch-depth": 0,
    }
    assert s0.get("with") == expected_with, "release job checkout must specify exact fetch-depth and ref"
    assert "env" not in s0, "release job step 0 must not declare env"


def _validate_release_publish_step_1(s1: Any) -> None:
    assert isinstance(s1, dict) and _is_pinned_action(s1.get("uses"), "actions/setup-python"), (
        "release job step 1 must use pinned actions/setup-python"
    )
    assert s1.get("with") == {"python-version": "3.12"}, "setup-python must use python-version: 3.12"
    assert "env" not in s1, "release job step 1 must not declare env"


def _validate_release_publish_step_2(s2: Any) -> None:
    expected_cmd = "python scripts/semantic_release.py --notes-file release-notes.md"
    assert isinstance(s2, dict), "release job step 2 must be mapping"
    assert s2.get("name") == "Plan release" and s2.get("id") == "plan", "release job step 2 must be Plan release"
    assert str(s2.get("run", "")).strip() == expected_cmd, "release job step 2 must run exact semantic_release.py"
    assert "env" not in s2, "release job step 2 must not declare env"


def _validate_release_publish_step_3(s3: Any) -> None:
    canonical_lines = [
        'git config user.name "github-actions[bot]"',
        'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"',
        'git tag -a "${{ steps.plan.outputs.next_tag }}" -F release-notes.md',
        'git push origin "${{ steps.plan.outputs.next_tag }}"',
    ]
    assert isinstance(s3, dict), "release job step 3 must be mapping"
    assert s3.get("name") == "Create tag", "release job step 3 must be Create tag"
    assert s3.get("if") == "steps.plan.outputs.release_needed == 'true'", "release job step 3 must check release_needed"
    lines = [line.strip() for line in str(s3.get("run", "")).strip().splitlines() if line.strip()]
    assert lines == canonical_lines, "release job step 3 must run exact canonical tag commands"
    assert "env" not in s3, "release job step 3 must not declare env"


def _validate_release_publish_step_4(s4: Any) -> None:
    expected_cmd = 'gh release create "${{ steps.plan.outputs.next_tag }}" --notes-file release-notes.md'
    assert isinstance(s4, dict), "release job step 4 must be mapping"
    assert s4.get("name") == "Create GitHub release", "release job step 4 must be Create GitHub release"
    assert s4.get("if") == "steps.plan.outputs.release_needed == 'true'", "release job step 4 must check release_needed"
    assert str(s4.get("run", "")).strip() == expected_cmd, "release job step 4 must run canonical gh release create"
    assert s4.get("env") == {"GH_TOKEN": "${{ github.token }}"}, "release job step 4 env must be exactly GH_TOKEN"


def _validate_release_publish_steps(release_steps: list[Any]) -> None:
    assert isinstance(release_steps, list), "release job steps must be a list"
    assert len(release_steps) == 5, f"release job must have exactly 5 canonical steps, got {len(release_steps)}"
    _validate_release_publish_step_0(release_steps[0])
    _validate_release_publish_step_1(release_steps[1])
    _validate_release_publish_step_2(release_steps[2])
    _validate_release_publish_step_3(release_steps[3])
    _validate_release_publish_step_4(release_steps[4])


def _validate_release_publish_job(release: dict[str, Any]) -> None:
    validate_shell_configuration("release job", release)
    _validate_job_level_if("release", release)
    needs = release.get("needs")
    assert needs == "verify" or (isinstance(needs, list) and "verify" in needs), (
        "release job must declare needs: verify"
    )
    release_if = str(release.get("if", "")).lower()
    assert "always()" not in release_if, "release job must not have if: always()"

    assert "permissions" in release, "release job must declare permissions"
    release_perms = release["permissions"]
    assert isinstance(release_perms, dict), "release job must declare permissions mapping"
    assert release_perms == {"contents": "write"}, (
        f"release job permissions must be exactly {{'contents': 'write'}}, got {release_perms!r}"
    )
    assert release.get("runs-on") == "ubuntu-latest", "release job runs-on must be ubuntu-latest"
    assert "container" not in release, "release job must not declare container"
    assert "services" not in release, "release job must not declare services"
    assert "continue-on-error" not in release, "release job must not declare continue-on-error"

    release_steps = release.get("steps", [])
    _validate_release_publish_steps(release_steps)


def _validate_pinned_ruff() -> None:
    quality_doc = yaml.safe_load((WORKFLOWS_DIR / "quality.yaml").read_text(encoding="utf-8"))
    test_steps = quality_doc["jobs"]["tests"]["steps"]
    assert any("ruff==0.15.21" in s.get("run", "") for s in test_steps if isinstance(s, dict))

    pre_commit = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    ruff_repo = next(r for r in pre_commit.get("repos", []) if "ruff-pre-commit" in r.get("repo", ""))
    assert ruff_repo.get("rev") == "v0.15.21"


def test_release_workflow_keeps_non_cancelling_serialisation() -> None:
    path = WORKFLOWS_DIR / "release.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "release workflow must be a valid mapping"
    assert not triggers_pull_request(doc), "release workflow must not trigger on pull_request"

    triggers = get_on_triggers(doc)
    assert isinstance(triggers, dict), "release workflow triggers must be a mapping"
    _validate_release_triggers(triggers)

    concurrency = doc.get("concurrency")
    assert isinstance(concurrency, dict), "release workflow must declare concurrency mapping"
    assert concurrency.get("group") == "agent-skills-release"
    assert concurrency.get("cancel-in-progress") is False
    validate_workflow_steps(path.name, doc)

    # Validate release workflow contract
    wf_perms = doc.get("permissions")
    assert wf_perms == {"contents": "read"}, (
        f"release workflow permissions must be exactly {{'contents': 'read'}}, got {wf_perms!r}"
    )

    jobs = doc.get("jobs", {})
    assert "verify" in jobs, "release workflow must contain verify job"
    assert "release" in jobs, "release workflow must contain release job"

    _validate_release_verify_job(jobs["verify"])
    _validate_release_publish_job(jobs["release"])
    _validate_pinned_ruff()


def _assert_mutant_fails(doc: dict[str, Any], expected_err: str) -> None:
    try:
        validate_workflow_steps("mutant.yaml", doc)
        raise AssertionError(f"Expected {expected_err!r} to be rejected")
    except AssertionError as err:
        assert expected_err in str(err)


def _test_mutants_basic_step_and_job_guards() -> None:
    # 1. Step with continue-on-error: true must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "pytest", "continue-on-error": True}]}}},
        "continue-on-error: true",
    )
    # 2. Step with if: false must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "pytest", "if": False}]}}},
        "if: false",
    )
    # 3. Job with if: false must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"if": False, "steps": [{"name": "bad", "run": "pytest"}]}}},
        "if: false",
    )
    # 4. Job with continue-on-error: true must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"continue-on-error": True, "steps": [{"name": "bad", "run": "pytest"}]}}},
        "continue-on-error: true",
    )
    # 5. Step with if expression ${{ false }} must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "pytest", "if": "${{ false }}"}]}}},
        "if: false",
    )
    # 6. Step with command failure masking (|| true) must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "ruff format --check --diff . || true"}]}}},
        "masks command failure",
    )
    # 7. Step with fully commented run command must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "# pytest --cov=tools"}]}}},
        "empty or fully commented",
    )
    # 8. Step with set +e in run command must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "set +e\npytest\ntrue"}]}}},
        "set +e",
    )
    # 9. Step with trailing true in run command must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "pytest\ntrue"}]}}},
        "masks failure",
    )
    # 10. Mandatory format check step with if condition must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "ruff format --check --diff .", "if": "${{ 1 == 2 }}"}]}}},
        "must be unconditional",
    )
    # 11. Step with continue-on-error expression must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "pytest", "continue-on-error": "${{ 1 == 1 }}"}]}}},
        "continue-on-error",
    )
    # 12. Step with shell conditional if false must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "if false; then\n  pytest\nfi"}]}}},
        "masks command failure or uses shell conditional",
    )
    # 13. Step with || test $? -ne 0 failure suppression must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "pytest || test $? -ne 0"}]}}},
        "masks command failure or uses shell conditional",
    )
    # 14. Step with if failure suppression must fail validation
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "if pytest; then echo passed; fi"}]}}},
        "masks command failure or uses shell conditional",
    )


def _test_mutants_release_verify_permissions(release_path: Path) -> None:
    from tests.test_format_gate import validate_release_workflow as fg_rel_val
    from tools.validate_marketplace import validate_release_workflow as vm_rel_val

    bad_verify_scopes = [
        {"contents": "read", "checks": "read", "actions": "write"},
        {"contents": "read", "checks": "read", "issues": "write"},
        {"contents": "read", "checks": "read", "id-token": "write"},
        {"contents": "read", "checks": "read", "pull-requests": "write"},
        {"contents": "read", "checks": "read", "packages": "write"},
        "write-all",
        {"contents": "write", "checks": "read"},
    ]
    for bad_perm in bad_verify_scopes:
        doc = yaml.safe_load(release_path.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["permissions"] = bad_perm
        sp_failed = False
        try:
            validate_workflow_steps("release.yaml", doc)
        except AssertionError:
            sp_failed = True
        assert sp_failed, f"scripts.test_pr_workflow_cancellation accepted bad verify perms {bad_perm}"

        vm_errs = vm_rel_val(doc)
        assert len(vm_errs) > 0, f"tools.validate_marketplace accepted bad verify perms {bad_perm}"

        fg_failed = False
        try:
            fg_rel_val(doc)
        except AssertionError:
            fg_failed = True
        assert fg_failed, f"tests.test_format_gate accepted bad verify perms {bad_perm}"


def _test_mutants_release_publish_permissions(release_path: Path) -> None:
    from tests.test_format_gate import validate_release_workflow as fg_rel_val
    from tools.validate_marketplace import validate_release_workflow as vm_rel_val

    bad_release_scopes = [
        {"contents": "write", "actions": "write"},
        {"contents": "write", "id-token": "write"},
        {"contents": "write", "issues": "write"},
        {"contents": "write", "pull-requests": "write"},
        {"contents": "write", "packages": "write"},
        "write-all",
        {"contents": "read"},
    ]
    for bad_perm in bad_release_scopes:
        doc = yaml.safe_load(release_path.read_text(encoding="utf-8"))
        doc["jobs"]["release"]["permissions"] = bad_perm
        sp_failed = False
        try:
            _validate_release_publish_job(doc["jobs"]["release"])
        except AssertionError:
            sp_failed = True
        assert sp_failed, f"scripts accepted bad release perms {bad_perm}"

        vm_errs = vm_rel_val(doc)
        assert len(vm_errs) > 0, f"tools.validate_marketplace accepted bad release perms {bad_perm}"

        fg_failed = False
        try:
            fg_rel_val(doc)
        except AssertionError:
            fg_failed = True
        assert fg_failed, f"tests.test_format_gate accepted bad release perms {bad_perm}"


def _test_mutants_release_workflow_permissions(release_path: Path) -> None:
    from tests.test_format_gate import validate_release_workflow as fg_rel_val
    from tools.validate_marketplace import validate_release_workflow as vm_rel_val

    bad_wf_perms = [
        "write-all",
        {"contents": "write"},
        {"contents": "read", "id-token": "write"},
    ]
    for bad_perm in bad_wf_perms:
        doc = yaml.safe_load(release_path.read_text(encoding="utf-8"))
        doc["permissions"] = bad_perm

        vm_errs = vm_rel_val(doc)
        assert len(vm_errs) > 0, f"tools.validate_marketplace accepted bad release wf perms {bad_perm}"

        fg_failed = False
        try:
            fg_rel_val(doc)
        except AssertionError:
            fg_failed = True
        assert fg_failed, f"tests.test_format_gate accepted bad release wf perms {bad_perm}"


def _assert_release_wf_mutant_fails(mutated_doc: dict[str, Any], desc: str) -> None:
    from tests.test_format_gate import validate_release_workflow as fg_rel_val
    from tools.validate_marketplace import validate_release_workflow as vm_rel_val

    vm_errs = vm_rel_val(mutated_doc)
    assert len(vm_errs) > 0, f"tools.validate_marketplace accepted mutant: {desc}"

    fg_failed = False
    try:
        fg_rel_val(mutated_doc)
    except AssertionError:
        fg_failed = True
    assert fg_failed, f"tests.test_format_gate accepted mutant: {desc}"


def _test_mutants_release_publish_steps(release_path: Path) -> None:
    doc1 = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc1["jobs"]["release"]["steps"].append({"name": "exfil", "run": "curl https://example.com"})
    _assert_release_wf_mutant_fails(doc1, "appended step")

    doc2 = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc2["jobs"]["release"]["steps"][4]["run"] = "gh release create evil"
    _assert_release_wf_mutant_fails(doc2, "modified release cmd")


def _test_mutants_release_actions_and_inputs(release_path: Path) -> None:
    doc_unpinned_checkout = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_unpinned_checkout["jobs"]["release"]["steps"][0]["uses"] = "actions/checkout@main"
    _assert_release_wf_mutant_fails(doc_unpinned_checkout, "checkout@main")

    doc_semver_checkout = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_semver_checkout["jobs"]["release"]["steps"][0]["uses"] = "actions/checkout@v7.0.0"
    _assert_release_wf_mutant_fails(doc_semver_checkout, "checkout@v7.0.0")

    doc_unpinned_setup = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_unpinned_setup["jobs"]["release"]["steps"][1]["uses"] = "actions/setup-python@attacker-branch"
    _assert_release_wf_mutant_fails(doc_unpinned_setup, "setup-python@attacker-branch")

    doc_semver_setup = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_semver_setup["jobs"]["release"]["steps"][1]["uses"] = "actions/setup-python@v6.3.0"
    _assert_release_wf_mutant_fails(doc_semver_setup, "setup-python@v6.3.0")

    doc_repo_override = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_repo_override["jobs"]["release"]["steps"][0]["with"]["repository"] = "attacker/project"
    _assert_release_wf_mutant_fails(doc_repo_override, "checkout repository override")


def _test_mutants_release_job_environment(release_path: Path) -> None:
    doc_self_hosted = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_self_hosted["jobs"]["release"]["runs-on"] = "self-hosted"
    _assert_release_wf_mutant_fails(doc_self_hosted, "runs-on: self-hosted")

    doc_container = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_container["jobs"]["release"]["container"] = "attacker/image:latest"
    _assert_release_wf_mutant_fails(doc_container, "container declared")

    doc_services = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_services["jobs"]["release"]["services"] = {"redis": {"image": "redis"}}
    _assert_release_wf_mutant_fails(doc_services, "services declared")

    doc_extra_env = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_extra_env["jobs"]["release"]["steps"][2]["env"] = {"GH_TOKEN": "stolen"}
    _assert_release_wf_mutant_fails(doc_extra_env, "extra step env")


def _test_mutants_release_commands_and_triggers(release_path: Path) -> None:
    doc_appended_plan = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_appended_plan["jobs"]["release"]["steps"][2]["run"] += "\ngit push origin HEAD:refs/heads/unreviewed"
    _assert_release_wf_mutant_fails(doc_appended_plan, "appended plan command")

    doc_appended_tag = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    doc_appended_tag["jobs"]["release"]["steps"][3]["run"] += "\ngit push origin HEAD:refs/heads/unreviewed"
    _assert_release_wf_mutant_fails(doc_appended_tag, "appended tag command")

    doc_pr_target = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    on_key = "on" if "on" in doc_pr_target else True
    doc_pr_target[on_key]["pull_request_target"] = {}
    _assert_release_wf_mutant_fails(doc_pr_target, "pull_request_target trigger")


def _test_mutants_release_and_shell_overrides() -> None:
    release_path = WORKFLOWS_DIR / "release.yaml"
    rel_doc = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    bad_rel_doc = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    bad_rel_doc["jobs"]["verify"]["if"] = "true || (" + rel_doc["jobs"]["verify"]["if"] + ")"
    try:
        _validate_release_verify_job(bad_rel_doc["jobs"]["verify"])
        raise AssertionError("Expected job guard with true || to fail")
    except AssertionError as err:
        assert "illegal bypass expression" in str(err) or "must have exactly 2 top-level branches" in str(err)

    bad_perm_doc = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    del bad_perm_doc["jobs"]["verify"]["permissions"]
    try:
        _validate_release_verify_job(bad_perm_doc["jobs"]["verify"])
        raise AssertionError("Expected missing verify permissions to fail")
    except AssertionError as err:
        assert "declare permissions" in str(err)

    _test_mutants_release_verify_permissions(release_path)
    _test_mutants_release_publish_permissions(release_path)
    _test_mutants_release_workflow_permissions(release_path)
    _test_mutants_release_publish_steps(release_path)
    _test_mutants_release_actions_and_inputs(release_path)
    _test_mutants_release_job_environment(release_path)
    _test_mutants_release_commands_and_triggers(release_path)

    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "bad", "run": "pytest", "shell": "bash {0}"}]}}},
        "illegal shell override",
    )
    _assert_mutant_fails(
        {"jobs": {"test": {"defaults": {"run": {"shell": "bash {0}"}}, "steps": [{"name": "bad", "run": "pytest"}]}}},
        "illegal shell override",
    )
    _assert_mutant_fails(
        {"defaults": {"run": {"shell": "bash {0}"}}, "jobs": {"test": {"steps": [{"name": "bad", "run": "pytest"}]}}},
        "illegal shell override",
    )
    try:
        validate_workflow_steps("quality.yaml", {"jobs": {"quality": {"if": "${{ false }}", "steps": []}}})
        raise AssertionError("Expected quality job with if to fail")
    except AssertionError as err:
        assert "must not have an if: condition" in str(err)

    _assert_mutant_fails(
        {
            "jobs": {
                "test": {
                    "steps": [
                        {
                            "name": "Run tests with coverage",
                            "run": "pytest --cov=tools --cov-branch --cov-report=json:coverage.json --cov-fail-under=0",
                        }
                    ]
                }
            }
        },
        "coverage floor 0 < 90",
    )
    _assert_mutant_fails(
        {
            "jobs": {
                "test": {
                    "steps": [
                        {
                            "name": "Run tests with coverage",
                            "run": "pytest --cov=tools --cov-branch --cov-fail-under=90",
                        }
                    ]
                }
            }
        },
        "missing '--cov-report=json:coverage.json'",
    )
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "Check format", "run": ": python -m ruff format --check --diff ."}]}}},
        "banned prefix",
    )
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "Enforce Release Gate", "run": "true python3 tools/release_gate.py"}]}}},
        "banned prefix",
    )
    _assert_mutant_fails(
        {"jobs": {"test": {"steps": [{"name": "Enforce Release Gate", "run": "! python3 tools/release_gate.py"}]}}},
        "banned prefix",
    )
    _assert_mutant_fails(
        {
            "jobs": {
                "test": {
                    "steps": [{"name": "Enforce Release Gate", "run": "set +o errexit\npython3 tools/release_gate.py"}]
                }
            }
        },
        "set +o errexit",
    )
    bad_slug_doc = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    for s in bad_slug_doc["jobs"]["verify"]["steps"]:
        if "tools/release_gate.py" in s.get("run", ""):
            s["run"] = s["run"].replace('--expected-app-slug "github-actions"', '--expected-app-slug ""')
    try:
        validate_workflow_steps("release.yaml", bad_slug_doc)
        raise AssertionError("Expected empty expected-app-slug to fail")
    except AssertionError as err:
        assert "missing expected release gate command" in str(
            err
        ) or "release gate command does not match expected" in str(err)


def _assert_quality_env_val_rejected(quality_path: Path, env_val: str) -> None:
    bad_wf = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    bad_wf["env"] = {"PYTEST_ADDOPTS": env_val}
    try:
        validate_workflow_steps("quality.yaml", bad_wf)
        raise AssertionError(f"Expected workflow top-level env {env_val} to fail")
    except AssertionError as err:
        assert "must not declare env mapping" in str(err) or "disallowed env variable" in str(err)

    bad_job = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    bad_job["jobs"]["tests"]["env"] = {"PYTEST_ADDOPTS": env_val}
    try:
        validate_workflow_steps("quality.yaml", bad_job)
        raise AssertionError(f"Expected tests job env {env_val} to fail")
    except AssertionError as err:
        assert "must not declare env mapping" in str(err) or "disallowed env variable" in str(err)

    bad_step = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    for s in bad_step["jobs"]["tests"]["steps"]:
        if "pytest" in s.get("run", ""):
            s["env"] = {"PYTEST_ADDOPTS": env_val}
    try:
        validate_workflow_steps("quality.yaml", bad_step)
        raise AssertionError(f"Expected coverage step env {env_val} to fail")
    except AssertionError as err:
        assert "must not declare env mapping" in str(err) or "disallowed env variable" in str(err)


def _test_mutants_quality_env_bypasses() -> None:
    quality_path = WORKFLOWS_DIR / "quality.yaml"
    for env_val in ("--collect-only", "--no-cov"):
        _assert_quality_env_val_rejected(quality_path, env_val)


def _test_mutants_quality_step_conditions() -> None:
    quality_path = WORKFLOWS_DIR / "quality.yaml"

    for target_script in (
        "tools/validate_skills.py",
        "tools/validate_marketplace.py",
        "tools/tf_module_layout.py",
        "skills/portfolio-delivery-control/tests",
    ):
        bad_if_doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
        for s in bad_if_doc["jobs"]["tests"]["steps"]:
            if target_script in s.get("run", ""):
                s["if"] = "${{ 1 == 2 }}"
        try:
            validate_workflow_steps("quality.yaml", bad_if_doc)
            raise AssertionError(f"Expected step running {target_script} with if condition to fail")
        except AssertionError as err:
            assert "must be unconditional" in str(err)

    bad_pip_doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    bad_pip_doc["jobs"]["tests"]["steps"].insert(3, {"name": "Reinstall ruff", "run": "pip install ruff==0.9.10"})
    try:
        validate_workflow_steps("quality.yaml", bad_pip_doc)
        raise AssertionError("Expected secondary pip install in tests to fail")
    except AssertionError as err:
        assert "pip install steps" in str(err) or "secondary pip install" in str(err)


def _test_mutants_quality_job_step_conditions() -> None:
    quality_path = WORKFLOWS_DIR / "quality.yaml"
    for target_script in MANDATORY_QUALITY_JOB_SCRIPTS:
        bad_if_doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
        modified = False
        for s in bad_if_doc["jobs"]["quality"]["steps"]:
            if target_script in s.get("run", ""):
                s["if"] = "${{ 1 == 2 }}"
                modified = True
        assert modified, f"Expected step running {target_script} in quality job"
        try:
            validate_workflow_steps("quality.yaml", bad_if_doc)
            raise AssertionError(f"Expected quality step running {target_script} with if condition to fail")
        except AssertionError as err:
            assert "must be unconditional" in str(err)


def _test_mutant_del_quality_job(quality_path: Path) -> None:
    del_q_doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    del del_q_doc["jobs"]["quality"]
    try:
        validate_workflow_steps("quality.yaml", del_q_doc)
        raise AssertionError("Expected missing jobs.quality to fail")
    except AssertionError as err:
        assert "missing required 'quality' job" in str(err)


def _test_mutant_spoof_pr_title(quality_path: Path) -> None:
    spoof_doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    for s in spoof_doc["jobs"]["quality"]["steps"]:
        if "privacy_scan.py" in s.get("run", ""):
            s["name"] = "Validate PR title and privacy"
            s["if"] = "github.event_name == 'pull_request' && 1 == 2"
    try:
        validate_workflow_steps("quality.yaml", spoof_doc)
        raise AssertionError("Expected spoofed PR title with if: condition on privacy_scan to fail")
    except AssertionError as err:
        assert "must be unconditional" in str(err) or "missing required script" in str(err)


def _test_mutant_python_c_bypass(quality_path: Path) -> None:
    python_c_doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    for s in python_c_doc["jobs"]["quality"]["steps"]:
        if "privacy_scan.py" in s.get("run", ""):
            s["run"] = 'python3 -c "pass" scripts/privacy_scan.py'
    try:
        validate_workflow_steps("quality.yaml", python_c_doc)
        raise AssertionError("Expected python -c argument bypass on privacy_scan to fail")
    except AssertionError as err:
        assert "missing required script: privacy_scan.py" in str(err)


def _test_mutant_stubbed_tests_job(quality_path: Path) -> None:
    stub_doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    stub_doc["jobs"]["tests"] = {"runs-on": "ubuntu-latest", "steps": [{"uses": "actions/checkout@v7.0.0"}]}
    try:
        validate_workflow_steps("quality.yaml", stub_doc)
        raise AssertionError("Expected stubbed tests job to fail")
    except AssertionError as err:
        assert (
            "tests job failed gate validation" in str(err)
            or "missing required gate" in str(err)
            or "runs-on must be 'ubuntu-latest'" in str(err)
        )


def _assert_all_validators_reject(doc: dict[str, Any], label: str) -> None:
    from tests.test_format_gate import validate_quality_workflow as fg_val
    from tools.validate_marketplace import validate_quality_workflow as vm_val
    from tools.validate_skills import validate_quality_workflow as vs_val

    e1 = vs_val(doc)
    e2 = vm_val(doc)
    assert len(e1) > 0, f"{label}: tools.validate_skills accepted mutant"
    assert len(e2) > 0, f"{label}: tools.validate_marketplace accepted mutant"

    sp_failed = False
    try:
        validate_workflow_steps("quality.yaml", doc)
    except AssertionError:
        sp_failed = True
    assert sp_failed, f"{label}: scripts.test_pr_workflow_cancellation accepted mutant"

    fg_failed = False
    try:
        fg_val(doc)
    except AssertionError:
        fg_failed = True
    assert fg_failed, f"{label}: tests.test_format_gate accepted mutant"


def _test_mutant_github_env_writer(quality_path: Path) -> None:
    doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    doc["jobs"]["tests"]["steps"].insert(
        3,
        {
            "name": "Write GITHUB_ENV",
            "run": (
                "python -c \"open(__import__('os').environ['GITHUB_ENV'],'a')"
                ".write('PYTEST_ADDOPTS=--collect-only --no-cov\\n')\""
            ),
        },
    )
    _assert_all_validators_reject(doc, "GITHUB_ENV writer injection")


def _test_mutant_workflow_defaults_working_directory(quality_path: Path) -> None:
    doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    doc["defaults"] = {"run": {"working-directory": "tests/fixtures/sol61_shadow"}}
    _assert_all_validators_reject(doc, "workflow-level defaults.run.working-directory")


def _test_mutant_job_defaults_working_directory(quality_path: Path) -> None:
    doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    doc["jobs"]["tests"]["defaults"] = {"run": {"working-directory": "tests/fixtures/sol61_shadow"}}
    _assert_all_validators_reject(doc, "job-level defaults.run.working-directory")


def _test_mutant_step_working_directory(quality_path: Path) -> None:
    doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    doc["jobs"]["tests"]["steps"][3]["working-directory"] = "tests/fixtures/sol61_shadow"
    _assert_all_validators_reject(doc, "step-level working-directory")


def _test_mutants_checkout_with_inputs(quality_path: Path) -> None:
    bad_with_inputs = [
        {"ref": "main"},
        {"ref": "84ef88038cc25582d6a54338ef77b570bc833340"},
        {"repository": "other/agent-skills"},
        {"path": "sub"},
        {"sparse-checkout": "tools"},
    ]
    for target_job in ("quality", "tests"):
        for with_dict in bad_with_inputs:
            doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
            for step in doc["jobs"][target_job]["steps"]:
                if step.get("uses") == "actions/checkout@v7.0.0":
                    step["with"] = dict(with_dict)
            _assert_all_validators_reject(doc, f"checkout with {with_dict} in {target_job}")


def _test_mutants_setup_python_with_inputs(quality_path: Path) -> None:
    bad_with_inputs = [
        {"python-version": "3.11"},
        {},
        {"python-version": "3.12", "cache": "pip"},
    ]
    for target_job in ("quality", "tests"):
        for with_dict in bad_with_inputs:
            doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
            for step in doc["jobs"][target_job]["steps"]:
                if step.get("uses") == "actions/setup-python@v6.3.0":
                    step["with"] = dict(with_dict)
            _assert_all_validators_reject(doc, f"setup-python with {with_dict} in {target_job}")


def _test_mutants_workflow_permissions(quality_path: Path) -> None:
    bad_wf_perms: list[tuple[Any, str]] = [
        ("write-all", "permissions write-all"),
        ("read-all", "permissions read-all"),
        ({"contents": "write"}, "permissions contents write"),
        ({"contents": "read", "issues": "write"}, "permissions extra issues write"),
        ({"contents": "read", "actions": "write"}, "permissions extra actions write"),
        (None, "missing permissions"),
    ]
    for perm_val, label in bad_wf_perms:
        doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
        if perm_val is None:
            doc.pop("permissions", None)
        else:
            doc["permissions"] = perm_val
        _assert_all_validators_reject(doc, f"quality workflow {label}")


def _test_mutants_quality_workflow_integrity() -> None:
    quality_path = WORKFLOWS_DIR / "quality.yaml"
    _test_mutant_del_quality_job(quality_path)
    _test_mutant_spoof_pr_title(quality_path)
    _test_mutant_python_c_bypass(quality_path)
    _test_mutant_stubbed_tests_job(quality_path)
    _test_mutant_github_env_writer(quality_path)
    _test_mutant_workflow_defaults_working_directory(quality_path)
    _test_mutant_job_defaults_working_directory(quality_path)
    _test_mutant_step_working_directory(quality_path)
    _test_mutants_checkout_with_inputs(quality_path)
    _test_mutants_setup_python_with_inputs(quality_path)
    _test_mutants_job_level_keys(quality_path)
    _test_mutants_workflow_permissions(quality_path)


def _test_mutants_job_level_keys(quality_path: Path) -> None:
    bad_job_mutations: list[tuple[str, Any, str]] = [
        (
            "container",
            {"image": "python:3.12-slim", "env": {"PYTEST_ADDOPTS": "--collect-only --no-cov"}},
            "container dict with env",
        ),
        ("container", "python:3.12-slim", "container string"),
        ("services", {"redis": {"image": "redis"}}, "services dict"),
        ("runs-on", "self-hosted", "runs-on self-hosted"),
        ("timeout-minutes", 1, "timeout-minutes 1"),
        ("permissions", "write-all", "permissions write-all"),
        ("permissions", "read-all", "permissions read-all"),
        ("permissions", {"contents": "write"}, "permissions contents write"),
    ]
    for target_job in ("quality", "tests"):
        for key, value, label in bad_job_mutations:
            doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
            doc["jobs"][target_job][key] = value
            _assert_all_validators_reject(doc, f"job {target_job} {label}")

    doc_extra = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    doc_extra["jobs"]["extra"] = {"runs-on": "ubuntu-latest", "steps": []}
    _assert_all_validators_reject(doc_extra, "extra job in quality workflow")


def _assert_release_env_val_rejected(release_path: Path, env_val: str) -> None:
    rel_wf = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    rel_wf["env"] = {"PYTEST_ADDOPTS": env_val}
    try:
        validate_workflow_steps("release.yaml", rel_wf)
        raise AssertionError(f"Expected release workflow top-level env {env_val} to fail")
    except AssertionError as err:
        assert "must not declare env mapping" in str(err) or "disallowed env variable" in str(err)

    rel_job = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    rel_job["jobs"]["verify"]["env"] = {"PYTEST_ADDOPTS": env_val}
    try:
        _validate_release_verify_job(rel_job["jobs"]["verify"])
        raise AssertionError(f"Expected verify job env {env_val} to fail")
    except AssertionError as err:
        assert "must not declare env mapping" in str(err) or "disallowed env variable" in str(err)

    rel_step = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    for s in rel_step["jobs"]["verify"]["steps"]:
        if "pytest" in s.get("run", ""):
            s["env"] = {"PYTEST_ADDOPTS": env_val}
    try:
        validate_workflow_steps("release.yaml", rel_step)
        raise AssertionError(f"Expected verify suite step env {env_val} to fail")
    except AssertionError as err:
        assert "must not declare env mapping" in str(err) or "disallowed env variable" in str(err)


def _test_mutants_release_env_bypasses() -> None:
    release_path = WORKFLOWS_DIR / "release.yaml"
    for env_val in ("--collect-only", "--no-cov"):
        _assert_release_env_val_rejected(release_path, env_val)


def _test_mutants_release_gate_and_pip() -> None:
    release_path = WORKFLOWS_DIR / "release.yaml"

    rel_gate_co = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    for s in rel_gate_co["jobs"]["verify"]["steps"]:
        if "tools/release_gate.py" in s.get("run", ""):
            s["env"]["PYTEST_ADDOPTS"] = "--collect-only"
    try:
        validate_workflow_steps("release.yaml", rel_gate_co)
        raise AssertionError("Expected release gate step env collect-only to fail")
    except AssertionError as err:
        assert "disallowed env variable" in str(err) or "banned env variable" in str(err)

    rel_pip = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    rel_pip["jobs"]["verify"]["steps"].insert(4, {"name": "Reinstall ruff", "run": "pip install ruff==0.9.10"})
    try:
        _validate_release_verify_job(rel_pip["jobs"]["verify"])
        raise AssertionError("Expected secondary pip install in verify to fail")
    except AssertionError as err:
        assert "pip install steps" in str(err) or "secondary pip install" in str(err)


def test_negative_mutants_rejected() -> None:
    _test_mutants_basic_step_and_job_guards()
    _test_mutants_release_and_shell_overrides()
    _test_mutants_quality_env_bypasses()
    _test_mutants_quality_step_conditions()
    _test_mutants_quality_job_step_conditions()
    _test_mutants_quality_workflow_integrity()
    _test_mutants_release_env_bypasses()
    _test_mutants_release_gate_and_pip()


def test_execution_probe_failing_gate_command_exits_nonzero() -> None:
    """Execution probe test using bash -eo pipefail confirming failing gate command exits non-zero."""
    script = """
set -eo pipefail
python3 -c "import sys; sys.exit(23)"
echo "SHOULD_NOT_EXECUTE"
"""
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 23
    assert "SHOULD_NOT_EXECUTE" not in proc.stdout


def main() -> int:
    test_pull_request_workflows_cancel_obsolete_runs()
    test_release_workflow_keeps_non_cancelling_serialisation()
    test_negative_mutants_rejected()
    test_execution_probe_failing_gate_command_exits_nonzero()
    print("PR workflow cancellation tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
