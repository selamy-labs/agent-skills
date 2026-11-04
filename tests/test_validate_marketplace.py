"""Tests for tools/validate_marketplace.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.validate_marketplace import (
    REQUIRED_RELEASE_SCANNERS,
    _check_plugin_entry,
    _validate_marketplace_json,
    _validate_plugin_json,
    main,
    validate_marketplace_files,
    validate_release_scanners,
    validate_release_workflow,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yaml"


def _make_valid_verify_job() -> dict[str, Any]:
    scanner_runs = "# Scanners\n\n" + "\n".join(f"python3 -B scripts/{s}" for s in REQUIRED_RELEASE_SCANNERS)
    return {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"uses": "actions/checkout@v7.0.0"},
            {
                "uses": "actions/setup-go@d35c59abb061a4a6fb18e82ac0862c26744d6ab5",
                "with": {"go-version": "1.26.10"},
            },
            {"run": "pip install pytest==8.3.4 pytest-cov==6.0.0 ruff==0.15.21 pyyaml==6.0.1"},
            {
                "name": "Verify Go helper",
                "working-directory": "tools/hclblocks",
                "env": {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOFLAGS": "-mod=readonly"},
                "run": "go vet ./...",
            },
            {"run": "python -m ruff check ."},
            {"run": "python -m ruff format --check --diff ."},
            {"run": "python tools/validate_skills.py"},
            {"run": "python tools/validate_marketplace.py"},
            {"run": "python tools/tf_module_layout.py --self-test"},
            {
                "run": (
                    "pytest --cov=tools --cov-branch --cov-report=term-missing "
                    "--cov-report=json:coverage.json --cov-fail-under=90"
                )
            },
            {"run": "python -m tests.test_coverage_threshold coverage.json"},
            {"run": scanner_runs},
        ],
    }


def test_validate_marketplace_files_actual() -> None:
    errors = validate_marketplace_files(REPO_ROOT)
    assert errors == []


def test_check_plugin_entry() -> None:
    valid = {"name": "test-plugin", "source": "./", "version": "1.0.0"}
    assert _check_plugin_entry(valid, 0) == []
    assert any("must be a mapping" in e for e in _check_plugin_entry("invalid", 0))
    missing_name = {"source": "./", "version": "1.0.0"}
    assert any("missing required field 'name'" in e for e in _check_plugin_entry(missing_name, 0))


def test_validate_marketplace_json(tmp_path: Path) -> None:
    missing_path = tmp_path / "nonexistent.json"
    assert any("Missing marketplace file" in e for e in _validate_marketplace_json(missing_path))

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{broken", encoding="utf-8")
    assert any("Failed to parse" in e for e in _validate_marketplace_json(bad_json))

    not_dict = tmp_path / "list.json"
    not_dict.write_text("[]", encoding="utf-8")
    assert any("must be a JSON object" in e for e in _validate_marketplace_json(not_dict))

    missing_fields = tmp_path / "incomplete.json"
    missing_fields.write_text('{"name": "repo"}', encoding="utf-8")
    errs = _validate_marketplace_json(missing_fields)
    assert any("missing required top-level key 'owner'" in e for e in errs)
    assert any("missing required top-level key 'plugins'" in e for e in errs)

    bad_plugins = tmp_path / "bad_plugins.json"
    bad_plugins.write_text('{"name": "r", "owner": "o", "plugins": "not-a-list"}', encoding="utf-8")
    assert any("'plugins' must be a list" in e for e in _validate_marketplace_json(bad_plugins))


def test_validate_plugin_json(tmp_path: Path) -> None:
    missing_path = tmp_path / "nonexistent.json"
    assert any("Missing plugin manifest" in e for e in _validate_plugin_json(missing_path))

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{broken", encoding="utf-8")
    assert any("Failed to parse" in e for e in _validate_plugin_json(bad_json))

    not_dict = tmp_path / "list.json"
    not_dict.write_text("[]", encoding="utf-8")
    assert any("must be a JSON object" in e for e in _validate_plugin_json(not_dict))

    missing_fields = tmp_path / "incomplete.json"
    missing_fields.write_text('{"name": "plugin"}', encoding="utf-8")
    errs = _validate_plugin_json(missing_fields)
    assert any("missing required top-level key 'version'" in e for e in errs)
    assert any("missing required top-level key 'skills'" in e for e in errs)


def test_validate_release_scanners_valid() -> None:
    job = _make_valid_verify_job()
    assert validate_release_scanners(job, "verify") == []


def test_validate_release_scanners_rejects_inline_comment_bypass() -> None:
    job = _make_valid_verify_job()
    comment_scanners = " ".join(REQUIRED_RELEASE_SCANNERS)
    bypass_cmd = f"python3 -B scripts/lint_skills.py # {comment_scanners}"
    job["steps"] = [s for s in job["steps"] if not any(sc in s.get("run", "") for sc in REQUIRED_RELEASE_SCANNERS)]
    job["steps"].append({"run": bypass_cmd})
    errs = validate_release_scanners(job, "verify")
    for scanner in REQUIRED_RELEASE_SCANNERS:
        assert any(f"missing required scanner: {scanner}" in e for e in errs)


def test_validate_release_scanners_rejects_python_c_argument_bypass() -> None:
    job = _make_valid_verify_job()
    job["steps"] = [s for s in job["steps"] if not any(sc in s.get("run", "") for sc in REQUIRED_RELEASE_SCANNERS)]
    for scanner in REQUIRED_RELEASE_SCANNERS:
        job["steps"].append({"run": f'python -c "" scripts/{scanner}'})
    errs = validate_release_scanners(job, "verify")
    for scanner in REQUIRED_RELEASE_SCANNERS:
        assert any(f"missing required scanner: {scanner}" in e for e in errs)


def test_validate_release_scanners_rejects_extra_arguments() -> None:
    job = _make_valid_verify_job()
    job["steps"] = [s for s in job["steps"] if not any(sc in s.get("run", "") for sc in REQUIRED_RELEASE_SCANNERS)]
    for scanner in REQUIRED_RELEASE_SCANNERS:
        job["steps"].append({"run": f"python3 -B scripts/{scanner} --extra-arg"})
    errs = validate_release_scanners(job, "verify")
    for scanner in REQUIRED_RELEASE_SCANNERS:
        assert any(f"missing required scanner: {scanner}" in e for e in errs)


def test_validate_release_scanners_missing_one() -> None:
    job = _make_valid_verify_job()
    job["steps"] = [s for s in job["steps"] if "privacy_scan.py" not in s.get("run", "")]
    errs = validate_release_scanners(job, "verify")
    assert any("missing required scanner: privacy_scan.py" in e for e in errs)


def test_validate_release_scanners_banned_prefixes() -> None:
    job = _make_valid_verify_job()
    job["steps"].append({"run": "true python3 -B scripts/privacy_scan.py"})
    errs = validate_release_scanners(job, "verify")
    assert any("banned prefix" in e for e in errs)


def test_validate_release_scanners_invalid_steps() -> None:
    errs = validate_release_scanners({"steps": "not-a-list"}, "verify")
    assert any("steps must be a list" in e for e in errs)


def test_validate_release_workflow_actual() -> None:
    assert validate_release_workflow(RELEASE_WORKFLOW_PATH) == []


def test_validate_release_workflow_missing_job() -> None:
    doc = {"jobs": {"other": {"steps": []}}}
    errs = validate_release_workflow(doc)
    assert any("missing 'verify' or 'release' target job" in e for e in errs)


def test_validate_release_workflow_malformed() -> None:
    assert any("must be a mapping" in e for e in validate_release_workflow("- item"))
    assert any("Failed to parse YAML" in e for e in validate_release_workflow("not: [valid"))


def test_validate_release_workflow_jobs_not_mapping() -> None:
    assert any("jobs must be a mapping" in e for e in validate_release_workflow({"jobs": "not-dict"}))


def test_validate_release_scanners_step_not_dict() -> None:
    assert validate_release_scanners({"steps": ["not-a-dict"]}, "verify") != []


def test_main_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    assert code == 0
    out = capsys.readouterr().out
    assert "Validated marketplace files and release workflow successfully." in out


def test_main_cli_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("tools.validate_marketplace.validate_marketplace_files", lambda _root: ["forced error"])
    code = main([])
    assert code == 1
    err = capsys.readouterr().err
    assert "forced error" in err


def test_validate_release_workflow_rejects_top_level_env() -> None:
    import yaml

    doc = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    doc["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
    errs = validate_release_workflow(doc)
    assert any("disallowed env variable" in e or "banned env variable" in e for e in errs)


def test_validate_release_workflow_rejects_verify_job_env() -> None:
    import yaml

    doc = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    doc["jobs"]["verify"]["env"] = {"PYTEST_ADDOPTS": "--no-cov"}
    errs = validate_release_workflow(doc)
    assert any("disallowed env variable" in e or "banned env variable" in e for e in errs)


def test_validate_release_workflow_rejects_step_env_in_verify() -> None:
    import yaml

    doc = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    for s in doc["jobs"]["verify"]["steps"]:
        if "pytest" in s.get("run", ""):
            s["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
    errs = validate_release_workflow(doc)
    assert any("disallowed env variable" in e or "banned env variable" in e for e in errs)


def test_validate_release_workflow_rejects_secondary_pip_install() -> None:
    import yaml

    doc = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    doc["jobs"]["verify"]["steps"].insert(4, {"name": "Reinstall ruff", "run": "pip install ruff==0.9.10"})
    errs = validate_release_workflow(doc)
    assert any("pip install steps" in e or "secondary pip install" in e for e in errs)


def test_validate_quality_workflow_exported() -> None:
    from tools.validate_marketplace import validate_quality_workflow

    quality_path = REPO_ROOT / ".github" / "workflows" / "quality.yaml"
    assert validate_quality_workflow(quality_path) == []


def test_main_cli_quality_workflow_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("tools.validate_marketplace.validate_quality_workflow", lambda _path: ["forced quality error"])
    code = main([])
    assert code == 1
    err = capsys.readouterr().err
    assert "forced quality error" in err


def test_validate_release_scanners_rejects_step_if_and_coe() -> None:
    job = _make_valid_verify_job()
    for s in job["steps"]:
        if "privacy_scan.py" in s.get("run", ""):
            s["if"] = "${{ 1 == 2 }}"
            s["continue-on-error"] = True
    errs = validate_release_scanners(job, "verify")
    assert any("must not have an if: condition" in e for e in errs)
    assert any("must not have continue-on-error" in e for e in errs)


def test_validate_quality_workflow_rejects_missing_quality_job() -> None:
    from tools.validate_marketplace import validate_quality_workflow

    quality_path = REPO_ROOT / ".github" / "workflows" / "quality.yaml"
    doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    del doc["jobs"]["quality"]
    errs = validate_quality_workflow(doc)
    assert any("missing target tests/quality job" in e or "missing required 'quality' job" in e for e in errs)


def test_validate_quality_workflow_rejects_pr_title_spoofing_on_mandatory_script() -> None:
    from tools.validate_marketplace import validate_quality_workflow

    quality_path = REPO_ROOT / ".github" / "workflows" / "quality.yaml"
    doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    for step in doc["jobs"]["quality"]["steps"]:
        if "privacy_scan.py" in step.get("run", ""):
            step["name"] = "Validate PR title and privacy"
            step["if"] = "github.event_name == 'pull_request' && 1 == 2"
    errs = validate_quality_workflow(doc)
    assert any("must not have an if: condition" in e for e in errs) or any(
        "missing required step: privacy_scan.py" in e for e in errs
    )


def test_validate_quality_workflow_rejects_python_c_argument_bypass() -> None:
    from tools.validate_marketplace import validate_quality_workflow

    quality_path = REPO_ROOT / ".github" / "workflows" / "quality.yaml"
    doc = yaml.safe_load(quality_path.read_text(encoding="utf-8"))
    for step in doc["jobs"]["quality"]["steps"]:
        if "privacy_scan.py" in step.get("run", ""):
            step["run"] = 'python3 -c "pass" scripts/privacy_scan.py'
    errs = validate_quality_workflow(doc)
    assert any("missing required step: privacy_scan.py" in e for e in errs)


def _make_valid_release_job() -> dict[str, Any]:
    return {
        "runs-on": "ubuntu-latest",
        "permissions": {"contents": "write"},
        "steps": [
            {
                "uses": "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
                "with": {
                    "ref": "${{ github.event.workflow_run.head_sha || github.sha }}",
                    "fetch-depth": 0,
                },
            },
            {
                "uses": "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
                "with": {"python-version": "3.12"},
            },
            {
                "name": "Install dependencies",
                "run": "python -m pip install -r requirements-dev.txt",
            },
            {
                "name": "Plan release",
                "id": "plan",
                "run": "python scripts/semantic_release.py --notes-file release-notes.md",
            },
            {
                "name": "Create tag",
                "if": "steps.plan.outputs.release_needed == 'true'",
                "run": (
                    'git config user.name "github-actions[bot]"\n'
                    'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"\n'
                    'git tag -a "${{ steps.plan.outputs.next_tag }}" -F release-notes.md\n'
                    'git push origin "${{ steps.plan.outputs.next_tag }}"\n'
                ),
            },
            {
                "name": "Create GitHub release",
                "if": "steps.plan.outputs.release_needed == 'true'",
                "env": {"GH_TOKEN": "${{ github.token }}"},
                "run": 'gh release create "${{ steps.plan.outputs.next_tag }}" --notes-file release-notes.md',
            },
        ],
    }


def test_validate_release_publish_job_valid() -> None:
    from tools.validate_marketplace import validate_release_publish_job

    job = _make_valid_release_job()
    assert validate_release_publish_job(job) == []


def test_validate_release_publish_job_attributes() -> None:
    from tools.validate_marketplace import validate_release_publish_job

    job1 = _make_valid_release_job()
    job1["permissions"] = {"contents": "read"}
    assert any("permissions must be {'contents': 'write'}" in e for e in validate_release_publish_job(job1))

    job2 = _make_valid_release_job()
    job2["runs-on"] = "self-hosted"
    assert any("runs-on must be 'ubuntu-latest'" in e for e in validate_release_publish_job(job2))

    job3 = _make_valid_release_job()
    job3["container"] = "attacker/image"
    assert any("must not declare a container" in e for e in validate_release_publish_job(job3))

    job4 = _make_valid_release_job()
    job4["services"] = {"redis": {}}
    assert any("must not declare services" in e for e in validate_release_publish_job(job4))

    job5 = _make_valid_release_job()
    job5["continue-on-error"] = True
    assert any("must not declare continue-on-error" in e for e in validate_release_publish_job(job5))

    job6 = _make_valid_release_job()
    job6["steps"] = "not a list"
    assert any("steps must be a list" in e for e in validate_release_publish_job(job6))

    job7 = _make_valid_release_job()
    job7["steps"] = job7["steps"][:4]
    assert any("must have exactly 6 canonical steps" in e for e in validate_release_publish_job(job7))


def test_validate_release_step_0_and_1_errors() -> None:
    from tools.validate_marketplace import validate_release_publish_job

    for bad_s0 in (
        "not a dict",
        {"uses": "actions/checkout@main"},
        {"uses": "actions/checkout@v7.0.0"},
        {
            "uses": "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
            "with": {},
        },
        {
            "uses": "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
            "with": {
                "ref": "${{ github.event.workflow_run.head_sha || github.sha }}",
                "fetch-depth": 0,
            },
            "env": {"A": "B"},
        },
    ):
        job = _make_valid_release_job()
        job["steps"][0] = bad_s0
        assert len(validate_release_publish_job(job)) > 0

    # Pinned commit SHA is accepted
    job_sha = _make_valid_release_job()
    job_sha["steps"][0]["uses"] = "actions/checkout@0123456789abcdef0123456789abcdef01234567"
    job_sha["steps"][1]["uses"] = "actions/setup-python@0123456789abcdef0123456789abcdef01234567"
    assert validate_release_publish_job(job_sha) == []

    for bad_s1 in (
        "not a dict",
        {"uses": "actions/setup-python@attacker"},
        {"uses": "actions/setup-python@v6.3.0"},
        {"uses": "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1", "with": {"python-version": "3.11"}},
        {
            "uses": "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            "with": {"python-version": "3.12"},
            "env": {"A": "B"},
        },
    ):
        job = _make_valid_release_job()
        job["steps"][1] = bad_s1
        assert len(validate_release_publish_job(job)) > 0


def test_validate_release_step_2_3_4_errors() -> None:
    from tools.validate_marketplace import validate_release_publish_job

    # Step 2 errors
    for bad_s2 in (
        "not a dict",
        {"name": "Wrong", "id": "plan", "run": "python scripts/semantic_release.py --notes-file release-notes.md"},
        {"name": "Plan release", "id": "plan", "run": "echo evil"},
        {
            "name": "Plan release",
            "id": "plan",
            "run": "python scripts/semantic_release.py --notes-file release-notes.md",
            "env": {"A": "B"},
        },
    ):
        job = _make_valid_release_job()
        job["steps"][2] = bad_s2
        assert len(validate_release_publish_job(job)) > 0

    # Step 3 errors
    for bad_s3 in (
        "not a dict",
        {"name": "Wrong"},
        {"name": "Create tag", "if": "true", "run": "git tag -a v1"},
        {"name": "Create tag", "if": "steps.plan.outputs.release_needed == 'true'", "run": "echo evil"},
        {
            "name": "Create tag",
            "if": "steps.plan.outputs.release_needed == 'true'",
            "run": "git config user.name 'b'\ngit config user.email 'e'\ngit tag -a\ngit push",
            "env": {"A": "B"},
        },
    ):
        job = _make_valid_release_job()
        job["steps"][3] = bad_s3
        assert len(validate_release_publish_job(job)) > 0

    # Step 4 errors
    for bad_s4 in (
        "not a dict",
        {"name": "Wrong"},
        {"name": "Create GitHub release", "if": "true"},
        {
            "name": "Create GitHub release",
            "if": "steps.plan.outputs.release_needed == 'true'",
            "run": "echo evil",
            "env": {"GH_TOKEN": "${{ github.token }}"},
        },
        {
            "name": "Create GitHub release",
            "if": "steps.plan.outputs.release_needed == 'true'",
            "run": 'gh release create "${{ steps.plan.outputs.next_tag }}" --notes-file release-notes.md',
            "env": {"GH_TOKEN": "stolen", "EXTRA": "1"},
        },
    ):
        job = _make_valid_release_job()
        job["steps"][4] = bad_s4
        assert len(validate_release_publish_job(job)) > 0


def test_validate_release_workflow_triggers_and_jobs_errors() -> None:
    from tools.validate_marketplace import validate_release_workflow

    doc = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))

    # Unparseable/bad input
    assert any("Failed to parse YAML" in e for e in validate_release_workflow("not: yaml: [broken"))

    # Bad permissions
    doc_bad_perm = dict(doc)
    doc_bad_perm["permissions"] = {"contents": "write"}
    assert any("release workflow permissions" in e for e in validate_release_workflow(doc_bad_perm))

    # Extra triggers as dict
    doc_trig_dict = dict(doc)
    on_key = "on" if "on" in doc_trig_dict else True
    doc_trig_dict[on_key] = {"workflow_dispatch": {}, "pull_request": {}}
    assert any("release workflow triggers" in e for e in validate_release_workflow(doc_trig_dict))

    # Extra triggers as list
    doc_trig_list = dict(doc)
    doc_trig_list[on_key] = ["workflow_dispatch", "push"]
    assert any("release workflow triggers" in e for e in validate_release_workflow(doc_trig_list))

    # Jobs not mapping
    doc_bad_jobs = dict(doc)
    doc_bad_jobs["jobs"] = "not a map"
    assert any("Release workflow jobs must be a mapping" in e for e in validate_release_workflow(doc_bad_jobs))

    # Missing verify and release
    doc_no_jobs = dict(doc)
    doc_no_jobs["jobs"] = {}
    assert any("missing 'verify' or 'release' target job" in e for e in validate_release_workflow(doc_no_jobs))

    # Extra jobs
    doc_extra_job = dict(doc)
    doc_extra_job["jobs"] = dict(doc["jobs"])
    doc_extra_job["jobs"]["evil"] = {"runs-on": "ubuntu-latest"}
    assert any(
        "release workflow jobs must contain only 'verify' and 'release'" in e
        for e in validate_release_workflow(doc_extra_job)
    )
