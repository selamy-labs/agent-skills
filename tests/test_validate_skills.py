"""Tests for tools/validate_skills.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.validate_skills import (
    check_banned_prefixes_and_operators,
    check_statement_tokens,
    main,
    strip_github_expressions,
    validate_dependency_install_step,
    validate_job_gates,
    validate_pytest_command,
    validate_quality_job,
    validate_quality_workflow,
    validate_ruff_check_command,
    validate_ruff_format_command,
    validate_shell_configuration,
    validate_skills_directory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "quality.yaml"


def _make_valid_job() -> dict[str, Any]:
    return {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"uses": "actions/checkout@v7.0.0"},
            {"uses": "actions/setup-python@v6.3.0", "with": {"python-version": "3.12"}},
            {"run": "pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 pyyaml==6.0.1 python-hcl2==7.2.0"},
            {"run": "python -m ruff check ."},
            {"run": "python -m ruff format --check --diff ."},
            {"run": "python tools/validate_skills.py"},
            {"run": "python tools/validate_marketplace.py"},
            {"run": "python tools/tf_module_layout.py --self-test"},
            {"run": "python -m unittest discover -s skills/portfolio-delivery-control/tests -v"},
            {
                "run": (
                    "pytest --cov=tools --cov-branch --cov-report=term-missing "
                    "--cov-report=json:coverage.json --cov-fail-under=90"
                )
            },
            {"run": "python -m tests.test_coverage_threshold coverage.json"},
        ],
    }


def test_strip_github_expressions() -> None:
    text = "echo ${{ github.actor }} and ${{ github.sha }}"
    assert strip_github_expressions(text) == "echo __GH_EXPR__ and __GH_EXPR__"


def test_validate_shell_configuration() -> None:
    assert validate_shell_configuration("test", {}) == []
    assert validate_shell_configuration("test", {"shell": "bash -eo pipefail"}) == []
    assert validate_shell_configuration("test", {"defaults": {"run": {"shell": "bash -eo pipefail"}}}) == []
    assert validate_shell_configuration("test", {"defaults": {"run": "not-dict"}}) == []
    errs = validate_shell_configuration("test", {"shell": "bash {0}"})
    assert any("disallowed shell override" in e for e in errs)
    errs_def = validate_shell_configuration("test", {"defaults": {"run": {"shell": "sh"}}})
    assert any("disallowed defaults.run.shell override" in e for e in errs_def)


def test_check_statement_tokens_banned_prefix() -> None:
    errs = check_statement_tokens(["true", "pytest"], "true pytest")
    assert any("banned prefix" in e for e in errs)


def test_check_statement_tokens_hiding_sensitive_target() -> None:
    errs = check_statement_tokens(["sudo", "pytest"], "sudo pytest")
    assert any("hiding sensitive target" in e for e in errs)


def test_check_statement_tokens_allowed_runners() -> None:
    assert check_statement_tokens(["python", "-m", "pytest"], "python -m pytest") == []
    assert check_statement_tokens(["pytest", "--cov=tools"], "pytest --cov=tools") == []


def test_check_banned_prefixes_and_operators() -> None:
    assert check_banned_prefixes_and_operators("pytest || true") != []
    assert check_banned_prefixes_and_operators("pytest && echo ok") != []
    assert check_banned_prefixes_and_operators(": ruff check .") != []
    assert check_banned_prefixes_and_operators("! pytest") != []
    assert check_banned_prefixes_and_operators("set -e") != []
    assert check_banned_prefixes_and_operators("python -m ruff check .") == []


def test_validate_pytest_command_valid() -> None:
    valid_cmd = [
        "pytest",
        "--cov=tools",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=json:coverage.json",
        "--cov-fail-under=90",
    ]
    assert validate_pytest_command(valid_cmd) == []
    assert validate_pytest_command(["python", "-m"] + valid_cmd) == []
    assert validate_pytest_command(["python3", "-m"] + valid_cmd) == []
    assert validate_pytest_command(valid_cmd + ["tests"]) == []
    assert validate_pytest_command(valid_cmd + ["tests/"]) == []
    assert (
        validate_pytest_command(
            [
                "pytest",
                "--cov=tools",
                "--cov-branch",
                "--cov-report=term-missing",
                "--cov-report=json:coverage.json",
                "--cov-fail-under",
                "95",
            ]
        )
        == []
    )


def test_validate_pytest_command_invalid_flags() -> None:
    base = [
        "pytest",
        "--cov=tools",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=json:coverage.json",
        "--cov-fail-under=90",
    ]
    assert any("Disallowed token/flag" in e for e in validate_pytest_command(base + ["--no-cov"]))
    assert any("Disallowed token/flag" in e for e in validate_pytest_command(base + ["--co"]))
    assert any("Disallowed token/flag" in e for e in validate_pytest_command(base + ["--collect-only"]))
    assert any("Disallowed token/flag" in e for e in validate_pytest_command(base + ["-o", "addopts="]))
    assert any("Disallowed token/flag" in e for e in validate_pytest_command(base + ["-k", "test_foo"]))
    assert any("Disallowed token/flag" in e for e in validate_pytest_command(base + ["-m", "unit"]))


def test_validate_pytest_command_missing_requirements() -> None:
    cov_cmd = [
        "pytest",
        "--cov=tools",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=json:coverage.json",
    ]
    assert any(
        "missing '--cov=tools'" in e
        for e in validate_pytest_command(
            [
                "pytest",
                "--cov-branch",
                "--cov-report=term-missing",
                "--cov-report=json:coverage.json",
                "--cov-fail-under=90",
            ]
        )
    )
    assert any(
        "missing '--cov-branch'" in e
        for e in validate_pytest_command(
            [
                "pytest",
                "--cov=tools",
                "--cov-report=term-missing",
                "--cov-report=json:coverage.json",
                "--cov-fail-under=90",
            ]
        )
    )
    assert any(
        "missing '--cov-report=json:coverage.json'" in e
        for e in validate_pytest_command(
            ["pytest", "--cov=tools", "--cov-branch", "--cov-report=term-missing", "--cov-fail-under=90"]
        )
    )
    assert any("missing '--cov-fail-under'" in e for e in validate_pytest_command(cov_cmd))
    assert any("Missing value" in e for e in validate_pytest_command(cov_cmd + ["--cov-fail-under"]))
    assert any(
        "coverage floor" in e and "< 90" in e for e in validate_pytest_command(cov_cmd + ["--cov-fail-under=85"])
    )
    assert any(
        "Invalid --cov-fail-under value" in e for e in validate_pytest_command(cov_cmd + ["--cov-fail-under=bad"])
    )
    assert any("Expected pytest command" in e for e in validate_pytest_command(["ls", "-la"]))


def test_validate_ruff_check_command() -> None:
    assert validate_ruff_check_command(["python", "-m", "ruff", "check", "."]) == []
    assert validate_ruff_check_command(["python3", "-m", "ruff", "check", "."]) == []
    assert validate_ruff_check_command(["ruff", "check", "."]) == []
    assert validate_ruff_check_command(["python", "-m", "ruff", "check", "--fix", "."]) != []
    assert validate_ruff_check_command(["uvx", "ruff@0.15.21", "check", "."]) != []


def test_validate_ruff_format_command() -> None:
    assert validate_ruff_format_command(["python", "-m", "ruff", "format", "--check", "--diff", "."]) == []
    assert validate_ruff_format_command(["python", "-m", "ruff", "format", "--diff", "--check", "."]) == []
    assert validate_ruff_format_command(["python3", "-m", "ruff", "format", "--check", "--diff", "."]) == []
    assert validate_ruff_format_command(["ruff", "format", "--check", "--diff", "."]) == []
    assert validate_ruff_format_command(["python", "-m", "ruff", "format", "."]) != []
    assert validate_ruff_format_command(["python", "-m", "ruff", "format", "--check", "."]) != []


def test_validate_dependency_install_step() -> None:
    valid_step = {"run": "pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 pyyaml==6.0.1 python-hcl2==7.2.0"}
    assert validate_dependency_install_step(valid_step) == []
    unpinned = {"run": "pip install ruff==0.15.21 python-hcl2==7.2.0 pytest"}
    assert any("Disallowed pip pin for 'pytest'" in e for e in validate_dependency_install_step(unpinned))
    hcl2_only = {"run": "pip install pytest==8.3.4 pytest-cov==6.0.0 python-hcl2==7.2.0"}
    assert any("missing 'ruff==0.15.21'" in e for e in validate_dependency_install_step(hcl2_only))
    ruff_only = {"run": "pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21"}
    assert any("missing 'python-hcl2==7.2.0'" in e for e in validate_dependency_install_step(ruff_only))
    assert any("missing 'pip install'" in e for e in validate_dependency_install_step({"run": "npm install"}))


def test_validate_dependency_install_step_rejects_suffix_pins() -> None:
    ruff_suffix = {"run": "pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.210 python-hcl2==7.2.0"}
    errs = validate_dependency_install_step(ruff_suffix)
    assert any("missing 'ruff==0.15.21' pin" in e or "Disallowed pip pin" in e for e in errs)

    hcl2_suffix = {"run": "pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 python-hcl2==7.2.0rc1"}
    errs2 = validate_dependency_install_step(hcl2_suffix)
    assert any("missing 'python-hcl2==7.2.0' pin" in e or "Disallowed pip pin" in e for e in errs2)


def test_validate_dependency_install_step_requires_actual_pip_install() -> None:
    not_real_pip = {"run": "echo pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 python-hcl2==7.2.0"}
    errs = validate_dependency_install_step(not_real_pip)
    assert any("missing 'pip install' command" in e for e in errs)

    python_m_pip = {"run": "python -m pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 python-hcl2==7.2.0"}
    assert validate_dependency_install_step(python_m_pip) == []

    python3_m_pip = {"run": "python3 -m pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 python-hcl2==7.2.0"}
    assert validate_dependency_install_step(python3_m_pip) == []


def test_validate_job_gates_valid() -> None:
    job = _make_valid_job()
    assert validate_job_gates(job, "tests") == []


def test_validate_job_gates_rejects_python_c_argument_bypass() -> None:
    job = _make_valid_job()
    job["steps"] = [
        s
        for s in job["steps"]
        if not any(
            g in s.get("run", "") for g in ("validate_skills.py", "validate_marketplace.py", "tf_module_layout.py")
        )
    ]
    bypass_cmd = (
        'python -c "" tools/validate_skills.py tools/validate_marketplace.py tools/tf_module_layout.py --self-test'
    )
    job["steps"].append({"run": bypass_cmd})
    errs = validate_job_gates(job, "tests")
    assert any("missing required gate: validate_skills" in e for e in errs)
    assert any("missing required gate: validate_marketplace" in e for e in errs)
    assert any("missing required gate: tf_module_layout" in e for e in errs)


def test_validate_job_gates_rejects_inline_comment_bypass() -> None:
    job = _make_valid_job()
    job["steps"] = [
        s
        for s in job["steps"]
        if not any(
            g in s.get("run", "") for g in ("validate_skills.py", "validate_marketplace.py", "tf_module_layout.py")
        )
    ]
    inline_comment_cmd = (
        "python -m ruff check . # tools/validate_skills.py "
        "tools/validate_marketplace.py tools/tf_module_layout.py --self-test"
    )
    job["steps"].append({"run": inline_comment_cmd})
    errs = validate_job_gates(job, "tests")
    assert any("missing required gate: validate_skills" in e for e in errs)
    assert any("missing required gate: validate_marketplace" in e for e in errs)
    assert any("missing required gate: tf_module_layout" in e for e in errs)


def test_validate_job_gates_missing_gates() -> None:
    job: dict[str, Any] = {"steps": []}
    errs = validate_job_gates(job, "tests")
    assert any("missing required gate: pip_ruff_pin" in e for e in errs)
    assert any("missing required gate: pip_hcl2_pin" in e for e in errs)
    assert any("missing required gate: ruff_check" in e for e in errs)
    assert any("missing required gate: ruff_format" in e for e in errs)
    assert any("missing required gate: pytest" in e for e in errs)
    assert any("missing required gate: validate_skills" in e for e in errs)
    assert any("missing required gate: validate_marketplace" in e for e in errs)
    assert any("missing required gate: tf_module_layout" in e for e in errs)


def test_validate_job_gates_rejects_banned_prefixes() -> None:
    job = _make_valid_job()
    job["steps"].append({"run": "true pytest --cov=tools --cov-branch --cov-report=term-missing --cov-fail-under=90"})
    errs = validate_job_gates(job, "tests")
    assert any("banned prefix 'true'" in e for e in errs)

    job2 = _make_valid_job()
    job2["steps"].append({"run": ": ruff check ."})
    errs2 = validate_job_gates(job2, "tests")
    assert any("banned prefix ':'" in e for e in errs2)


def test_validate_quality_workflow_actual_file() -> None:
    assert validate_quality_workflow(QUALITY_WORKFLOW_PATH) == []


def test_validate_quality_workflow_rejects_if_condition() -> None:
    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    doc["jobs"]["tests"]["if"] = "always()"
    errs = validate_quality_workflow(doc)
    assert any("must not declare an if: condition" in e for e in errs)


def test_validate_quality_workflow_missing_job() -> None:
    doc = {"jobs": {"other": {"steps": []}}}
    errs = validate_quality_workflow(doc)
    assert any("missing target tests/quality job" in e for e in errs)


def test_validate_quality_workflow_malformed() -> None:
    assert any("must be a mapping" in e for e in validate_quality_workflow("- item1\n- item2"))
    assert any("Failed to parse YAML" in e for e in validate_quality_workflow("not: [a, mapping"))


def test_validate_skills_directory_actual() -> None:
    errors, count = validate_skills_directory(REPO_ROOT)
    assert count > 0
    assert errors == []


def test_check_banned_prefixes_unclosed_quote() -> None:
    assert any("Failed to parse shell tokens" in e for e in check_banned_prefixes_and_operators('pytest "unclosed'))


def test_validate_ruff_format_unrecognized_prefix() -> None:
    assert any(
        "Disallowed ruff format invocation prefix" in e for e in validate_ruff_format_command(["unknown", "format"])
    )


def test_validate_job_gates_invalid_commands() -> None:
    job = {
        "steps": [
            {"run": "python -m ruff check --extra ."},
            {"run": "python -m ruff format --extra ."},
            {"run": "pytest --cov=tools --cov-branch --cov-fail-under=10"},
        ]
    }
    errs = validate_job_gates(job, "tests")
    assert any("Disallowed ruff check invocation" in e for e in errs)
    assert any("Disallowed ruff format arguments" in e for e in errs)
    assert any("coverage floor 10.0 < 90" in e for e in errs)


def test_validate_job_gates_steps_not_list() -> None:
    assert any("steps must be a list" in e for e in validate_job_gates({"steps": "not-list"}, "tests"))


def test_validate_quality_workflow_jobs_not_mapping() -> None:
    assert any("jobs must be a mapping" in e for e in validate_quality_workflow({"jobs": "not-dict"}))


def test_main_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    assert code == 0
    out = capsys.readouterr().out
    assert "Validated" in out


def test_main_cli_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("tools.validate_skills.validate_quality_workflow", lambda _path: ["forced quality error"])
    code = main([])
    assert code == 1
    err = capsys.readouterr().err
    assert "forced quality error" in err


def test_validate_env_configuration_rules() -> None:
    from tools.validate_skills import validate_env_configuration

    assert validate_env_configuration("test", {}) == []
    assert validate_env_configuration("test", {"env": None}) == []
    assert validate_env_configuration("test", {"env": {}}) == []
    assert any("disallowed env type" in e for e in validate_env_configuration("test", {"env": "not-a-dict"}))
    assert (
        validate_env_configuration("test", {"env": {"GH_TOKEN": "token"}}, allowed_keys=frozenset({"GH_TOKEN"})) == []
    )
    assert any("disallowed env variable" in e for e in validate_env_configuration("test", {"env": {"FOO": "bar"}}))
    assert any(
        "banned env variable" in e
        for e in validate_env_configuration(
            "test", {"env": {"PYTEST_ADDOPTS": "--no-cov"}}, allowed_keys=frozenset({"PYTEST_ADDOPTS"})
        )
    )


def test_validate_job_gates_rejects_step_if_condition() -> None:
    for script_name in (
        "python tools/validate_skills.py",
        "python tools/validate_marketplace.py",
        "python tools/tf_module_layout.py --self-test",
        "python -m unittest discover -s skills/portfolio-delivery-control/tests -v",
    ):
        job = _make_valid_job()
        for step in job["steps"]:
            if step.get("run") == script_name:
                step["if"] = "${{ 1 == 2 }}"
        errs = validate_job_gates(job, "tests")
        assert any("must not have an if: condition" in e for e in errs)


def test_validate_job_gates_rejects_step_env_mapping() -> None:
    job = _make_valid_job()
    for step in job["steps"]:
        if "pytest" in step.get("run", ""):
            step["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
    errs = validate_job_gates(job, "tests")
    assert any("disallowed env variable" in e or "banned env variable" in e for e in errs)


def test_validate_job_gates_rejects_job_env_mapping() -> None:
    job = _make_valid_job()
    job["env"] = {"PYTEST_ADDOPTS": "--no-cov"}
    errs = validate_job_gates(job, "tests")
    assert any("disallowed env variable" in e or "banned env variable" in e for e in errs)


def test_validate_job_gates_rejects_secondary_pip_install() -> None:
    job = _make_valid_job()
    job["steps"].insert(3, {"name": "Reinstall ruff", "run": "pip install ruff==0.9.10"})
    errs = validate_job_gates(job, "tests")
    assert any("pip install steps" in e or "secondary pip install" in e for e in errs)


def test_validate_quality_workflow_rejects_top_level_env() -> None:
    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    doc["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
    errs = validate_quality_workflow(doc)
    assert any("disallowed env variable" in e or "banned env variable" in e for e in errs)


def test_validate_quality_job_steps_not_list() -> None:
    from tools.validate_skills import validate_quality_job

    assert any("steps must be a list" in e for e in validate_quality_job({"steps": "not-list"}))


def test_validate_quality_job_continue_on_error() -> None:
    from tools.validate_skills import validate_quality_job

    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = doc["jobs"]["quality"]
    job["continue-on-error"] = True
    errs = validate_quality_job(job)
    assert any("must not have continue-on-error" in e for e in errs)


def test_validate_quality_job_missing_script() -> None:
    from tools.validate_skills import validate_quality_job

    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = doc["jobs"]["quality"]
    job["steps"] = [s for s in job["steps"] if "privacy_scan.py" not in s.get("run", "")]
    errs = validate_quality_job(job)
    assert any("missing required step: privacy_scan.py" in e for e in errs)


def test_validate_quality_job_pr_title_conditions() -> None:
    from tools.validate_skills import validate_quality_job

    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = doc["jobs"]["quality"]
    for s in job["steps"]:
        if "validate_pr_title.py" in s.get("run", ""):
            s["if"] = "false"
    errs = validate_quality_job(job)
    assert any("must not have if: false" in e for e in errs)

    doc2 = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    job2 = doc2["jobs"]["quality"]
    for s in job2["steps"]:
        if "validate_pr_title.py" in s.get("run", ""):
            s["if"] = "always()"
    errs2 = validate_quality_job(job2)
    assert any("must reference pull_request" in e for e in errs2)


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
def test_validate_quality_workflow_rejects_quality_step_if_condition(target_script: str) -> None:
    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    modified = False
    for step in doc["jobs"]["quality"]["steps"]:
        if target_script in step.get("run", ""):
            step["if"] = "${{ 1 == 2 }}"
            modified = True
    assert modified
    errs = validate_quality_workflow(doc)
    assert any("must not have an if: condition" in e for e in errs)


def test_validate_quality_workflow_rejects_missing_quality_job() -> None:
    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    del doc["jobs"]["quality"]
    errs = validate_quality_workflow(doc)
    assert any("missing target tests/quality job" in e or "missing required 'quality' job" in e for e in errs)


def test_validate_quality_workflow_rejects_pr_title_spoofing_on_mandatory_script() -> None:
    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    for step in doc["jobs"]["quality"]["steps"]:
        if "privacy_scan.py" in step.get("run", ""):
            step["name"] = "Validate PR title and privacy"
            step["if"] = "github.event_name == 'pull_request' && 1 == 2"
    errs = validate_quality_workflow(doc)
    assert any("must not have an if: condition" in e for e in errs) or any(
        "missing required step: privacy_scan.py" in e for e in errs
    )


def test_validate_quality_workflow_rejects_python_c_argument_bypass() -> None:
    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    for step in doc["jobs"]["quality"]["steps"]:
        if "privacy_scan.py" in step.get("run", ""):
            step["run"] = 'python3 -c "pass" scripts/privacy_scan.py'
    errs = validate_quality_workflow(doc)
    assert any("missing required step: privacy_scan.py" in e for e in errs)


class TestValidateSkillsEdgeBranches:
    def test_validate_shell_configuration_edge_cases(self) -> None:
        assert len(validate_shell_configuration("test", {"defaults": "not-a-dict"})) > 0
        assert len(validate_shell_configuration("test", {"defaults": {"working-directory": "foo"}})) > 0
        assert validate_shell_configuration("test", "not-a-dict") == []

    def test_pytest_and_cov_parsing_edge_cases(self) -> None:
        from tools.validate_skills import _process_cov_fail_under_arg

        assert check_banned_prefixes_and_operators("# only comment") == []
        assert _process_cov_fail_under_arg("--other", None) == (0, None, None)

        py_err1 = validate_pytest_command(["pytest", "--cov-fail-under"])
        assert any("Missing value" in e for e in py_err1)

        py_err2 = validate_pytest_command(["pytest", "--cov-fail-under=invalid"])
        assert any("Invalid" in e for e in py_err2)

    def test_pip_install_tokens_edge_cases(self) -> None:
        from tools.validate_skills import (
            _find_pip_install_line,
            _handle_pip_tokens,
            _step_contains_pip_install,
            check_pip_install_tokens,
        )

        assert len(check_pip_install_tokens(["pip", "install", "complexipy==0.1.0"], True)) > 0
        assert len(check_pip_install_tokens(["pip", "install", "vulture==2.11"], True)) > 0
        assert check_pip_install_tokens(["pip", "list"], True) == []

        assert _find_pip_install_line(["pip install 'unclosed"]) is None

        errs_hp: list[str] = []
        _handle_pip_tokens(["pip", "install", "ruff==0.15.21"], True, {}, errs_hp)
        assert len(errs_hp) > 0

        assert _step_contains_pip_install("not-dict") is False
        assert _step_contains_pip_install({"run": "# comment\n'unclosed\npip install foo"}) is True

    def test_ruff_and_process_lines_edge_cases(self) -> None:
        from tools.validate_skills import (
            _check_ruff_token,
            _parse_line_tokens,
            _process_step_run_lines,
        )

        errs_rf: list[str] = []
        _check_ruff_token(["python", "-m", "ruff", "format", "--bad"], {}, errs_rf)
        assert len(errs_rf) > 0

        errs_rc: list[str] = []
        _check_ruff_token(["python", "-m", "ruff", "check", "--bad"], {}, errs_rc)
        assert len(errs_rc) > 0

        assert _parse_line_tokens("# comment") is None
        assert _parse_line_tokens("'unclosed") is None

        errs_proc: list[str] = []
        _process_step_run_lines("# comment\n; bad\n'unclosed\npython -m ruff check .", False, {}, errs_proc)
        assert len(errs_proc) > 0

    def test_cli_and_marketplace_edge_cases(self) -> None:
        from unittest.mock import patch

        from tools.validate_marketplace import _check_scanner_step_guards, _scan_step_for_scanners
        from tools.validate_skills import main as vs_main
        from tools.validate_skills import validate_skills_directory

        assert validate_skills_directory(None)[1] > 0

        with patch("pathlib.Path.is_file", return_value=False):
            assert vs_main([]) == 1

        vm_errs: list[str] = []
        _scan_step_for_scanners("python 'unclosed", set(), vm_errs)
        assert len(vm_errs) > 0

        guard_errs: list[str] = []
        _check_scanner_step_guards({"name": "s", "if": "true"}, "v", guard_errs)
        assert len(guard_errs) > 0


class TestJobLevelKeyValidation:
    """Negative tests verifying that job-level key mutations are rejected."""

    @pytest.mark.parametrize(
        "key,value,expected_substring",
        [
            (
                "container",
                {"image": "python:3.12-slim", "env": {"PYTEST_ADDOPTS": "--collect-only --no-cov"}},
                "disallowed job-level keys",
            ),
            ("container", "python:3.12-slim", "disallowed job-level keys"),
            ("services", {"redis": {"image": "redis"}}, "disallowed job-level keys"),
            ("runs-on", "self-hosted", "runs-on must be 'ubuntu-latest'"),
            ("timeout-minutes", 1, "timeout-minutes must be an integer between 5 and 60"),
            ("timeout-minutes", 65, "timeout-minutes must be an integer between 5 and 60"),
            ("timeout-minutes", True, "timeout-minutes must be an integer between 5 and 60"),
            ("permissions", "write-all", "permissions must be {'contents': 'read'}"),
            ("permissions", "read-all", "permissions must be {'contents': 'read'}"),
            ("permissions", {"contents": "write"}, "permissions must be {'contents': 'read'}"),
            ("permissions", {"contents": "read", "issues": "write"}, "permissions must be {'contents': 'read'}"),
        ],
    )
    def test_validate_job_gates_rejects_job_level_mutants(self, key: str, value: Any, expected_substring: str) -> None:
        job = _make_valid_job()
        job[key] = value
        errs = validate_job_gates(job, "tests")
        assert any(expected_substring in e for e in errs), f"Expected {expected_substring!r} in {errs}"

    @pytest.mark.parametrize(
        "key,value,expected_substring",
        [
            (
                "container",
                {"image": "python:3.12-slim", "env": {"PYTEST_ADDOPTS": "--collect-only --no-cov"}},
                "disallowed job-level keys",
            ),
            ("container", "python:3.12-slim", "disallowed job-level keys"),
            ("services", {"redis": {"image": "redis"}}, "disallowed job-level keys"),
            ("runs-on", "self-hosted", "runs-on must be 'ubuntu-latest'"),
            ("timeout-minutes", 1, "timeout-minutes must be an integer between 5 and 60"),
            ("permissions", "write-all", "permissions must be {'contents': 'read'}"),
            ("permissions", {"contents": "write"}, "permissions must be {'contents': 'read'}"),
        ],
    )
    def test_validate_quality_job_rejects_job_level_mutants(
        self, key: str, value: Any, expected_substring: str
    ) -> None:
        doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
        job = dict(doc["jobs"]["quality"])
        job[key] = value
        errs = validate_quality_job(job, "quality")
        assert any(expected_substring in e for e in errs), f"Expected {expected_substring!r} in {errs}"

    def test_validate_job_gates_accepts_valid_timeout(self) -> None:
        job = _make_valid_job()
        job["timeout-minutes"] = 15
        assert validate_job_gates(job, "tests") == []

    def test_validate_job_gates_accepts_valid_permissions(self) -> None:
        job = _make_valid_job()
        job["permissions"] = {"contents": "read"}
        assert validate_job_gates(job, "tests") == []

    def test_validate_quality_workflow_rejects_extra_job(self) -> None:
        doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
        doc["jobs"]["extra"] = {"runs-on": "ubuntu-latest", "steps": []}
        errs = validate_quality_workflow(doc)
        assert any("quality workflow jobs must contain only 'quality' and 'tests'" in e for e in errs)


def test_validate_job_gates_rejects_reordered_tests_steps() -> None:
    job = _make_valid_job()
    steps = job["steps"]
    pytest_idx = next(i for i, s in enumerate(steps) if "pytest" in s.get("run", ""))
    verify_idx = next(i for i, s in enumerate(steps) if "test_coverage_threshold" in s.get("run", ""))
    assert pytest_idx < verify_idx
    verify_step = steps.pop(verify_idx)
    steps.insert(pytest_idx, verify_step)
    errs = validate_job_gates(job, "tests")
    assert any("out of order" in e for e in errs)


def test_validate_quality_workflow_rejects_reordered_tests_steps() -> None:
    doc = yaml.safe_load(QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = doc["jobs"]["tests"]["steps"]
    pytest_idx = next(i for i, s in enumerate(steps) if "pytest" in s.get("run", ""))
    verify_idx = next(i for i, s in enumerate(steps) if "test_coverage_threshold" in s.get("run", ""))
    assert pytest_idx < verify_idx
    verify_step = steps.pop(verify_idx)
    steps.insert(pytest_idx, verify_step)
    errs = validate_quality_workflow(doc)
    assert any("out of order" in e for e in errs)


def test_check_no_tracked_coverage_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.validate_skills import check_no_tracked_coverage_json

    monkeypatch.setattr("tools.validate_skills.is_tracked_by_git", lambda _p: False)
    assert check_no_tracked_coverage_json(tmp_path) == []

    monkeypatch.setattr("tools.validate_skills.is_tracked_by_git", lambda _p: True)
    errs = check_no_tracked_coverage_json(tmp_path)
    assert any("must not be tracked in git" in e for e in errs)


def test_validate_quality_workflow_rejects_tracked_coverage_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tools.validate_skills.is_tracked_by_git", lambda _p: True)
    errs = validate_quality_workflow(QUALITY_WORKFLOW_PATH)
    assert any("must not be tracked in git" in e for e in errs)
