"""Contract tests for the CI Python-format gate (issue #101) and Release gate.

Guarantees:

1. Static workflow contract — the Quality workflow must run
   ``ruff format --check`` over the whole tree (``.``), so formatting drift
   fails CI. This locks the gate in place; deleting or narrowing it fails here.
2. Release workflow contract — the Release workflow must enforce that tagging
   and release creation strictly depend on a successful, read-only verify job
   running ``tools/release_gate.py``, with no write permissions in verify and
   no failure masking or conditional bypass.
3. Behavioral proof — ``ruff format --check`` actually rejects a misformatted
   file and accepts a clean one.
"""

from __future__ import annotations

import copy
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.validate_skills import (
    ALLOWED_JOB_LEVEL_KEYS,
    ALLOWED_USES_ACTIONS,
    CANONICAL_QUALITY_RUN_ARGVS,
    CANONICAL_TESTS_RUN_ARGVS,
    CANONICAL_TESTS_RUN_SEQUENCE,
    QUALITY_JOB_ALLOWED_ACTIONS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality.yaml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yaml"

# A file the formatter would rewrite (bad spacing, single quotes) and its
# formatted counterpart. Kept config-independent so the result does not depend
# on the repo's line-length.
MISFORMATTED = "x = {  'a' :1 ,'b':2 }\n"
FORMATTED = 'x = {"a": 1, "b": 2}\n'

FALSE_VALUES = ("false", "'false'", '"false"', "${{ false }}", "${{ 'false' }}")

REQUIRED_JOB_IF_SNIPPETS = (
    "github.event.workflow_run.event == 'push'",
    "github.event.workflow_run.conclusion == 'success'",
    "github.event.workflow_run.head_branch == 'main'",
    "github.event.workflow_run.head_repository.full_name == github.repository",
    "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'",
)

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

BANNED_SHELL_OPERATORS = frozenset({"||", "&&", ";", "|", "&", "`", "$(", ">", "<"})
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


def _is_bypassed_coe(coe: Any) -> bool:
    if coe is None:
        return False
    if coe is True:
        return True
    return str(coe).strip().lower() not in ("false", "${{ false }}", '"false"', "'false'")


def _is_bypassed_if(cond: Any) -> bool:
    if cond is None:
        return False
    if cond is False:
        return True
    return str(cond).strip().lower() in FALSE_VALUES


def is_effective_step(step: dict[str, Any]) -> bool:
    """Check that a step does not bypass execution via if: false or continue-on-error."""
    if not isinstance(step, dict):
        return False
    if _is_bypassed_coe(step.get("continue-on-error")):
        return False
    return not _is_bypassed_if(step.get("if"))


def is_effective_job(job: dict[str, Any]) -> bool:
    """Check that a job does not bypass execution via if: false or continue-on-error."""
    if not isinstance(job, dict):
        return False
    if _is_bypassed_coe(job.get("continue-on-error")):
        return False
    return not _is_bypassed_if(job.get("if"))


def is_failure_masked(line: str) -> bool:
    """Check if a shell command line masks non-zero exit codes (e.g. || true)."""
    return _has_shell_suppression_or_bypass(line)


def _is_format_line(line: str) -> bool:
    code_part = line.strip()
    if not code_part or code_part.startswith("#"):
        return False
    for op in BANNED_SHELL_OPERATORS:
        if op in code_part:
            return False
    try:
        tokens = shlex.split(code_part, comments=True)
    except ValueError:
        return False
    if not tokens or tokens[0] in BANNED_PREFIXES:
        return False
    if tokens in (
        EXPECTED_RUFF_FORMAT_ARGV,
        ["python", "-m", "ruff", "format", "--diff", "--check", "."],
        ["python3", "-m", "ruff", "format", "--check", "--diff", "."],
        ["python3", "-m", "ruff", "format", "--diff", "--check", "."],
        ["ruff", "format", "--check", "--diff", "."],
        ["ruff", "format", "--diff", "--check", "."],
        ["ruff", "format", "--check", "."],
    ):
        return True
    return False


def _is_invalid_format_line(line: str) -> bool:
    if "set +o errexit" in line or "set +e" in line:
        return True
    return _has_shell_suppression_or_bypass(line)


def _run_script_has_format(run_cmd: str) -> bool:
    active_lines = [line.strip() for line in run_cmd.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not active_lines:
        return False
    if any(_is_invalid_format_line(line) for line in active_lines):
        return False
    return any(_is_format_line(line) for line in active_lines)


def _step_has_format_gate(step: dict[str, Any]) -> bool:
    if not is_effective_step(step):
        return False
    if step.get("if") is not None and str(step.get("if")).strip():
        return False
    step_shell = step.get("shell")
    if step_shell is not None and step_shell != "bash -eo pipefail":
        return False
    return _run_script_has_format(step.get("run", ""))


def _job_has_format_gate(job_name: str, job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    if job_name in ("quality", "tests") and job.get("if") is not None:
        return False
    if not is_effective_job(job):
        return False
    try:
        validate_shell_configuration(f"job {job_name}", job)
    except AssertionError:
        return False
    return any(_step_has_format_gate(s) for s in job.get("steps", []) if isinstance(s, dict))


def has_active_format_gate(doc: dict[str, Any]) -> bool:
    """Validate that doc contains an active, effective ruff format --check step."""
    if not isinstance(doc, dict):
        return False
    try:
        validate_shell_configuration("workflow", doc)
    except AssertionError:
        return False
    jobs = doc.get("jobs", {})
    return isinstance(jobs, dict) and any(_job_has_format_gate(k, v) for k, v in jobs.items())


def _validate_step_run_lines(step_name: str, run_cmd: str) -> list[str]:
    """Parse step run: script, rejecting set +e, failure masking, and trivial bypasses."""
    assert "set +e" not in run_cmd, f"{step_name!r} must not contain 'set +e'"
    assert "set +o errexit" not in run_cmd, f"{step_name!r} must not contain 'set +o errexit'"
    active_lines: list[str] = []
    for line in run_cmd.splitlines():
        code_part = line.split("#")[0].strip()
        if not code_part:
            continue
        assert not _has_shell_suppression_or_bypass(code_part), (
            f"{step_name} command must not mask failures or use conditionals: {line!r}"
        )
        active_lines.append(code_part)
    assert active_lines, f"{step_name!r} has no active statements"
    return active_lines


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


def _validate_checkout_step(job_name: str, step: dict[str, Any]) -> None:
    checkout_with = step.get("with", {})
    assert checkout_with.get("fetch-depth") == 0, f"'{job_name}' job checkout must specify fetch-depth: 0"
    ref = checkout_with.get("ref", "")
    assert ref == "${{ github.event.workflow_run.head_sha || github.sha }}", (
        f"'{job_name}' job checkout must use exact ref expression "
        "'${{ github.event.workflow_run.head_sha || github.sha }}'"
    )


def _validate_release_gate_step(step: dict[str, Any]) -> None:
    validate_shell_configuration("release gate step", step)
    assert is_effective_step(step), "release gate step must not have continue-on-error or if: false"
    assert "if" not in step or not step["if"], "release gate step must be unconditional"
    assert step.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
        "release gate step must not specify continue-on-error"
    )
    run_cmd = step.get("run", "")
    cmd_argvs = parse_and_validate_gate_command_lines("release gate", run_cmd)
    assert len(cmd_argvs) == 1, "release gate step must contain exactly 1 gate command line"
    assert cmd_argvs[0] == EXPECTED_RELEASE_GATE_ARGV, (
        f"release gate command does not match expected exact argv:\n"
        f"Expected: {EXPECTED_RELEASE_GATE_ARGV}\nGot: {cmd_argvs[0]}"
    )


def _check_quality_pip_tokens(tokens: list[str]) -> None:
    for _pkg_name, expected_token in PINNED_TOOL_REQUIREMENTS.items():
        assert any(t == expected_token for t in tokens), f"quality tests pip install missing {expected_token!r}"
    for token in tokens:
        pkg = re.split(r"[=<>!~]", token)[0].strip()
        assert pkg not in DISALLOWED_PIP_PACKAGES, f"quality tests disallowed pip package {pkg!r}"
        if pkg in GATED_PACKAGES:
            expected = PINNED_TOOL_REQUIREMENTS.get(pkg)
            assert expected is not None and token == expected, (
                f"quality tests disallowed pip pin for {pkg!r}: {token!r}"
            )


def _validate_quality_pip_lines(step: dict[str, Any], primary_pip_step: dict[str, Any]) -> None:
    step_name = step.get("name", "unnamed step")
    run_cmd = step.get("run", "")
    for line in run_cmd.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        try:
            tokens = shlex.split(trimmed, comments=True)
        except ValueError:
            continue
        if is_pip_install_tokens(tokens):
            assert step is primary_pip_step, f"Disallowed secondary pip install in {step_name!r}: {tokens}"
            _check_quality_pip_tokens(tokens)


CANONICAL_TESTS_SCRIPT_COMMANDS: dict[tuple[str, ...], str] = {
    ("python", "tools/validate_skills.py"): "validate_skills",
    ("python3", "tools/validate_skills.py"): "validate_skills",
    ("python", "-B", "tools/validate_skills.py"): "validate_skills",
    ("python3", "-B", "tools/validate_skills.py"): "validate_skills",
    ("python", "tools/validate_marketplace.py"): "validate_marketplace",
    ("python3", "tools/validate_marketplace.py"): "validate_marketplace",
    ("python", "-B", "tools/validate_marketplace.py"): "validate_marketplace",
    ("python3", "-B", "tools/validate_marketplace.py"): "validate_marketplace",
    ("python", "tools/tf_module_layout.py", "--self-test"): "tf_module_layout",
    ("python3", "tools/tf_module_layout.py", "--self-test"): "tf_module_layout",
    ("python", "-B", "tools/tf_module_layout.py", "--self-test"): "tf_module_layout",
    ("python3", "-B", "tools/tf_module_layout.py", "--self-test"): "tf_module_layout",
    ("go", "vet", "./..."): "go_vet",
    ("go", "vet", "./tools/hclblocks/..."): "go_vet",
    ("python", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"): "unittest",
    ("python3", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"): "unittest",
    ("python", "-B", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"): "unittest",
    ("python3", "-B", "-m", "unittest", "discover", "-s", "skills/portfolio-delivery-control/tests", "-v"): "unittest",
    ("python", "-m", "tests.test_coverage_threshold", "coverage.json"): "coverage_threshold",
    ("python3", "-m", "tests.test_coverage_threshold", "coverage.json"): "coverage_threshold",
    ("python", "-B", "-m", "tests.test_coverage_threshold", "coverage.json"): "coverage_threshold",
    ("python3", "-B", "-m", "tests.test_coverage_threshold", "coverage.json"): "coverage_threshold",
}


def _classify_script_gate(step_name: str, run_cmd: str) -> str | None:
    triggers = (
        "tools/validate_skills.py",
        "tools/validate_marketplace.py",
        "tools/tf_module_layout.py",
        "go vet",
        "unittest discover",
        "skills/portfolio-delivery-control/tests",
        "tests.test_coverage_threshold",
    )
    for trigger in triggers:
        if trigger in run_cmd:
            lines = parse_and_validate_gate_command_lines(step_name, run_cmd)
            assert len(lines) == 1, f"expected 1 command line in {step_name!r}, got {len(lines)}"
            gate = CANONICAL_TESTS_SCRIPT_COMMANDS.get(tuple(lines[0]))
            assert gate is not None, f"disallowed command in {step_name!r}: {lines[0]}"
            return gate
    return None


def _classify_ruff_gate(run_cmd: str) -> str | None:
    if "format" in run_cmd and "ruff" in run_cmd:
        lines = parse_and_validate_gate_command_lines("quality format", run_cmd)
        assert len(lines) == 1 and lines[0] == EXPECTED_RUFF_FORMAT_ARGV
        return "format"
    if "check" in run_cmd and "ruff" in run_cmd:
        lines = parse_and_validate_gate_command_lines("quality lint", run_cmd)
        assert len(lines) == 1 and lines[0] == EXPECTED_RUFF_LINT_ARGV
        return "lint"
    return None


def _classify_coverage_gate(step_name: str, run_cmd: str) -> str | None:
    if run_cmd.strip().startswith("pytest") or step_name == "Run tests with coverage":
        lines = parse_and_validate_gate_command_lines("quality pytest", run_cmd)
        assert len(lines) == 1
        validate_pytest_coverage_command(lines[0], "quality pytest")
        return "cov"
    return None


def _classify_quality_gate(step_name: str, run_cmd: str) -> str | None:
    ruff_gate = _classify_ruff_gate(run_cmd)
    if ruff_gate:
        return ruff_gate
    cov_gate = _classify_coverage_gate(step_name, run_cmd)
    if cov_gate:
        return cov_gate
    return _classify_script_gate(step_name, run_cmd)


def _validate_step_uses(step: dict[str, Any], uses: str, step_name: str, prefix: str) -> None:
    if uses == "actions/checkout@v7.0.0":
        assert step.get("with") is None, (
            f"{prefix} step {step_name!r} disallowed 'with:' on actions/checkout: {step.get('with')!r}"
        )
    elif uses == "actions/setup-python@v6.3.0":
        assert step.get("with") == {"python-version": "3.12"}, (
            f"{prefix} step {step_name!r} invalid setup-python 'with:': "
            f"expected {{'python-version': '3.12'}}, got {step.get('with')!r}"
        )
    elif uses.startswith("actions/setup-go@") or _is_pinned_action(uses, "actions/setup-go"):
        assert _is_pinned_action(uses, "actions/setup-go"), (
            f"{prefix} step {step_name!r} actions/setup-go must be pinned by commit SHA: {uses!r}"
        )
        assert step.get("with") == {"go-version": "1.26.10"}, (
            f"{prefix} step {step_name!r} actions/setup-go 'with:' must be exactly "
            f"{{'go-version': '1.26.10'}}, got: {step.get('with')!r}"
        )


def _inspect_quality_tests_step(
    step: dict[str, Any],
    primary_pip_step: dict[str, Any],
    uses_seen: set[str],
    run_argvs_seen: list[tuple[str, ...]],
    detected_gates: set[str],
) -> None:
    step_name = str(step.get("name", "unnamed step"))
    validate_shell_configuration(f"quality tests step {step_name!r}", step)
    if "go vet" in str(step.get("run", "")):
        allowed_env = frozenset({"GOTOOLCHAIN", "GOWORK", "GOFLAGS"})
        assert step.get("working-directory") == "tools/hclblocks"
        assert step.get("env") == {
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOFLAGS": "-mod=readonly",
        }
    elif any(k in str(step.get("run", "")) for k in ("pytest", "test_coverage_threshold")):
        allowed_env = frozenset({"COVERAGE_SESSION_ID"})
    else:
        allowed_env = frozenset()
    validate_env_configuration(f"quality tests step {step_name!r}", step, allowed_keys=allowed_env)
    assert step.get("if") is None, f"quality tests step {step_name!r} must be unconditional"
    assert step.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
        f"quality tests step {step_name!r} must not have continue-on-error"
    )
    uses = step.get("uses")
    run_cmd = step.get("run")
    assert (uses is not None) ^ (run_cmd is not None), (
        f"quality tests step {step_name!r} must specify either 'uses' or 'run'"
    )
    if uses is not None:
        assert isinstance(uses, str)
        assert uses in ALLOWED_USES_ACTIONS or _is_pinned_action(uses, "actions/setup-go"), (
            f"quality tests step {step_name!r} has unrecognized uses action: {uses!r}"
        )
        assert uses not in uses_seen, f"quality tests step {step_name!r} duplicate uses step: {uses!r}"
        uses_seen.add(uses)
        _validate_step_uses(step, uses, step_name, "quality tests")
        return

    assert isinstance(run_cmd, str)
    _validate_quality_pip_lines(step, primary_pip_step)
    lines = parse_and_validate_gate_command_lines(step_name, run_cmd)
    assert len(lines) == 1, f"quality tests step {step_name!r} must have exactly 1 command line, got {len(lines)}"
    cmd_tuple = tuple(lines[0])
    assert cmd_tuple in CANONICAL_TESTS_RUN_ARGVS, (
        f"quality tests step {step_name!r} unrecognized run command: {lines[0]}"
    )
    assert cmd_tuple not in run_argvs_seen, f"quality tests step {step_name!r} duplicate run command: {lines[0]}"
    run_argvs_seen.append(cmd_tuple)

    gate = _classify_quality_gate(step_name, run_cmd)
    if gate:
        detected_gates.add(gate)


def _find_primary_pip_step(steps: list[Any]) -> dict[str, Any]:
    pip_steps: list[dict[str, Any]] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        for line in s.get("run", "").splitlines():
            trimmed = line.strip()
            if trimmed and not trimmed.startswith("#") and _has_pip_install(trimmed):
                pip_steps.append(s)
                break
    assert len(pip_steps) == 1, f"quality.yaml tests job must have exactly 1 pip install step, got {len(pip_steps)}"
    return pip_steps[0]


def _check_coverage_step_order(run_argvs_seen: list[tuple[str, ...]]) -> None:
    assert run_argvs_seen == list(CANONICAL_TESTS_RUN_SEQUENCE), (
        "quality.yaml tests job steps out of order: expected canonical step sequence"
    )
    p_idx = -1
    v_idx = -1
    for i, c in enumerate(run_argvs_seen):
        if c and c[0] == "pytest":
            p_idx = i
        elif c and "test_coverage_threshold" in " ".join(c):
            v_idx = i
    assert v_idx == p_idx + 1, "quality.yaml tests job: coverage threshold check must immediately follow pytest"


def _validate_quality_tests_steps(steps: list[Any]) -> None:
    detected_gates: set[str] = set()
    uses_seen: set[str] = set()
    run_argvs_seen: list[tuple[str, ...]] = []
    primary_pip_step = _find_primary_pip_step(steps)

    for step in steps:
        assert isinstance(step, dict), "quality tests step must be a mapping"
        _inspect_quality_tests_step(step, primary_pip_step, uses_seen, run_argvs_seen, detected_gates)

    for action in ALLOWED_USES_ACTIONS:
        assert action in uses_seen or (
            action.startswith("actions/setup-go") and any(_is_pinned_action(u, "actions/setup-go") for u in uses_seen)
        ), f"quality.yaml tests job missing required action: {action}"
    for cmd_tuple in CANONICAL_TESTS_RUN_ARGVS:
        assert cmd_tuple in run_argvs_seen, f"quality.yaml tests job missing canonical run command: {cmd_tuple}"

    _check_coverage_step_order(run_argvs_seen)

    expected_gates = {
        "format": "valid ruff format gate",
        "lint": "valid ruff lint gate",
        "cov": "valid pytest coverage gate",
        "coverage_threshold": "coverage threshold check",
        "validate_skills": "validate_skills.py",
        "validate_marketplace": "validate_marketplace.py",
        "tf_module_layout": "tf_module_layout.py --self-test",
        "go_vet": "go vet ./...",
        "unittest": "unittest discover",
    }
    for gate_key, gate_desc in expected_gates.items():
        assert gate_key in detected_gates, f"quality.yaml tests job missing {gate_desc}"


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
    for raw_line in run_cmd.splitlines():
        tokens = _line_tokens(raw_line)
        if _is_pr_title_tokens(tokens):
            return True
    return False


def _is_pr_title_step(step_name: str, run_cmd: str) -> bool:
    if any(s in run_cmd or s in step_name for s in MANDATORY_QUALITY_JOB_SCRIPTS):
        return False
    return _invokes_pr_title_script(run_cmd)


def _validate_quality_step_conditions(step: dict[str, Any], step_name: str, run_cmd: str) -> None:
    is_pr_title = _is_pr_title_step(step_name, run_cmd)
    step_if = step.get("if")
    if is_pr_title:
        if step_if is not None:
            step_if_str = str(step_if).strip()
            assert step_if is not False and step_if_str.lower() not in (
                "false",
                "${{ false }}",
                '"false"',
                "'false'",
            ), f"quality step {step_name!r} must not have if: false"
            assert "pull_request" in step_if_str, (
                f"quality step {step_name!r} if: condition must reference pull_request"
            )
    else:
        assert step_if is None, f"quality step {step_name!r} must be unconditional"

    assert step.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
        f"quality step {step_name!r} must not have continue-on-error"
    )


def _check_quality_step_lines(step_name: str, run_cmd: str) -> None:
    if not run_cmd:
        return
    active_lines = [line.strip() for line in run_cmd.splitlines() if line.strip() and not line.strip().startswith("#")]
    if any(_has_pip_install(line) for line in active_lines):
        assert len(active_lines) == 1, (
            f"pip step {step_name!r} must have exactly 1 command line, got {len(active_lines)}"
        )
        return
    lines = parse_and_validate_gate_command_lines(step_name, run_cmd)
    assert len(lines) == 1, f"quality step {step_name!r} must have exactly 1 command line, got {len(lines)}"


def _validate_quality_single_step(
    step: dict[str, Any],
    detected_scripts: set[str],
) -> None:
    step_name = step.get("name", "unnamed step")
    validate_shell_configuration(f"quality step {step_name!r}", step)
    run_cmd = step.get("run", "")
    is_pr_title = _is_pr_title_step(step_name, run_cmd)
    allowed_env = frozenset({"PR_TITLE", "PR_BODY"}) if is_pr_title else frozenset()
    validate_env_configuration(f"quality step {step_name!r}", step, allowed_keys=allowed_env)
    _validate_quality_step_conditions(step, step_name, run_cmd)
    _check_quality_step_lines(step_name, run_cmd)

    if step.get("if") is None and step.get("continue-on-error") in (None, False, "false", "${{ false }}"):
        for s in MANDATORY_QUALITY_JOB_SCRIPTS:
            if _is_valid_script_invocation(run_cmd, s):
                detected_scripts.add(s)


def _validate_quality_job_steps(steps: list[Any]) -> None:
    detected_scripts: set[str] = set()
    uses_seen: set[str] = set()
    run_argvs_seen: set[tuple[str, ...]] = set()

    for step in steps:
        assert isinstance(step, dict), "quality job step must be a mapping"
        step_name = step.get("name", "unnamed step")
        validate_shell_configuration(f"quality step {step_name!r}", step)
        uses = step.get("uses")
        run_cmd = step.get("run")
        assert (uses is not None) ^ (run_cmd is not None), (
            f"quality step {step_name!r} must specify either 'uses' or 'run'"
        )
        if uses is not None:
            assert isinstance(uses, str)
            assert uses in QUALITY_JOB_ALLOWED_ACTIONS, (
                f"quality step {step_name!r} has unrecognized uses action: {uses!r}"
            )
            assert uses not in uses_seen, f"quality step {step_name!r} duplicate uses step: {uses!r}"
            uses_seen.add(uses)
            _validate_step_uses(step, uses, str(step_name), "quality")
            continue

        _validate_quality_single_step(step, detected_scripts)
        assert isinstance(run_cmd, str)
        lines = parse_and_validate_gate_command_lines(step_name, run_cmd)
        assert len(lines) == 1, f"quality step {step_name!r} must have exactly 1 command line, got {len(lines)}"
        cmd_tuple = tuple(lines[0])
        assert cmd_tuple in CANONICAL_QUALITY_RUN_ARGVS, (
            f"quality step {step_name!r} unrecognized run command: {lines[0]}"
        )
        assert cmd_tuple not in run_argvs_seen, f"quality step {step_name!r} duplicate run command: {lines[0]}"
        run_argvs_seen.add(cmd_tuple)

    for action in QUALITY_JOB_ALLOWED_ACTIONS:
        assert action in uses_seen, f"quality.yaml quality job missing required action: {action}"
    for cmd_tuple in CANONICAL_QUALITY_RUN_ARGVS:
        assert cmd_tuple in run_argvs_seen, f"quality.yaml quality job missing canonical run command: {cmd_tuple}"

    for s in MANDATORY_QUALITY_JOB_SCRIPTS:
        assert s in detected_scripts, f"quality job missing mandatory step running {s}"


def _validate_quality_workflow_job_header(job_key: str, job: dict[str, Any]) -> None:
    job_name = str(job.get("name", job_key))
    if not (job_key in ("quality", "tests", "test") or "Quality" in job_name or "Test" in job_name):
        return
    assert job.get("if") is None, f"required job '{job_key}' in quality.yaml must not have an if: condition"
    assert job.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
        f"job {job_key!r} must not specify continue-on-error"
    )
    validate_shell_configuration(f"quality.yaml job '{job_key}'", job)
    disallowed = sorted(set(job.keys()) - ALLOWED_JOB_LEVEL_KEYS)
    assert not disallowed, f"quality.yaml job '{job_key}' contains disallowed job-level keys: {', '.join(disallowed)}"
    assert job.get("runs-on") == "ubuntu-latest", (
        f"quality.yaml job '{job_key}' runs-on must be 'ubuntu-latest', got: {job.get('runs-on')!r}"
    )
    if "timeout-minutes" in job:
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and not isinstance(timeout, bool) and 5 <= timeout <= 60, (
            f"quality.yaml job '{job_key}' timeout-minutes must be an integer between 5 and 60, got: {timeout!r}"
        )
    if "permissions" in job:
        perms = job.get("permissions")
        assert perms == {"contents": "read"}, (
            f"quality.yaml job '{job_key}' permissions must be {{'contents': 'read'}}, got: {perms!r}"
        )


def validate_quality_workflow(doc: dict[str, Any]) -> None:
    """Validate quality.yaml contract, requiring unconditional jobs, valid coverage and format gates."""
    assert isinstance(doc, dict), "Quality workflow must parse as a mapping"
    wf_perms = doc.get("permissions")
    assert wf_perms == {"contents": "read"}, (
        f"quality.yaml permissions must be {{'contents': 'read'}}, got: {wf_perms!r}"
    )
    validate_shell_configuration("quality workflow", doc)
    validate_env_configuration("quality workflow", doc, allowed_keys=frozenset())
    jobs = doc.get("jobs", {})
    assert isinstance(jobs, dict), "quality workflow jobs must be a mapping"

    for job_key, job in jobs.items():
        if isinstance(job, dict):
            _validate_quality_workflow_job_header(job_key, job)

    quality_job = jobs.get("quality")
    assert isinstance(quality_job, dict), "quality.yaml missing required 'quality' job"
    validate_env_configuration("quality.yaml quality job", quality_job, allowed_keys=frozenset())
    q_steps = quality_job.get("steps", [])
    assert isinstance(q_steps, list), "quality.yaml quality job steps must be a list"
    _validate_quality_job_steps(q_steps)

    tests_job = jobs.get("tests") or jobs.get("test")
    assert isinstance(tests_job, dict), "quality.yaml missing required 'tests' job"
    validate_env_configuration("quality.yaml tests job", tests_job, allowed_keys=frozenset())
    steps = tests_job.get("steps", [])
    assert isinstance(steps, list), "quality.yaml tests job steps must be a list"
    _validate_quality_tests_steps(steps)

    extra_jobs = set(jobs.keys()) - {"quality", "tests", "test"}
    assert not extra_jobs, (
        f"quality.yaml jobs must contain only 'quality' and 'tests', got unexpected: {sorted(extra_jobs)}"
    )


def _find_and_validate_ancestor_step(steps: list[dict[str, Any]]) -> None:
    ancestor_steps = [
        s
        for s in steps
        if isinstance(s, dict)
        and (
            'git merge-base --is-ancestor "${{ github.event.workflow_run.head_sha || github.sha }}" origin/main'
            in s.get("run", "")
            or s.get("name") == "Verify commit is reachable from origin/main"
        )
    ]
    assert ancestor_steps, "'verify' job must contain step verifying commit is ancestor of origin/main"
    ancestor_step = ancestor_steps[0]
    validate_shell_configuration("ancestor check step", ancestor_step)
    validate_env_configuration("ancestor check step", ancestor_step, allowed_keys=frozenset())
    assert is_effective_step(ancestor_step), "ancestor check step must not bypass execution"
    assert "if" not in ancestor_step or not ancestor_step["if"], "ancestor step must be unconditional"
    assert ancestor_step.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
        "ancestor check step must not specify continue-on-error"
    )
    anc_argvs = parse_and_validate_gate_command_lines("ancestor check", ancestor_step.get("run", ""))
    assert len(anc_argvs) == 1 and anc_argvs[0] == EXPECTED_ANCESTOR_ARGV, (
        f"ancestor check command does not match expected: {anc_argvs}"
    )


def _find_and_validate_suite_step(steps: list[dict[str, Any]]) -> None:
    suite_steps = [s for s in steps if isinstance(s, dict) and "lint_skills.py" in s.get("run", "")]
    assert suite_steps, "'verify' job must contain full test suite step"
    suite_step = suite_steps[0]
    validate_shell_configuration("full test suite step", suite_step)
    validate_env_configuration("full test suite step", suite_step, allowed_keys=frozenset({"COVERAGE_SESSION_ID"}))
    assert is_effective_step(suite_step), "full test suite step must not bypass execution"
    assert "if" not in suite_step or not suite_step["if"], "full test suite step must be unconditional"
    assert suite_step.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
        "full test suite step must not specify continue-on-error"
    )

    suite_argvs = parse_and_validate_gate_command_lines("full test suite", suite_step.get("run", ""))
    assert ["python3", "-B", "scripts/lint_skills.py"] in suite_argvs, "missing lint_skills in full test suite"
    assert EXPECTED_RUFF_LINT_ARGV in suite_argvs, "missing exact ruff lint in full test suite"
    assert EXPECTED_RUFF_FORMAT_ARGV in suite_argvs, "missing exact ruff format in full test suite"

    pytest_cmds = [argv for argv in suite_argvs if argv[0] == "pytest"]
    assert pytest_cmds, "full test suite step must contain active pytest coverage command"
    validate_pytest_coverage_command(pytest_cmds[0], "full test suite")
    verify_cmds = [
        argv
        for argv in suite_argvs
        if argv
        in (
            ["python", "-m", "tests.test_coverage_threshold", "coverage.json"],
            ["python3", "-m", "tests.test_coverage_threshold", "coverage.json"],
            ["python", "-B", "-m", "tests.test_coverage_threshold", "coverage.json"],
            ["python3", "-B", "-m", "tests.test_coverage_threshold", "coverage.json"],
        )
    ]
    assert verify_cmds, "missing tests.test_coverage_threshold in full test suite"
    p_idx = suite_argvs.index(pytest_cmds[0])
    v_idx = suite_argvs.index(verify_cmds[0])
    assert v_idx == p_idx + 1, "release full test suite step: coverage threshold check must immediately follow pytest"


def _validate_verify_job_permissions(verify: dict[str, Any]) -> None:
    assert "permissions" in verify, "'verify' job must declare permissions"
    raw_perms = verify["permissions"]
    assert raw_perms != "write-all", "'verify' job must not have write-all permissions"
    assert isinstance(raw_perms, dict), "'verify' job must declare permissions mapping"
    assert raw_perms == {"contents": "read", "checks": "read"}, (
        f"'verify' job permissions must be exactly {{'contents': 'read', 'checks': 'read'}}, got: {raw_perms!r}"
    )


def _validate_verify_job_step_guards(steps: list[Any]) -> None:
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_name = step.get("name", "unnamed step")
        assert step.get("if") is None, f"step {step_name!r} in 'verify' job must be unconditional"
        assert step.get("continue-on-error") in (None, False, "false", "${{ false }}"), (
            f"step {step_name!r} in 'verify' job must not specify continue-on-error"
        )
        if "tools/release_gate.py" in step.get("run", "") or step_name == "Enforce Release Gate":
            validate_env_configuration(
                f"step {step_name!r}",
                step,
                allowed_keys=frozenset({"GH_TOKEN", "GITHUB_TOKEN"}),
            )
        elif "go vet" in step.get("run", ""):
            assert step.get("working-directory") == "tools/hclblocks"
            assert step.get("env") == {
                "GOTOOLCHAIN": "local",
                "GOWORK": "off",
                "GOFLAGS": "-mod=readonly",
            }
            validate_env_configuration(
                f"step {step_name!r}",
                step,
                allowed_keys=frozenset({"GOTOOLCHAIN", "GOWORK", "GOFLAGS"}),
            )
        elif "pytest" in step.get("run", ""):
            validate_env_configuration(
                f"step {step_name!r}",
                step,
                allowed_keys=frozenset({"COVERAGE_SESSION_ID"}),
            )
        else:
            validate_env_configuration(f"step {step_name!r}", step, allowed_keys=frozenset())


def _validate_verify_pip_step(steps: list[Any]) -> None:
    pip_steps = [
        s
        for s in steps
        if isinstance(s, dict)
        and any(
            _has_pip_install(line)
            for line in s.get("run", "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    ]
    assert len(pip_steps) == 1, f"'verify' job has {len(pip_steps)} pip install steps; exactly 1 required"
    for _pkg_name, expected_token in PINNED_TOOL_REQUIREMENTS.items():
        assert any(t == expected_token for t in pip_steps[0].get("run", "").split()), (
            f"'verify' job pip install missing {expected_token!r}"
        )


def _validate_verify_job(verify: dict[str, Any]) -> None:
    assert isinstance(verify, dict), "'verify' job must be a mapping"
    validate_shell_configuration("'verify' job", verify)
    validate_env_configuration("'verify' job", verify, allowed_keys=frozenset())
    assert is_effective_job(verify), "'verify' job must not have if: false or continue-on-error: true"
    _validate_job_level_if("verify", verify)
    _validate_verify_job_permissions(verify)

    steps = verify.get("steps", [])
    assert isinstance(steps, list), "'verify' job steps must be a list"
    _validate_verify_job_step_guards(steps)

    checkout_steps = [s for s in steps if isinstance(s, dict) and "checkout" in str(s.get("uses", ""))]
    assert checkout_steps, "'verify' job must contain a checkout step"
    _validate_checkout_step("verify", checkout_steps[0])

    _find_and_validate_ancestor_step(steps)
    _validate_verify_pip_step(steps)
    _find_and_validate_suite_step(steps)

    gate_steps = [s for s in steps if isinstance(s, dict) and "tools/release_gate.py" in s.get("run", "")]
    assert gate_steps, "'verify' job must contain a step running tools/release_gate.py"
    _validate_release_gate_step(gate_steps[0])


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
    assert s0.get("with") == expected_with, (
        "release job checkout must specify exact fetch-depth and ref (exact ref expression)"
    )
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


def _validate_release_publish_steps(steps: list[Any]) -> None:
    assert len(steps) == 5, f"expected 5 release steps, got {len(steps)}"
    _validate_release_publish_step_0(steps[0])
    _validate_release_publish_step_1(steps[1])
    _validate_release_publish_step_2(steps[2])
    _validate_release_publish_step_3(steps[3])
    _validate_release_publish_step_4(steps[4])


def _validate_release_job(release: dict[str, Any]) -> None:
    assert isinstance(release, dict), "'release' job must be a mapping"
    assert is_effective_job(release), "'release' job must not have if: false or continue-on-error: true"

    needs = release.get("needs")
    if isinstance(needs, str):
        assert needs == "verify", "'release' job must have needs: verify"
    elif isinstance(needs, list):
        assert "verify" in needs, "'release' job must include 'verify' in needs"
    else:
        raise AssertionError(f"'release' job needs must specify 'verify', got {needs!r}")

    release_if = str(release.get("if", "")).lower()
    assert "always()" not in release_if, "'release' job must not use if: always()"
    assert "failure()" not in release_if, "'release' job must not use if: failure()"
    assert "cancelled()" not in release_if, "'release' job must not use if: cancelled()"
    _validate_job_level_if("release", release)

    assert "permissions" in release, "'release' job must declare permissions"
    perms = release["permissions"]
    assert isinstance(perms, dict), "'release' job must declare permissions mapping"
    assert perms == {"contents": "write"}, (
        f"'release' job permissions must be exactly {{'contents': 'write'}}, got {perms!r}"
    )
    assert release.get("runs-on") == "ubuntu-latest", "'release' job runs-on must be 'ubuntu-latest'"
    assert "container" not in release, "'release' job must not declare container"
    assert "services" not in release, "'release' job must not declare services"
    assert "continue-on-error" not in release, "'release' job must not declare continue-on-error"

    steps = release.get("steps", [])
    assert isinstance(steps, list), "'release' job steps must be a list"
    _validate_release_publish_steps(steps)


def validate_release_workflow(doc: dict[str, Any]) -> None:
    """Validate that doc satisfies the release workflow contract."""
    assert isinstance(doc, dict), "workflow must parse as a valid YAML mapping"
    validate_shell_configuration("release workflow", doc)
    validate_env_configuration("release workflow", doc, allowed_keys=frozenset())
    on_triggers = doc.get("on")
    allowed = {"workflow_run", "workflow_dispatch"}
    if isinstance(on_triggers, dict):
        extra = set(on_triggers.keys()) - allowed
        assert not extra, f"release workflow triggers must only be {sorted(allowed)}, got: {sorted(extra)}"
    elif isinstance(on_triggers, list):
        extra = set(on_triggers) - allowed
        assert not extra, f"release workflow triggers must only be {sorted(allowed)}, got: {sorted(extra)}"
    wf_perms = doc.get("permissions")
    assert wf_perms == {"contents": "read"}, (
        f"release workflow permissions must be exactly {{'contents': 'read'}}, got {wf_perms!r}"
    )
    raw_doc: dict[Any, Any] = doc
    triggers = raw_doc.get("on")
    if triggers is None:
        triggers = raw_doc.get(True)
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
    extra_triggers = set(triggers.keys()) - {"workflow_run", "workflow_dispatch"}
    assert not extra_triggers, f"release workflow triggers must only be allowed triggers, got: {sorted(extra_triggers)}"

    jobs = doc.get("jobs", {})
    assert isinstance(jobs, dict), "jobs must be a mapping"

    assert "verify" in jobs, "release workflow must contain a 'verify' job"
    assert "release" in jobs, "release workflow must contain a 'release' job"

    _validate_verify_job(jobs["verify"])
    _validate_release_job(jobs["release"])


def test_workflow_runs_ruff_format_check_over_whole_tree() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "Quality workflow must parse as a valid YAML mapping"
    assert has_active_format_gate(doc), (
        "Quality workflow must contain an active (uncommented) step running `ruff format --check` over `.`"
    )


def test_release_workflow_runs_ruff_format_check() -> None:
    doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "Release workflow must parse as a valid YAML mapping"
    assert has_active_format_gate(doc), (
        "Release workflow verify job must contain an active step running `ruff format --check` over `.`"
    )


def test_release_workflow_contract() -> None:
    doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    validate_release_workflow(doc)


def test_quality_workflow_contract() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    validate_quality_workflow(doc)


class TestFormatGateNegativeMutants:
    """Negative tests proving that disabling or bypassing the gate causes verification to fail."""

    def test_format_gate_fails_when_colon_prefix(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "format" in step.get("run", ""):
                step["run"] = ": python -m ruff format --check --diff ."
        assert has_active_format_gate(doc) is False
        with pytest.raises(AssertionError, match="banned prefix"):
            validate_quality_workflow(doc)

    def test_format_gate_fails_when_custom_shell_on_step(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "format" in step.get("run", ""):
                step["shell"] = "bash {0}"
        assert has_active_format_gate(doc) is False
        with pytest.raises(AssertionError, match="illegal shell override"):
            validate_quality_workflow(doc)

    def test_format_gate_fails_when_custom_shell_on_job(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["tests"]["defaults"] = {"run": {"shell": "bash {0}"}}
        assert has_active_format_gate(doc) is False
        with pytest.raises(AssertionError, match="illegal shell override"):
            validate_quality_workflow(doc)

    def test_format_gate_fails_when_custom_shell_on_workflow(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["defaults"] = {"run": {"shell": "bash {0}"}}
        assert has_active_format_gate(doc) is False
        with pytest.raises(AssertionError, match="illegal shell override"):
            validate_quality_workflow(doc)

    def test_quality_fails_when_job_if_false(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["quality"]["if"] = "${{ false }}"
        with pytest.raises(AssertionError, match="must not have an if: condition"):
            validate_quality_workflow(doc)

    def test_quality_fails_when_tests_job_if_false(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["tests"]["if"] = "${{ false }}"
        with pytest.raises(AssertionError, match="must not have an if: condition"):
            validate_quality_workflow(doc)

    def test_quality_fails_when_workflow_env_collect_only(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_quality_fails_when_workflow_env_no_cov(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["env"] = {"PYTEST_ADDOPTS": "--no-cov"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_quality_fails_when_tests_job_env_collect_only(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["tests"]["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
        with pytest.raises(
            AssertionError, match="disallowed env variable|must not declare env mapping|disallowed job-level keys"
        ):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        assert any(
            "disallowed env variable" in e or "banned env variable" in e or "disallowed job-level keys" in e
            for e in cli_validate(doc)
        )

    def test_quality_fails_when_tests_job_env_no_cov(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["tests"]["env"] = {"PYTEST_ADDOPTS": "--no-cov"}
        with pytest.raises(
            AssertionError, match="disallowed env variable|must not declare env mapping|disallowed job-level keys"
        ):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        assert any(
            "disallowed env variable" in e or "banned env variable" in e or "disallowed job-level keys" in e
            for e in cli_validate(doc)
        )

    def test_quality_fails_when_coverage_step_env_collect_only(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "pytest" in step.get("run", ""):
                step["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_quality_fails_when_coverage_step_env_no_cov(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "pytest" in step.get("run", ""):
                step["env"] = {"PYTEST_ADDOPTS": "--no-cov"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    @pytest.mark.parametrize(
        "target_script",
        [
            "go vet ./...",
            "python tools/validate_skills.py",
            "python tools/validate_marketplace.py",
            "python tools/tf_module_layout.py --self-test",
            "python -m unittest discover -s skills/portfolio-delivery-control/tests -v",
        ],
    )
    def test_quality_fails_when_required_step_if_condition(self, target_script: str) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if step.get("run") == target_script:
                step["if"] = "${{ 1 == 2 }}"
        with pytest.raises(AssertionError, match="must be unconditional"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        assert any("must not have an if: condition" in e for e in cli_validate(doc))

    @pytest.mark.parametrize(
        "target_script",
        [
            "lint_skills.py",
            "privacy_scan.py",
            "test_privacy_scan.py",
            "security_scan.py",
            "test_security_scan.py",
            "public_api_stability.py",
            "test_pr_workflow_cancellation.py",
            "test_semantic_release.py",
        ],
    )
    def test_quality_fails_when_quality_job_mandatory_step_if_condition(self, target_script: str) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        modified = False
        for step in doc["jobs"]["quality"]["steps"]:
            if target_script in step.get("run", ""):
                step["if"] = "${{ 1 == 2 }}"
                modified = True
        assert modified, f"Step running {target_script} not found in quality job"
        with pytest.raises(AssertionError, match="must be unconditional"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        errs = cli_validate(doc)
        assert any("must not have an if: condition" in e for e in errs)

    @pytest.mark.parametrize(
        "target_script",
        [
            "lint_skills.py",
            "privacy_scan.py",
            "test_privacy_scan.py",
            "security_scan.py",
            "test_security_scan.py",
            "public_api_stability.py",
            "test_pr_workflow_cancellation.py",
            "test_semantic_release.py",
        ],
    )
    def test_quality_fails_when_quality_job_mandatory_step_continue_on_error(self, target_script: str) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        modified = False
        for step in doc["jobs"]["quality"]["steps"]:
            if target_script in step.get("run", ""):
                step["continue-on-error"] = True
                modified = True
        assert modified, f"Step running {target_script} not found in quality job"
        with pytest.raises(AssertionError, match="must not have continue-on-error"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        errs = cli_validate(doc)
        assert any("must not have continue-on-error" in e for e in errs)

    def test_quality_fails_when_secondary_pip_install_step(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["tests"]["steps"].insert(3, {"name": "Reinstall ruff", "run": "pip install ruff==0.9.10"})
        with pytest.raises(AssertionError, match="pip install step|Disallowed secondary pip install"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        assert any("pip install step" in e or "secondary pip install" in e for e in cli_validate(doc))

    def test_quality_fails_when_quality_job_missing(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        del doc["jobs"]["quality"]
        with pytest.raises(AssertionError, match="missing required 'quality' job"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        errs = cli_validate(doc)
        assert any("missing target tests/quality job" in e or "missing required 'quality' job" in e for e in errs)

    def test_quality_fails_when_pr_title_spoofed_on_mandatory_script(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["quality"]["steps"]:
            if "privacy_scan.py" in step.get("run", ""):
                step["name"] = "Validate PR title and privacy"
                step["if"] = "github.event_name == 'pull_request' && 1 == 2"
        with pytest.raises(AssertionError, match="must be unconditional|missing mandatory step"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        errs = cli_validate(doc)
        assert any("must not have an if: condition" in e for e in errs) or any(
            "missing required step: privacy_scan.py" in e for e in errs
        )

    def test_quality_fails_when_python_c_argument_bypass(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["quality"]["steps"]:
            if "privacy_scan.py" in step.get("run", ""):
                step["run"] = 'python3 -c "pass" scripts/privacy_scan.py'
        with pytest.raises(AssertionError, match="unrecognized run command|missing mandatory step"):
            validate_quality_workflow(doc)
        from tools.validate_skills import validate_quality_workflow as cli_validate

        errs = cli_validate(doc)
        assert any("unrecognized run command" in e or "missing required step: privacy_scan.py" in e for e in errs)

    def test_quality_fails_when_cov_fail_under_zero(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "pytest" in step.get("run", ""):
                step["run"] = step["run"].replace("--cov-fail-under=90", "--cov-fail-under=0")
        with pytest.raises(AssertionError, match="coverage floor 0 < 90|unrecognized run command"):
            validate_quality_workflow(doc)

    def test_quality_fails_when_format_prefixed_with_true(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "format" in step.get("run", ""):
                step["run"] = "true " + step["run"]
        with pytest.raises(AssertionError, match="banned prefix"):
            validate_quality_workflow(doc)

    @pytest.mark.parametrize(
        "bad_with",
        [
            {"ref": "main"},
            {"ref": "84ef88038cc25582d6a54338ef77b570bc833340"},
            {"repository": "other/agent-skills"},
            {"path": "sub"},
            {"sparse-checkout": "tools"},
        ],
    )
    @pytest.mark.parametrize("target_job", ["quality", "tests"])
    def test_quality_fails_when_checkout_has_with_inputs(self, bad_with: dict[str, Any], target_job: str) -> None:
        from scripts.test_pr_workflow_cancellation import validate_workflow_steps
        from tools.validate_marketplace import validate_quality_workflow as vm_val
        from tools.validate_skills import validate_quality_workflow as vs_val

        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"][target_job]["steps"]:
            if step.get("uses") == "actions/checkout@v7.0.0":
                step["with"] = dict(bad_with)

        with pytest.raises(AssertionError, match="disallowed 'with:' on actions/checkout"):
            validate_quality_workflow(doc)

        with pytest.raises(AssertionError, match="disallowed 'with:' on actions/checkout"):
            validate_workflow_steps("quality.yaml", doc)

        vs_errs = vs_val(doc)
        assert any("disallowed 'with:' on actions/checkout" in e for e in vs_errs)
        vm_errs = vm_val(doc)
        assert any("disallowed 'with:' on actions/checkout" in e for e in vm_errs)

    @pytest.mark.parametrize(
        "bad_with",
        [
            {"python-version": "3.11"},
            {},
            {"python-version": "3.12", "cache": "pip"},
        ],
    )
    @pytest.mark.parametrize("target_job", ["quality", "tests"])
    def test_quality_fails_when_setup_python_has_invalid_with(self, bad_with: dict[str, Any], target_job: str) -> None:
        from scripts.test_pr_workflow_cancellation import validate_workflow_steps
        from tools.validate_marketplace import validate_quality_workflow as vm_val
        from tools.validate_skills import validate_quality_workflow as vs_val

        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"][target_job]["steps"]:
            if step.get("uses") == "actions/setup-python@v6.3.0":
                step["with"] = dict(bad_with)

        with pytest.raises(
            AssertionError, match="invalid setup-python 'with:'|actions/setup-python 'with:' must be exactly"
        ):
            validate_quality_workflow(doc)

        with pytest.raises(
            AssertionError, match="invalid setup-python 'with:'|actions/setup-python 'with:' must be exactly"
        ):
            validate_workflow_steps("quality.yaml", doc)

        vs_errs = vs_val(doc)
        assert any(
            "actions/setup-python 'with:' must be exactly" in e or "invalid setup-python 'with:'" in e for e in vs_errs
        )
        vm_errs = vm_val(doc)
        assert any(
            "actions/setup-python 'with:' must be exactly" in e or "invalid setup-python 'with:'" in e for e in vm_errs
        )

    def test_quality_fails_when_set_plus_o_errexit(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "format" in step.get("run", ""):
                step["run"] = "set +o errexit\n" + step["run"]
        with pytest.raises(AssertionError, match="banned prefix"):
            validate_quality_workflow(doc)

    def test_quality_fails_when_validate_skills_command_is_echoed(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "tools/validate_skills.py" in step.get("run", ""):
                step["run"] = "echo python tools/validate_skills.py"
        with pytest.raises(AssertionError, match="banned prefix"):
            validate_quality_workflow(doc)

    def test_quality_fails_when_validate_skills_command_masks_failure(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "tools/validate_skills.py" in step.get("run", ""):
                step["run"] = "python tools/validate_skills.py || true"
        with pytest.raises(AssertionError, match="banned shell operator"):
            validate_quality_workflow(doc)

    def test_quality_fails_when_privacy_scan_prefixed_with_exit_zero(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["quality"]["steps"]:
            if "privacy_scan.py" in step.get("run", ""):
                step["run"] = "exit 0\npython scripts/privacy_scan.py"
        with pytest.raises(AssertionError, match="banned prefix|must have exactly 1 command line"):
            validate_quality_workflow(doc)

    def test_all_validators_reject_reordering_verify_before_pytest_in_quality_workflow(self) -> None:
        from scripts.test_pr_workflow_cancellation import validate_workflow_steps
        from tools.validate_marketplace import validate_quality_workflow as vm_val
        from tools.validate_skills import validate_quality_workflow as vs_val

        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = doc["jobs"]["tests"]["steps"]
        pytest_idx = next(i for i, s in enumerate(steps) if "pytest" in s.get("run", ""))
        verify_idx = next(i for i, s in enumerate(steps) if "test_coverage_threshold" in s.get("run", ""))
        assert pytest_idx < verify_idx
        verify_step = steps.pop(verify_idx)
        steps.insert(pytest_idx, verify_step)

        with pytest.raises(AssertionError, match="out of order"):
            validate_quality_workflow(doc)

        with pytest.raises(AssertionError, match="out of order"):
            validate_workflow_steps("quality.yaml", doc)

        vs_errs = vs_val(doc)
        assert any("out of order" in e for e in vs_errs)
        vm_errs = vm_val(doc)
        assert any("out of order" in e for e in vm_errs)

    def test_format_gate_fails_when_commented_out(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {"run": "# ruff format --check --diff ."},
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_command_is_echoed(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {"run": 'echo "ruff format --check --diff ."'},
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_step_if_false(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {
                            "if": False,
                            "run": "ruff format --check --diff .",
                        },
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_step_if_expression_false(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {
                            "if": "${{ false }}",
                            "run": "ruff format --check --diff .",
                        },
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_step_continue_on_error(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {
                            "continue-on-error": True,
                            "run": "ruff format --check --diff .",
                        },
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_job_if_false(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "if": False,
                    "steps": [
                        {"run": "ruff format --check --diff ."},
                    ],
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_job_continue_on_error(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "continue-on-error": True,
                    "steps": [
                        {"run": "ruff format --check --diff ."},
                    ],
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_failure_masked_or_true(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {"run": "ruff format --check --diff . || true"},
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_step_has_if_condition(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {
                            "if": "${{ 1 == 2 }}",
                            "run": "ruff format --check --diff .",
                        },
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_errexit_disabled(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {
                            "run": "set +e\nruff format --check --diff .\ntrue",
                        },
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_trailing_true(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {
                            "run": "ruff format --check --diff .\ntrue",
                        },
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_format_gate_fails_when_missing(self) -> None:
        doc = {
            "jobs": {
                "tests": {
                    "steps": [
                        {"run": "pytest --cov=tools"},
                    ]
                }
            }
        }
        assert has_active_format_gate(doc) is False

    def test_all_validators_reject_github_env_writer_injection(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
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
        _assert_all_validators_reject(doc, "GITHUB_ENV writer step injection")

    def test_all_validators_reject_workflow_defaults_working_directory(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["defaults"] = {"run": {"working-directory": "tests/fixtures/sol61_shadow"}}
        _assert_all_validators_reject(doc, "workflow-level defaults.run.working-directory")

    def test_all_validators_reject_job_defaults_working_directory(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["tests"]["defaults"] = {"run": {"working-directory": "tests/fixtures/sol61_shadow"}}
        _assert_all_validators_reject(doc, "job-level defaults.run.working-directory")

    def test_all_validators_reject_step_working_directory(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["tests"]["steps"][3]["working-directory"] = "tests/fixtures/sol61_shadow"
        _assert_all_validators_reject(doc, "step-level working-directory")

    @pytest.mark.parametrize("target_job", ["quality", "tests"])
    @pytest.mark.parametrize(
        "key,value,label",
        [
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
        ],
    )
    def test_all_validators_reject_job_level_key_mutants(
        self, target_job: str, key: str, value: Any, label: str
    ) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"][target_job][key] = value
        _assert_all_validators_reject(doc, f"{target_job} job {label}")

    def test_all_validators_reject_extra_job_in_quality_workflow(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["extra"] = {"runs-on": "ubuntu-latest", "steps": []}
        _assert_all_validators_reject(doc, "extra job in quality workflow")

    @pytest.mark.parametrize(
        "perm_val,label",
        [
            ("write-all", "permissions write-all"),
            ("read-all", "permissions read-all"),
            ({"contents": "write"}, "permissions contents write"),
            ({"contents": "read", "issues": "write"}, "permissions extra issues write"),
            ({"contents": "read", "actions": "write"}, "permissions extra actions write"),
            (None, "missing permissions"),
        ],
    )
    def test_all_validators_reject_workflow_permissions_mutants(self, perm_val: Any, label: str) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        if perm_val is None:
            doc.pop("permissions", None)
        else:
            doc["permissions"] = perm_val
        _assert_all_validators_reject(doc, f"quality workflow {label}")


def _assert_all_validators_reject(doc: dict[str, Any], label: str) -> None:
    from scripts.test_pr_workflow_cancellation import validate_workflow_steps as sp_val
    from tools.validate_marketplace import validate_quality_workflow as vm_val
    from tools.validate_skills import validate_quality_workflow as vs_val

    e1 = vs_val(doc)
    e2 = vm_val(doc)
    assert len(e1) > 0, f"{label}: tools.validate_skills accepted mutant"
    assert len(e2) > 0, f"{label}: tools.validate_marketplace accepted mutant"

    sp_failed = False
    try:
        sp_val("quality.yaml", doc)
    except AssertionError:
        sp_failed = True
    assert sp_failed, f"{label}: scripts.test_pr_workflow_cancellation accepted mutant"

    fg_failed = False
    try:
        validate_quality_workflow(doc)
    except AssertionError:
        fg_failed = True
    assert fg_failed, f"{label}: tests.test_format_gate accepted mutant"


def _mutate_suite_step_coverage_order(run_cmd: str) -> str:
    lines = run_cmd.splitlines()
    p_idx = -1
    v_idx = -1
    for i, line in enumerate(lines):
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue
        if "pytest" in clean:
            p_idx = i
        elif "test_coverage_threshold" in clean:
            v_idx = i
    assert p_idx != -1 and v_idx != -1 and p_idx < v_idx
    v_line = lines.pop(v_idx)
    lines.insert(p_idx, v_line)
    return "\n".join(lines)


class TestReleaseWorkflowNegativeMutants:
    """Negative tests proving that tampering with the release gate or dependencies fails validation."""

    def test_deleting_release_gate_step_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        steps = doc["jobs"]["verify"]["steps"]
        doc["jobs"]["verify"]["steps"] = [s for s in steps if "tools/release_gate.py" not in s.get("run", "")]
        with pytest.raises(AssertionError, match="must contain a step running tools/release_gate.py"):
            validate_release_workflow(doc)

    def test_removing_needs_verify_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        del doc["jobs"]["release"]["needs"]
        with pytest.raises(AssertionError, match="needs must specify 'verify'"):
            validate_release_workflow(doc)

    def test_adding_if_always_to_release_job_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["release"]["if"] = "always()"
        with pytest.raises(AssertionError, match="must not use if: always()"):
            validate_release_workflow(doc)

    def test_giving_verify_job_contents_write_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["permissions"]["contents"] = "write"
        with pytest.raises(AssertionError, match="permissions must be exactly"):
            validate_release_workflow(doc)

    @pytest.mark.parametrize(
        "extra_scope",
        ["actions", "issues", "id-token", "pull-requests", "packages"],
    )
    def test_giving_verify_job_extra_write_scopes_fails(self, extra_scope: str) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["permissions"][extra_scope] = "write"
        with pytest.raises(AssertionError, match="permissions must be exactly"):
            validate_release_workflow(doc)

    def test_appending_or_true_to_gate_command_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = step["run"] + " || true"
        with pytest.raises(AssertionError, match="must not mask failures|banned shell operator"):
            validate_release_workflow(doc)

    def test_conditional_gate_step_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["if"] = "success()"
        with pytest.raises(AssertionError, match="must be unconditional"):
            validate_release_workflow(doc)

    def test_shallow_verify_checkout_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "checkout" in str(step.get("uses", "")):
                step["with"] = {}
        with pytest.raises(AssertionError, match="checkout must specify fetch-depth: 0"):
            validate_release_workflow(doc)

    def test_gate_echo_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = "echo " + step["run"]
        with pytest.raises(AssertionError):
            validate_release_workflow(doc)

    def test_gate_commented_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = "# " + step["run"] + "\ntrue"
        with pytest.raises(AssertionError):
            validate_release_workflow(doc)

    def test_gate_errexit_disabled_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = "set +e\n" + step["run"] + "\ntrue"
        with pytest.raises(AssertionError):
            validate_release_workflow(doc)

    def test_wrong_gate_sha_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = step["run"].replace(
                    '--sha "${{ github.event.workflow_run.head_sha || github.sha }}"',
                    '--sha "arbitrary-other-commit"',
                )
        with pytest.raises(
            AssertionError, match="exact SHA expression|release gate command does not match expected exact argv"
        ):
            validate_release_workflow(doc)

    def test_wait_seconds_zero_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = step["run"].replace("--wait-seconds 600", "--wait-seconds 0")
        with pytest.raises(
            AssertionError, match="--wait-seconds >= 60|release gate command does not match expected exact argv"
        ):
            validate_release_workflow(doc)

    def test_revert_trigger_to_push_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["on"] = {"push": {"branches": ["main"]}}
        with pytest.raises(AssertionError, match="workflow_run"):
            validate_release_workflow(doc)

    def test_missing_conclusion_success_if_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["if"] = doc["jobs"]["verify"]["if"].replace(
            "github.event.workflow_run.conclusion == 'success'", ""
        )
        with pytest.raises(AssertionError, match="missing required clause"):
            validate_release_workflow(doc)

    def test_release_checkout_wrong_ref_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["release"]["steps"]:
            if "checkout" in str(step.get("uses", "")):
                step["with"]["ref"] = "${{ github.head_ref }}"
        with pytest.raises(AssertionError, match="exact ref expression"):
            validate_release_workflow(doc)

    def test_ruff_unpinned_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "pip install" in step.get("run", ""):
                step["run"] = step["run"].replace("ruff==0.15.21", "ruff==0.9.10")
        with pytest.raises(AssertionError, match="pip install missing 'ruff==0.15.21'|pin ruff==0.15.21"):
            validate_release_workflow(doc)

    def test_gate_wrapped_in_if_false_shell_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = "if false; then\n  " + step["run"] + "\nfi"
        with pytest.raises(
            AssertionError, match="must not mask failures or use conditionals|banned shell operator|banned prefix"
        ):
            validate_release_workflow(doc)

    def test_gate_or_test_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = step["run"] + " || test $? -ne 0"
        with pytest.raises(AssertionError, match="must not mask failures or use conditionals|banned shell operator"):
            validate_release_workflow(doc)

    def test_gate_if_failure_suppression_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = "if " + step["run"] + "; then printf passed; fi"
        with pytest.raises(
            AssertionError, match="must not mask failures or use conditionals|banned shell operator|banned prefix"
        ):
            validate_release_workflow(doc)

    def test_gate_continue_on_error_expression_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["continue-on-error"] = "${{ 1 == 1 }}"
        with pytest.raises(AssertionError, match="continue-on-error"):
            validate_release_workflow(doc)

    def test_ancestor_wrapped_in_if_false_shell_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "git merge-base" in step.get("run", ""):
                step["run"] = "if false; then\n  " + step["run"] + "\nfi"
        with pytest.raises(
            AssertionError, match="must not mask failures or use conditionals|banned shell operator|banned prefix"
        ):
            validate_release_workflow(doc)

    def test_format_wrapped_in_if_false_shell_fails(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "format" in step.get("run", ""):
                step["run"] = "if false; then\n  " + step["run"] + "\nfi"
        assert has_active_format_gate(doc) is False

    def test_format_continue_on_error_expression_fails(self) -> None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["tests"]["steps"]:
            if "format" in step.get("run", ""):
                step["continue-on-error"] = "${{ 1 == 1 }}"
        assert has_active_format_gate(doc) is False

    def test_job_guard_prefixed_with_or_true_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["if"] = "true || (" + doc["jobs"]["verify"]["if"] + ")"
        with pytest.raises(AssertionError, match="illegal bypass expression|must have exactly 2 top-level branches"):
            validate_release_workflow(doc)

    def test_release_job_guard_prefixed_with_or_true_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["release"]["if"] = "true || (" + doc["jobs"]["release"]["if"] + ")"
        with pytest.raises(AssertionError, match="illegal bypass expression|must have exactly 2 top-level branches"):
            validate_release_workflow(doc)

    def test_missing_permissions_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        del doc["jobs"]["verify"]["permissions"]
        with pytest.raises(AssertionError, match="declare permissions"):
            validate_release_workflow(doc)

    def test_release_missing_permissions_fails(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        del doc["jobs"]["release"]["permissions"]
        with pytest.raises(AssertionError, match="declare permissions"):
            validate_release_workflow(doc)

    def test_release_fails_when_cov_fail_under_zero(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "pytest" in step.get("run", ""):
                step["run"] = step["run"].replace("--cov-fail-under=90", "--cov-fail-under=0")
        with pytest.raises(AssertionError, match="coverage floor 0 < 90"):
            validate_release_workflow(doc)

    def test_release_fails_when_custom_shell_on_step(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["shell"] = "bash {0}"
        with pytest.raises(AssertionError, match="illegal shell override"):
            validate_release_workflow(doc)

    def test_release_fails_when_custom_shell_on_job(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["defaults"] = {"run": {"shell": "bash {0}"}}
        with pytest.raises(AssertionError, match="illegal shell override"):
            validate_release_workflow(doc)

    def test_release_fails_when_custom_shell_on_workflow(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["defaults"] = {"run": {"shell": "bash {0}"}}
        with pytest.raises(AssertionError, match="illegal shell override"):
            validate_release_workflow(doc)

    def test_release_fails_when_gate_prefixed_with_true(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = "true " + step["run"]
        with pytest.raises(AssertionError, match="banned prefix"):
            validate_release_workflow(doc)

    def test_release_fails_when_gate_prefixed_with_colon(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = ": " + step["run"]
        with pytest.raises(AssertionError, match="banned prefix"):
            validate_release_workflow(doc)

    def test_release_fails_when_gate_prefixed_with_not(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = "! " + step["run"]
        with pytest.raises(AssertionError, match="banned prefix"):
            validate_release_workflow(doc)

    def test_release_fails_when_set_plus_o_errexit(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = "set +o errexit\n" + step["run"]
        with pytest.raises(AssertionError, match="banned prefix"):
            validate_release_workflow(doc)

    def test_release_fails_when_empty_expected_app_slug(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["run"] = step["run"].replace('--expected-app-slug "github-actions"', '--expected-app-slug ""')
        with pytest.raises(AssertionError, match="release gate command does not match expected exact argv"):
            validate_release_workflow(doc)

    def test_release_fails_when_workflow_env_collect_only(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_release_workflow(doc)
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_release_fails_when_workflow_env_no_cov(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["env"] = {"PYTEST_ADDOPTS": "--no-cov"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_release_workflow(doc)
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_release_fails_when_verify_job_env_collect_only(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_release_workflow(doc)
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_release_fails_when_verify_job_env_no_cov(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["env"] = {"PYTEST_ADDOPTS": "--no-cov"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_release_workflow(doc)
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_release_fails_when_suite_step_env_collect_only(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "pytest" in step.get("run", ""):
                step["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_release_workflow(doc)
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_release_fails_when_suite_step_env_no_cov(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "pytest" in step.get("run", ""):
                step["env"] = {"PYTEST_ADDOPTS": "--no-cov"}
        with pytest.raises(AssertionError, match="disallowed env variable|must not declare env mapping"):
            validate_release_workflow(doc)
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_release_fails_when_gate_step_env_collect_only(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "tools/release_gate.py" in step.get("run", ""):
                step["env"]["PYTEST_ADDOPTS"] = "--collect-only"
        with pytest.raises(AssertionError, match="disallowed env variable|banned env variable"):
            validate_release_workflow(doc)
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        assert any("disallowed env variable" in e or "banned env variable" in e for e in cli_validate(doc))

    def test_release_fails_when_secondary_pip_install_step(self) -> None:
        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["verify"]["steps"].insert(4, {"name": "Reinstall ruff", "run": "pip install ruff==0.9.10"})
        with pytest.raises(AssertionError, match="pip install steps"):
            validate_release_workflow(doc)
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        assert any("pip install steps" in e or "secondary pip install" in e for e in cli_validate(doc))

    def test_release_fails_when_verify_command_reordered_before_pytest(self) -> None:
        from scripts.test_pr_workflow_cancellation import validate_workflow_steps

        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        for step in doc["jobs"]["verify"]["steps"]:
            if "lint_skills.py" in step.get("run", ""):
                step["run"] = _mutate_suite_step_coverage_order(step["run"])
                break

        with pytest.raises(AssertionError, match="coverage threshold"):
            validate_release_workflow(doc)

        with pytest.raises(AssertionError, match="coverage threshold"):
            validate_workflow_steps("release.yaml", doc)

    def test_release_publish_extra_write_scopes_fail(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        for bad_scope in (
            {"contents": "write", "actions": "write"},
            {"contents": "write", "id-token": "write"},
            {"contents": "write", "issues": "write"},
            "write-all",
        ):
            doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
            doc["jobs"]["release"]["permissions"] = bad_scope
            with pytest.raises(AssertionError, match="release.*permissions"):
                validate_release_workflow(doc)
            assert any("permissions" in e for e in cli_validate(doc))

    def test_release_publish_extra_step_fails(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["release"]["steps"].append({"name": "exfil", "run": "curl https://example.com"})
        with pytest.raises(AssertionError, match="expected 5 release steps"):
            validate_release_workflow(doc)
        assert any("canonical steps" in e for e in cli_validate(doc))

    def test_release_publish_modified_command_fails(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["release"]["steps"][4]["run"] = "gh release create evil"
        with pytest.raises(AssertionError, match="canonical gh release create"):
            validate_release_workflow(doc)
        assert any("canonical gh release create" in e for e in cli_validate(doc))

    def test_release_top_level_permissions_mutant_fails(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        for bad_perm in ("write-all", {"contents": "write"}, {"contents": "read", "id-token": "write"}):
            doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
            doc["permissions"] = bad_perm
            with pytest.raises(AssertionError, match="release workflow permissions"):
                validate_release_workflow(doc)
            assert any("permissions" in e for e in cli_validate(doc))

    def test_release_publish_unpinned_actions_fail(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        doc1 = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc1["jobs"]["release"]["steps"][0]["uses"] = "actions/checkout@main"
        with pytest.raises(AssertionError, match="pinned actions/checkout"):
            validate_release_workflow(doc1)
        assert any("pinned actions/checkout" in e for e in cli_validate(doc1))

        doc2 = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc2["jobs"]["release"]["steps"][1]["uses"] = "actions/setup-python@attacker-branch"
        with pytest.raises(AssertionError, match="pinned actions/setup-python"):
            validate_release_workflow(doc2)
        assert any("pinned actions/setup-python" in e for e in cli_validate(doc2))

    def test_release_publish_checkout_repo_override_fails(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc["jobs"]["release"]["steps"][0]["with"]["repository"] = "attacker/project"
        with pytest.raises(AssertionError, match="exact fetch-depth and ref"):
            validate_release_workflow(doc)
        assert any("exact fetch-depth and ref" in e for e in cli_validate(doc))

    def test_release_publish_environment_mutants_fail(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        doc_sh = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc_sh["jobs"]["release"]["runs-on"] = "self-hosted"
        with pytest.raises(AssertionError, match="runs-on"):
            validate_release_workflow(doc_sh)
        assert any("runs-on" in e for e in cli_validate(doc_sh))

        doc_ct = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc_ct["jobs"]["release"]["container"] = "attacker/image:latest"
        with pytest.raises(AssertionError, match="container"):
            validate_release_workflow(doc_ct)
        assert any("container" in e for e in cli_validate(doc_ct))

        doc_sv = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc_sv["jobs"]["release"]["services"] = {"redis": {"image": "redis"}}
        with pytest.raises(AssertionError, match="services"):
            validate_release_workflow(doc_sv)
        assert any("services" in e for e in cli_validate(doc_sv))

        doc_env = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc_env["jobs"]["release"]["steps"][2]["env"] = {"GH_TOKEN": "stolen"}
        with pytest.raises(AssertionError, match="must not declare env"):
            validate_release_workflow(doc_env)
        assert any("must not declare env" in e for e in cli_validate(doc_env))

    def test_release_publish_command_mutants_fail(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        doc_plan = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc_plan["jobs"]["release"]["steps"][2]["run"] += "\ngit push origin HEAD:refs/heads/unreviewed"
        with pytest.raises(AssertionError, match="exact semantic_release.py"):
            validate_release_workflow(doc_plan)
        assert any("exact canonical semantic_release.py" in e for e in cli_validate(doc_plan))

        doc_tag = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        doc_tag["jobs"]["release"]["steps"][3]["run"] += "\ngit push origin HEAD:refs/heads/unreviewed"
        with pytest.raises(AssertionError, match="exact canonical tag commands"):
            validate_release_workflow(doc_tag)
        assert any("exact canonical git tag and push" in e for e in cli_validate(doc_tag))

    def test_release_disallowed_trigger_fails(self) -> None:
        from tools.validate_marketplace import validate_release_workflow as cli_validate

        doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        on_key = "on" if "on" in doc else True
        doc[on_key]["pull_request_target"] = {}
        with pytest.raises(AssertionError, match="release workflow triggers"):
            validate_release_workflow(doc)
        assert any("release workflow triggers" in e for e in cli_validate(doc))

    def test_execution_probe_failing_gate_command_exits_nonzero(self) -> None:
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


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ruff_format_check_fails_on_misformatted_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(MISFORMATTED, encoding="utf-8")
    result = subprocess.run(
        ["ruff", "format", "--check", str(bad)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "gate must fail on a misformatted file"


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ruff_format_check_passes_on_formatted_file(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(FORMATTED, encoding="utf-8")
    result = subprocess.run(
        ["ruff", "format", "--check", str(good)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"gate must pass on a clean file; stderr: {result.stderr}"


def test_ruff_is_pinned_consistently_across_repo() -> None:
    release_doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    verify_steps = release_doc["jobs"]["verify"]["steps"]
    assert any("ruff==0.15.21" in s.get("run", "") for s in verify_steps if isinstance(s, dict))

    quality_doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    test_steps = quality_doc["jobs"]["tests"]["steps"]
    assert any("ruff==0.15.21" in s.get("run", "") for s in test_steps if isinstance(s, dict))

    pre_commit = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    ruff_repo = next(r for r in pre_commit.get("repos", []) if "ruff-pre-commit" in r.get("repo", ""))
    assert ruff_repo.get("rev") == "v0.15.21"


def _find_setup_go_step(steps: list[Any]) -> dict[str, Any]:
    step = next((s for s in steps if isinstance(s, dict) and "actions/setup-go" in str(s.get("uses", ""))), None)
    assert step is not None, "missing actions/setup-go step"
    return step


def test_setup_go_is_pinned_by_sha_and_exact_version() -> None:
    """TS25-01 / AS-TS-R25-SOL61-01 / AS-TS-R26-SOL61-01: CI workflows must pin actions/setup-go
    by 40-hex commit SHA and specify exact go-version: '1.26.10'.
    """
    quality_doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    go_step = _find_setup_go_step(quality_doc["jobs"]["tests"]["steps"])
    uses = str(go_step.get("uses", ""))
    assert re.fullmatch(r"actions/setup-go@[0-9a-fA-F]{40}", uses), (
        f"setup-go must be pinned by 40-hex commit SHA: {uses}"
    )
    assert go_step.get("with") == {"go-version": "1.26.10"}

    release_doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    rel_go_step = _find_setup_go_step(release_doc["jobs"]["verify"]["steps"])
    rel_uses = str(rel_go_step.get("uses", ""))
    assert re.fullmatch(r"actions/setup-go@[0-9a-fA-F]{40}", rel_uses), (
        f"release setup-go must be pinned by SHA: {rel_uses}"
    )
    assert rel_go_step.get("with") == {"go-version": "1.26.10"}


def test_ci_go_version_matches_toolchain_in_go_mod() -> None:
    """AS-TS-R26-SOL61-01: CI workflow setup-go go-version must match tools/hclblocks/go.mod toolchain."""
    go_mod_text = (REPO_ROOT / "tools" / "hclblocks" / "go.mod").read_text(encoding="utf-8")
    m = re.search(r"^toolchain\s+go(\d+\.\d+\.\d+)", go_mod_text, re.MULTILINE)
    assert m is not None, "tools/hclblocks/go.mod must declare toolchain directive"
    expected_version = m.group(1)

    for wf_path in (WORKFLOW, RELEASE_WORKFLOW):
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        job_key = "tests" if wf_path == WORKFLOW else "verify"
        go_step = _find_setup_go_step(doc["jobs"][job_key]["steps"])
        assert go_step.get("with", {}).get("go-version") == expected_version, (
            f"{wf_path.name} actions/setup-go go-version must match go.mod toolchain {expected_version}"
        )


def test_go_vet_step_in_ci_has_isolated_env() -> None:
    """TS26-02 / AS-TS-R26-SOL61-01: go vet in CI must run with local toolchain and readonly modules."""
    expected_env = {
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOFLAGS": "-mod=readonly",
    }
    for wf_path in (WORKFLOW, RELEASE_WORKFLOW):
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        job_key = "tests" if wf_path == WORKFLOW else "verify"
        steps = doc["jobs"][job_key]["steps"]
        vet_step = next(s for s in steps if isinstance(s, dict) and "go vet" in s.get("run", ""))
        assert vet_step.get("working-directory") == "tools/hclblocks"
        assert vet_step.get("env") == expected_env


def test_setup_go_unpinned_mutant_rejected() -> None:
    from tools.validate_skills import validate_quality_workflow as vs_validate

    mutant_unpinned = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for s in mutant_unpinned["jobs"]["tests"]["steps"]:
        if "actions/setup-go" in str(s.get("uses", "")):
            s["uses"] = "actions/setup-go@v5"
    assert any("must be pinned by commit SHA" in e for e in vs_validate(mutant_unpinned))


def test_setup_go_wrong_version_mutant_rejected() -> None:
    from tools.validate_skills import validate_quality_workflow as vs_validate

    mutant_floating_version = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for s in mutant_floating_version["jobs"]["tests"]["steps"]:
        if "actions/setup-go" in str(s.get("uses", "")):
            s["with"] = {"go-version": "1.26"}
    assert any("must be exactly" in e for e in vs_validate(mutant_floating_version))

    mutant_file_version = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for s in mutant_file_version["jobs"]["tests"]["steps"]:
        if "actions/setup-go" in str(s.get("uses", "")):
            s["with"] = {"go-version-file": "tools/hclblocks/go.mod"}
    assert any("must be exactly" in e for e in vs_validate(mutant_file_version))


def test_go_vet_env_mutant_rejected() -> None:
    from tools.validate_skills import validate_quality_workflow as vs_validate

    mutant_vet_env = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for s in mutant_vet_env["jobs"]["tests"]["steps"]:
        if "go vet" in str(s.get("run", "")):
            s.pop("env", None)
    assert any("go vet env must be exactly" in e for e in vs_validate(mutant_vet_env))


def test_workflow_step_imports_installed_before_execution() -> None:
    from tools.validate_skills import validate_workflow_step_imports

    quality_doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    release_doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))

    q_errs = validate_workflow_step_imports(quality_doc, REPO_ROOT)
    assert not q_errs, f"quality.yaml has uninstalled step dependencies: {q_errs}"

    r_errs = validate_workflow_step_imports(release_doc, REPO_ROOT)
    assert not r_errs, f"release.yaml has uninstalled step dependencies: {r_errs}"


def test_workflow_step_imports_rejects_missing_pip_install() -> None:
    from tools.validate_skills import validate_workflow_step_imports

    quality_doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    mutant = copy.deepcopy(quality_doc)
    mutant["jobs"]["quality"]["steps"] = [
        s for s in mutant["jobs"]["quality"]["steps"] if "pip install" not in s.get("run", "")
    ]
    errs = validate_workflow_step_imports(mutant, REPO_ROOT)
    assert any("imports 'yaml'" in e for e in errs), f"Expected missing yaml error, got: {errs}"


def test_workflow_step_imports_rejects_uninstalled_tool_execution() -> None:
    from tools.validate_skills import validate_workflow_step_imports

    quality_doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    mutant_pytest = copy.deepcopy(quality_doc)
    mutant_pytest["jobs"]["quality"]["steps"].append(
        {"name": "early pytest", "run": "pytest tests/test_format_gate.py"}
    )
    errs = validate_workflow_step_imports(mutant_pytest, REPO_ROOT)
    assert any("executes 'pytest' before it is installed" in e for e in errs), f"Expected pytest error, got: {errs}"

    mutant_ruff = copy.deepcopy(quality_doc)
    mutant_ruff["jobs"]["tests"]["steps"].insert(0, {"name": "early ruff", "run": "python -m ruff check ."})
    errs_ruff = validate_workflow_step_imports(mutant_ruff, REPO_ROOT)
    assert any("executes 'ruff' before it is installed" in e for e in errs_ruff), (
        f"Expected ruff error, got: {errs_ruff}"
    )


def test_workflow_step_imports_rejects_transitive_local_package_imports(tmp_path: Path) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    test_script = tmp_path / "scripts" / "repro_transitive.py"
    test_script.parent.mkdir(parents=True, exist_ok=True)
    test_script.write_text("from tests.test_format_gate import validate_release_workflow\n")

    wf = {
        "jobs": {
            "quality": {
                "steps": [
                    {"run": "pip install pyyaml==6.0.1"},
                    {"run": f"python {test_script}"},
                ]
            }
        }
    }
    errs = validate_workflow_step_imports(wf, REPO_ROOT)
    assert any("imports 'pytest'" in e for e in errs), f"Expected transitive pytest rejection, got: {errs}"


def test_workflow_step_imports_handles_wrapped_chained_and_python_c() -> None:
    from tools.validate_skills import validate_workflow_step_imports

    wf = {
        "jobs": {
            "tests": {
                "steps": [
                    {"run": "env X=1 pytest"},
                    {"run": "cd . && pytest"},
                    {"run": 'python -c "import pytest"'},
                    {"run": "python \\\n scripts/lint_skills.py"},
                ]
            }
        }
    }
    errs = validate_workflow_step_imports(wf, REPO_ROOT)
    assert any("executes 'pytest' before it is installed" in e for e in errs)
    assert any("inline python code imports 'pytest'" in e for e in errs)
    assert any("imports 'yaml'" in e for e in errs)


def test_workflow_step_imports_rejects_unanalysable_command() -> None:
    from tools.validate_skills import validate_workflow_step_imports

    wf = {
        "jobs": {
            "quality": {
                "steps": [
                    {"run": "unknown_tool_xyz --flag"},
                ]
            }
        }
    }
    errs = validate_workflow_step_imports(wf, REPO_ROOT)
    assert any("unanalysable command: 'unknown_tool_xyz'" in e for e in errs)


OPUS_15_FAIL_CLOSED_CASES = [
    ("echo start | pytest -q", "operator '|'"),
    ("cd .&&pytest", "unanalysable command: 'cd'"),
    ("python -X dev -m pytest", "executes 'pytest' before it is installed"),
    ("python -W error -m pytest", "executes 'pytest' before it is installed"),
    ("python -W ignore scripts/temp_script.py", "imports 'pytest'"),
    ("echo $(pytest -q)", "command substitution"),
    ("echo `pytest -q`", "command substitution"),
    ("find . -name x -exec pytest {} +", "unanalysable command: 'find'"),
    ("source ./run.sh", "unanalysable command: 'source'"),
    ("git -c core.pager=pytest log", "unanalysable git configuration flag"),
    ("python -c '__import__(\"pytest\")'", "unanalysable statement or call in python -c"),
    (
        "python -c 'import importlib; importlib.import_module(\"pytest\")'",
        "unanalysable statement or call in python -c",
    ),
    ("python -m unittest discover -s skills/zzdisc -p check_*.py", "imports 'pytest'"),
    ("python scripts/does_not_exist_ts30.py", "cannot resolve execution target"),
    ("python scripts/bad_syntax.py", "unparseable script"),
]


def _setup_opus_15_fixtures(tmp_path: Path) -> None:
    temp_script = tmp_path / "scripts" / "temp_script.py"
    temp_script.parent.mkdir(parents=True, exist_ok=True)
    temp_script.write_text("import pytest\n", encoding="utf-8")
    bad_syntax = tmp_path / "scripts" / "bad_syntax.py"
    bad_syntax.write_text("def broken(\n", encoding="utf-8")
    custom_disc = tmp_path / "skills" / "zzdisc"
    custom_disc.mkdir(parents=True, exist_ok=True)
    (custom_disc / "check_zz.py").write_text("import pytest\n", encoding="utf-8")


@pytest.mark.parametrize("run_cmd,expected_needle", OPUS_15_FAIL_CLOSED_CASES)
def test_workflow_step_imports_opus_15_fail_closed_forms(tmp_path: Path, run_cmd: str, expected_needle: str) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    _setup_opus_15_fixtures(tmp_path)
    wf = {"jobs": {"tests": {"steps": [{"run": run_cmd}]}}}
    errs = validate_workflow_step_imports(wf, tmp_path)
    assert any(expected_needle in e for e in errs), f"Expected {expected_needle!r} in {errs}"


def test_workflow_step_imports_sol_findings(tmp_path: Path) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    # SOL-R30-01 / ARGON-TS-R31-002: package initializers along import path
    pkg = tmp_path / "pkg_init_test"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import pytest\n", encoding="utf-8")
    (pkg / "helper.py").write_text("x = 1\n", encoding="utf-8")

    # 1. import pkg_init_test.helper
    entry1 = tmp_path / "scripts" / "entry_sol1.py"
    entry1.parent.mkdir(parents=True, exist_ok=True)
    entry1.write_text("import pkg_init_test.helper\n", encoding="utf-8")
    wf1 = {"jobs": {"quality": {"steps": [{"run": f"python {entry1.relative_to(tmp_path)}"}]}}}
    assert any("imports 'pytest'" in e for e in validate_workflow_step_imports(wf1, tmp_path))

    # 2. Multi-level subpackage: from pkg_init_test.sub import helper
    sub = pkg / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "helper.py").write_text("y = 2\n", encoding="utf-8")
    entry2 = tmp_path / "scripts" / "entry_sol2.py"
    entry2.write_text("from pkg_init_test.sub import helper\n", encoding="utf-8")
    wf1_sub = {"jobs": {"quality": {"steps": [{"run": f"python {entry2.relative_to(tmp_path)}"}]}}}
    assert any("imports 'pytest'" in e for e in validate_workflow_step_imports(wf1_sub, tmp_path))

    # 3. Relative import: from .helpers import leaf where enclosing pkg init imports pytest
    pkg_rel = tmp_path / "pkg_rel_init"
    pkg_rel.mkdir()
    (pkg_rel / "__init__.py").write_text("import pytest\n", encoding="utf-8")
    (pkg_rel / "helpers.py").write_text("leaf = 1\n", encoding="utf-8")
    (pkg_rel / "user.py").write_text("from .helpers import leaf\n", encoding="utf-8")
    entry3 = tmp_path / "scripts" / "entry_sol3.py"
    entry3.write_text("from pkg_rel_init import user\n", encoding="utf-8")
    wf1_rel = {"jobs": {"quality": {"steps": [{"run": f"python {entry3.relative_to(tmp_path)}"}]}}}
    assert any("imports 'pytest'" in e for e in validate_workflow_step_imports(wf1_rel, tmp_path))

    # SOL-R30-02: unparsed shell operators fail closed
    wf2 = {
        "jobs": {
            "quality": {
                "steps": [
                    {"run": "echo ready;pytest"},
                    {"run": "echo ready&&pytest"},
                    {"run": 'echo ready | python -c "import pytest"'},
                    {"run": "cd scripts && python lint_skills.py"},
                ]
            }
        }
    }
    errs2 = validate_workflow_step_imports(wf2, tmp_path)
    assert any("unanalysable command: 'echo'" in e for e in errs2)
    assert any("operator '|'" in e for e in errs2)
    assert any("unanalysable command: 'cd'" in e for e in errs2)

    # SOL-R30-03: Python option operands and attached flags
    wf3 = {
        "jobs": {
            "quality": {
                "steps": [
                    {"run": 'python -W ignore -c "import pytest"'},
                    {"run": "python -mpytest"},
                ]
            }
        }
    }
    errs3 = validate_workflow_step_imports(wf3, tmp_path)
    assert any("inline python code imports 'pytest'" in e for e in errs3)
    assert any("executes 'pytest' before it is installed" in e for e in errs3)

    # SOL-R30-04: arbitrary python -m fails closed under R32 minimal closure
    wf4 = {"jobs": {"quality": {"steps": [{"run": "python -m pkg_with_main"}]}}}
    assert any(
        "unanalysable python -m target: 'pkg_with_main'" in e for e in validate_workflow_step_imports(wf4, tmp_path)
    )

    # SOL-R30-05: custom unittest pattern
    checks = tmp_path / "checks_sol"
    checks.mkdir()
    (checks / "check_probe.py").write_text("import pytest\n", encoding="utf-8")
    wf5 = {"jobs": {"quality": {"steps": [{"run": 'python -m unittest discover -s checks_sol -p "check_*.py"'}]}}}
    assert any("imports 'pytest'" in e for e in validate_workflow_step_imports(wf5, tmp_path))


def test_workflow_step_imports_ts30_02_resolves_against_repo_root_not_parent(tmp_path: Path) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    repo_dir = tmp_path / "fake_repo"
    repo_dir.mkdir()
    parent_pytest = tmp_path / "pytest"
    parent_pytest.mkdir()

    wf = {"jobs": {"tests": {"steps": [{"run": 'python -c "import pytest"'}]}}}
    errs = validate_workflow_step_imports(wf, repo_dir)
    assert any("inline python code imports 'pytest'" in e for e in errs)


def test_workflow_step_imports_relative_multi_hop_and_discover(tmp_path: Path) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    # 1. Two-hop package import: entry -> pkg_a.sub -> pkg_b.leaf -> pytest
    pkg_a = tmp_path / "pkg_a" / "sub"
    pkg_a.mkdir(parents=True)
    (tmp_path / "pkg_a" / "__init__.py").write_text("", encoding="utf-8")
    (pkg_a / "__init__.py").write_text("from pkg_b.leaf import val\n", encoding="utf-8")

    pkg_b = tmp_path / "pkg_b"
    pkg_b.mkdir(parents=True)
    (pkg_b / "__init__.py").write_text("", encoding="utf-8")
    (pkg_b / "leaf.py").write_text("import pytest\nval = 1\n", encoding="utf-8")

    entry1 = tmp_path / "scripts" / "entry_two_hop.py"
    entry1.parent.mkdir(parents=True, exist_ok=True)
    entry1.write_text("import pkg_a.sub\n", encoding="utf-8")

    wf1 = {"jobs": {"quality": {"steps": [{"run": f"python {entry1.relative_to(tmp_path)}"}]}}}
    assert any("imports 'pytest'" in e for e in validate_workflow_step_imports(wf1, tmp_path))

    # 2. Relative import: from .inner import x
    pkg_rel = tmp_path / "pkg_rel"
    pkg_rel.mkdir(parents=True)
    (pkg_rel / "__init__.py").write_text("", encoding="utf-8")
    (pkg_rel / "inner.py").write_text("import pytest\nx = 1\n", encoding="utf-8")
    (pkg_rel / "mod_a.py").write_text("from .inner import x\n", encoding="utf-8")

    entry2 = tmp_path / "scripts" / "entry_rel.py"
    entry2.write_text("from pkg_rel import mod_a\n", encoding="utf-8")

    wf2 = {"jobs": {"quality": {"steps": [{"run": f"python {entry2.relative_to(tmp_path)}"}]}}}
    assert any("imports 'pytest'" in e for e in validate_workflow_step_imports(wf2, tmp_path))

    # 3. Relative import: from . import inner
    (pkg_rel / "mod_b.py").write_text("from . import inner\n", encoding="utf-8")
    entry3 = tmp_path / "scripts" / "entry_rel_b.py"
    entry3.write_text("from pkg_rel import mod_b\n", encoding="utf-8")

    wf3 = {"jobs": {"quality": {"steps": [{"run": f"python {entry3.relative_to(tmp_path)}"}]}}}
    assert any("imports 'pytest'" in e for e in validate_workflow_step_imports(wf3, tmp_path))

    # 4. Local module/package via python -m fails closed in R32 (minimal closure)
    wf4 = {"jobs": {"quality": {"steps": [{"run": "python -m local_mod"}]}}}
    assert any("unanalysable python -m target: 'local_mod'" in e for e in validate_workflow_step_imports(wf4, tmp_path))

    wf5 = {"jobs": {"quality": {"steps": [{"run": "python -m my_local_pkg"}]}}}
    assert any(
        "unanalysable python -m target: 'my_local_pkg'" in e for e in validate_workflow_step_imports(wf5, tmp_path)
    )

    # 5. Unittest discover with -s and -p
    test_suite = tmp_path / "my_test_suite"
    test_suite.mkdir()
    (test_suite / "custom_test.py").write_text("import pytest\n", encoding="utf-8")
    wf6 = {"jobs": {"quality": {"steps": [{"run": 'python -m unittest discover -s my_test_suite -p "custom_*.py"'}]}}}
    assert any("imports 'pytest'" in e for e in validate_workflow_step_imports(wf6, tmp_path))


EXPECTED_CURRENT_WORKFLOW_FORMS = [
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
]


def test_current_workflow_forms_pinned_and_accepted() -> None:
    from tools.validate_skills import validate_workflow_step_imports

    wf_dir = REPO_ROOT / ".github" / "workflows"
    run_lines = []
    for wf_file in sorted(wf_dir.glob("*.y*ml")):
        content = yaml.safe_load(wf_file.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            continue
        jobs = content.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        for job_data in jobs.values():
            if not isinstance(job_data, dict):
                continue
            for step in job_data.get("steps", []):
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    for line in step["run"].splitlines():
                        trimmed = line.strip()
                        if trimmed and not trimmed.startswith("#"):
                            run_lines.append(trimmed)

    unique_forms = sorted(set(run_lines))
    assert unique_forms == sorted(EXPECTED_CURRENT_WORKFLOW_FORMS), (
        f"Workflow forms drift: added={set(unique_forms) - set(EXPECTED_CURRENT_WORKFLOW_FORMS)}, "
        f"removed={set(EXPECTED_CURRENT_WORKFLOW_FORMS) - set(unique_forms)}"
    )

    q_yaml = yaml.safe_load((wf_dir / "quality.yaml").read_text(encoding="utf-8"))
    r_yaml = yaml.safe_load((wf_dir / "release.yaml").read_text(encoding="utf-8"))
    assert validate_workflow_step_imports(q_yaml, REPO_ROOT) == []
    assert validate_workflow_step_imports(r_yaml, REPO_ROOT) == []


def test_real_r28_workflows_give_exactly_one_pytest_finding(tmp_path: Path) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    r28_q_text = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "show", "05f02f98:.github/workflows/quality.yaml"],
        text=True,
    )
    r28_q = yaml.safe_load(r28_q_text)

    r28_r_text = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "show", "05f02f98:.github/workflows/release.yaml"],
        text=True,
    )
    r28_r = yaml.safe_load(r28_r_text)

    # Replicate R28 file tree where scripts/test_pr_workflow_cancellation.py transitively imports pytest
    r28_dir = tmp_path / "r28_root"
    r28_dir.mkdir()
    shutil.copytree(REPO_ROOT / "tools", r28_dir / "tools")
    shutil.copytree(REPO_ROOT / "scripts", r28_dir / "scripts")
    shutil.copytree(REPO_ROOT / "skills", r28_dir / "skills")
    shutil.copytree(REPO_ROOT / "tests", r28_dir / "tests")
    # In R28, test_pr_workflow_cancellation.py imported test_format_gate which imported pytest
    (r28_dir / "scripts" / "test_pr_workflow_cancellation.py").write_text(
        "import pytest\n",
        encoding="utf-8",
    )

    errs_q = validate_workflow_step_imports(r28_q, r28_dir)
    assert len(errs_q) == 1, f"Expected exactly 1 error on R28 quality.yaml, got: {errs_q}"
    assert "test_pr_workflow_cancellation.py" in errs_q[0] and "pytest" in errs_q[0]

    errs_r = validate_workflow_step_imports(r28_r, r28_dir)
    assert errs_r == [], f"Expected 0 errors on R28 release.yaml, got: {errs_r}"


REVIEWER_FAIL_CLOSED_CASES = [
    # SOL-R31-02: arbitrary -m
    ("python -m timeit '1+1'", "unanalysable python -m target: 'timeit'"),
    # SOL-R31-03: dynamic calls in -c
    ("python -c 'exec(\"import pytest\")'", "unanalysable statement or call in python -c"),
    ("python -c '__import__(\"sys\")'", "unanalysable statement or call in python -c"),
    ("python -c 'import runpy; runpy.run_module(\"pytest\")'", "unanalysable statement or call in python -c"),
    ("python -c 'import subprocess; subprocess.run([\"pytest\"])'", "unanalysable statement or call in python -c"),
    ("python -c 'import os; os.system(\"pytest\")'", "unanalysable statement or call in python -c"),
    # SOL-R31-04: safe heads executing arbitrary commands
    ("git difftool --extcmd=pytest", "unanalysable git subcommand: 'difftool'"),
    ("go run ./x", "unanalysable go subcommand: 'run'"),
    ("gh alias set t --shell pytest", "unanalysable gh subcommand: 'alias set'"),
    # SOL-R31-05: pip flags allowlist
    ("pip install --dry-run pytest==8.3.4", "unanalysable pip install flag: '--dry-run'"),
    ("pip install --target vendor pytest", "unanalysable pip install flag: '--target'"),
    ("pip install --no-deps pytest", "unanalysable pip install flag: '--no-deps'"),
    ("pip install --user pytest", "unanalysable pip install flag: '--user'"),
    ("pip install --prefix /tmp pytest", "unanalysable pip install flag: '--prefix'"),
    ("pip install --root /tmp pytest", "unanalysable pip install flag: '--root'"),
    ("pip install -t vendor pytest", "unanalysable pip install flag: '-t'"),
    # TS31-02: mid-word #, safe heads, file-mutating commands, env prefixes
    ("echo a#b; pytest", "unanalysable command: 'echo'"),
    ("echo a#;pytest", "unanalysable command: 'echo'"),
    ("git rebase -x pytest HEAD~1", "unanalysable git subcommand: 'rebase'"),
    ("git bisect run pytest", "unanalysable git subcommand: 'bisect'"),
    ("git submodule foreach pytest", "unanalysable git subcommand: 'submodule'"),
    ("git config alias.t '!pytest'", "unanalysable git config key: 'alias.t'"),
    ("go generate ./...", "unanalysable go subcommand: 'generate'"),
    ("go test ./...", "unanalysable go subcommand: 'test'"),
    ("cp scripts/lint_skills.py scripts/clean.py", "unanalysable command: 'cp'"),
    ("mv scripts/lint_skills.py scripts/clean.py", "unanalysable command: 'mv'"),
    ("git checkout HEAD~1 -- scripts/lint_skills.py", "unanalysable git subcommand: 'checkout'"),
    ("PYTHONPATH=tools python scripts/lint_skills.py", "environment prefix"),
    ("env PYTHONPATH=tools python -c pass", "environment prefix or command 'env'"),
    ("export PYTHONPATH=tools", "environment prefix or command 'export'"),
]


@pytest.mark.parametrize("run_cmd,expected_needle", REVIEWER_FAIL_CLOSED_CASES)
def test_reviewer_counterexamples_fail_closed(run_cmd: str, expected_needle: str) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    wf = {"jobs": {"quality": {"steps": [{"run": run_cmd}]}}}
    errs = validate_workflow_step_imports(wf, REPO_ROOT)
    assert any(expected_needle in e for e in errs), f"Expected {expected_needle!r} in {errs}"


def test_namespace_package_traversal_and_error_reporting(tmp_path: Path) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "ns_helper.py").write_text("import pytest\n", encoding="utf-8")
    (scripts_dir / "uses_ns1.py").write_text("from scripts.ns_helper import pytest\n", encoding="utf-8")
    (scripts_dir / "uses_ns2.py").write_text("import scripts.ns_helper\n", encoding="utf-8")
    (scripts_dir / "uses_missing.py").write_text("from scripts.missing_helper import func\n", encoding="utf-8")

    wf1 = {
        "jobs": {
            "quality": {
                "steps": [
                    {"run": "pip install pyyaml==6.0.1"},
                    {"run": "python scripts/uses_ns1.py"},
                ]
            }
        }
    }
    errs1 = validate_workflow_step_imports(wf1, tmp_path)
    assert any("imports 'pytest'" in e for e in errs1)

    wf2 = {
        "jobs": {
            "quality": {
                "steps": [
                    {"run": "pip install pyyaml==6.0.1"},
                    {"run": "python scripts/uses_ns2.py"},
                ]
            }
        }
    }
    errs2 = validate_workflow_step_imports(wf2, tmp_path)
    assert any("imports 'pytest'" in e for e in errs2)

    wf3 = {
        "jobs": {
            "quality": {
                "steps": [
                    {"run": "pip install pyyaml==6.0.1"},
                    {"run": "python scripts/uses_missing.py"},
                ]
            }
        }
    }
    errs3 = validate_workflow_step_imports(wf3, tmp_path)
    assert any("cannot resolve local import chain" in e and "scripts.missing_helper" in e for e in errs3)


def test_pytest_collection_imports_fail_closed(tmp_path: Path) -> None:
    from tools.validate_skills import validate_workflow_step_imports

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_zz.py").write_text("import requests_never_installed_zz\n", encoding="utf-8")

    wf1 = {
        "jobs": {
            "tests": {
                "steps": [
                    {"run": "pip install pytest==8.3.4"},
                    {"run": "pytest tests/test_zz.py"},
                ]
            }
        }
    }
    errs1 = validate_workflow_step_imports(wf1, tmp_path)
    assert any("imports 'requests_never_installed_zz'" in e for e in errs1)

    wf2 = {
        "jobs": {
            "tests": {
                "steps": [
                    {"run": "pip install pytest==8.3.4"},
                    {"run": "python -m pytest tests"},
                ]
            }
        }
    }
    errs2 = validate_workflow_step_imports(wf2, tmp_path)
    assert any("imports 'requests_never_installed_zz'" in e for e in errs2)


def test_search_bases_precedence() -> None:
    from tools.validate_skills import _get_search_bases

    file_path = REPO_ROOT / "scripts" / "lint_skills.py"
    bases = _get_search_bases(file_path, REPO_ROOT)
    assert bases[0] == REPO_ROOT / "scripts"
    assert bases[1] == REPO_ROOT


def test_workflow_step_validator_branch_coverage(tmp_path: Path) -> None:
    from tools.validate_skills import (
        _check_line_shell_constructs,
        _consume_python_interpreter_flag,
        _extract_python_exec_mode,
        _parse_python_options_and_target,
        _validate_gh_command,
        _validate_git_command,
        _validate_go_command,
        _validate_job_step_imports,
    )

    # python option extract
    assert _extract_python_exec_mode("-m", None) == "python -m requires a target module operand"
    assert _extract_python_exec_mode("-mmod", None) == ("m", "mod", 1)
    assert _extract_python_exec_mode("-c", None) == "python -c requires a code operand"
    assert _extract_python_exec_mode("-cprint(1)", None) == ("c", "print(1)", 1)

    # python flag consume
    assert _consume_python_interpreter_flag(["-W"], 0) == (0, "python flag -W requires an operand")
    assert _consume_python_interpreter_flag(["-Wignore"], 0) == (1, None)
    assert _consume_python_interpreter_flag(["-Zunknown"], 0) == (0, "unrecognised python flag '-Zunknown'")
    assert _parse_python_options_and_target(["python"]) == "python invoked without script, module, or code"

    # go
    assert _validate_go_command(["go"]) == "unanalysable go subcommand: '<missing>'"
    assert _validate_go_command(["go", "vet", "x"]) == "unanalysable go vet arguments: ['x']"

    # gh
    assert _validate_gh_command(["gh"]) == "unanalysable gh subcommand: '<missing>'"
    assert _validate_gh_command(["gh", "release"]) == "unanalysable gh subcommand: 'release'"
    assert _validate_gh_command(["gh", "release", "create", "-F"]) == "gh release create -F missing argument"
    assert _validate_gh_command(["gh", "release", "create", "--foo"]) == "unanalysable gh release create flag: '--foo'"

    # git
    assert _validate_git_command(["git"]) == "git invoked without subcommand"
    assert _validate_git_command(["git", "merge-base"]) == "unanalysable git merge-base arguments: []"
    assert _validate_git_command(["git", "merge-base", "--is-ancestor", "A"]) is not None
    assert _validate_git_command(["git", "config"]) == "unanalysable git config key: '<missing>'"
    assert _validate_git_command(["git", "config", "core.bare"]) == "unanalysable git config key: 'core.bare'"
    assert _validate_git_command(["git", "tag", "-a"]) == "git tag -a missing tag operand"
    assert _validate_git_command(["git", "tag", "-F"]) == "git tag -F missing file operand"
    assert _validate_git_command(["git", "tag", "-s", "v1.0"]) == "unanalysable git tag option: '-s'"
    assert _validate_git_command(["git", "tag", "v1.0"]) == "git tag missing -a flag in ['v1.0']"
    assert _validate_git_command(["git", "push"]) == "unanalysable git push arguments: []"
    assert _validate_git_command(["git", "push", "origin", "--force"]) == "unanalysable git push option: '--force'"

    # shell constructs
    assert _check_line_shell_constructs("cat <<EOF\n")[0] is None
    assert _check_line_shell_constructs("echo 'unclosed")[0] is None

    # job step edge cases
    errs: list[str] = []
    _validate_job_step_imports("j", {"steps": "not-list"}, tmp_path, errs)
    _validate_job_step_imports("j", {"steps": [None, 123, {"name": "no-run"}]}, tmp_path, errs)
