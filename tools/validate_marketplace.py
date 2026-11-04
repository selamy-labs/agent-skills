#!/usr/bin/env python3
"""Marketplace and release workflow validator."""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, cast

import yaml

try:
    from tools.validate_skills import (
        ALLOWED_JOB_LEVEL_KEYS,
        ALLOWED_USES_ACTIONS,
        CANONICAL_QUALITY_RUN_ARGVS,
        CANONICAL_TESTS_RUN_ARGVS,
        check_banned_prefixes_and_operators,
        validate_env_configuration,
        validate_job_gates,
        validate_job_level_keys,
        validate_quality_workflow,
        validate_shell_configuration,
        validate_workflow_step_imports,
    )
except ModuleNotFoundError:
    _root = Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from tools.validate_skills import (
        ALLOWED_JOB_LEVEL_KEYS,
        ALLOWED_USES_ACTIONS,
        CANONICAL_QUALITY_RUN_ARGVS,
        CANONICAL_TESTS_RUN_ARGVS,
        check_banned_prefixes_and_operators,
        validate_env_configuration,
        validate_job_gates,
        validate_job_level_keys,
        validate_quality_workflow,
        validate_shell_configuration,
        validate_workflow_step_imports,
    )

__all__ = [
    "ALLOWED_JOB_LEVEL_KEYS",
    "ALLOWED_USES_ACTIONS",
    "CANONICAL_QUALITY_RUN_ARGVS",
    "CANONICAL_TESTS_RUN_ARGVS",
    "validate_job_level_keys",
    "validate_marketplace_files",
    "validate_quality_workflow",
    "validate_release_publish_job",
    "validate_release_scanners",
    "validate_release_workflow",
    "validate_workflow_step_imports",
    "main",
]

REQUIRED_RELEASE_SCANNERS = (
    "privacy_scan.py",
    "test_privacy_scan.py",
    "security_scan.py",
    "test_security_scan.py",
    "public_api_stability.py",
    "test_pr_workflow_cancellation.py",
    "test_semantic_release.py",
)

CANONICAL_RELEASE_SCANNER_COMMANDS: dict[tuple[str, ...], str] = {
    cmd: scanner
    for scanner in REQUIRED_RELEASE_SCANNERS
    for cmd in (
        ("python", f"scripts/{scanner}"),
        ("python3", f"scripts/{scanner}"),
        ("python", "-B", f"scripts/{scanner}"),
        ("python3", "-B", f"scripts/{scanner}"),
    )
}


def _check_plugin_entry(plugin: Any, idx: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(plugin, dict):
        return [f"marketplace.json plugin #{idx} must be a mapping"]
    for key in ("name", "source", "version"):
        if not plugin.get(key):
            errors.append(f"marketplace.json plugin #{idx} missing required field {key!r}")
    return errors


def _validate_marketplace_json(path: Path) -> list[str]:
    if not path.is_file():
        return [f"Missing marketplace file: {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"Failed to parse {path}: {exc}"]
    if not isinstance(data, dict):
        return [f"{path} root must be a JSON object"]

    errors: list[str] = []
    for key in ("name", "owner", "plugins"):
        if not data.get(key):
            errors.append(f"{path} missing required top-level key {key!r}")

    plugins = data.get("plugins")
    if isinstance(plugins, list):
        for idx, plugin in enumerate(plugins):
            errors.extend(_check_plugin_entry(plugin, idx))
    else:
        errors.append(f"{path} 'plugins' must be a list")
    return errors


def _validate_plugin_json(path: Path) -> list[str]:
    if not path.is_file():
        return [f"Missing plugin manifest: {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"Failed to parse {path}: {exc}"]
    if not isinstance(data, dict):
        return [f"{path} root must be a JSON object"]

    errors: list[str] = []
    for key in ("name", "version", "skills"):
        if not data.get(key):
            errors.append(f"{path} missing required top-level key {key!r}")
    return errors


def validate_marketplace_files(repo_root: Path | None = None) -> list[str]:
    """Validate .claude-plugin/marketplace.json and plugin.json."""
    root = repo_root or Path(__file__).resolve().parents[1]
    plugin_dir = root / ".claude-plugin"
    errors: list[str] = []
    errors.extend(_validate_marketplace_json(plugin_dir / "marketplace.json"))
    errors.extend(_validate_plugin_json(plugin_dir / "plugin.json"))
    return errors


def _scan_step_for_scanners(run_cmd: str, found_scanners: set[str], errors: list[str]) -> None:
    for raw_line in run_cmd.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line_errs = check_banned_prefixes_and_operators(line)
        if line_errs:
            errors.extend(line_errs)
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as exc:
            errors.append(f"Invalid shell command syntax in line {line!r}: {exc}")
            continue
        scanner = CANONICAL_RELEASE_SCANNER_COMMANDS.get(tuple(tokens))
        if scanner:
            found_scanners.add(scanner)


def _check_scanner_step_guards(step: dict[str, Any], job_name: str, errors: list[str]) -> None:
    step_if = step.get("if")
    if step_if is not None and str(step_if).strip():
        errors.append(f"step {step.get('name', 'unnamed')!r} in '{job_name}' must not have an if: condition")
    coe = step.get("continue-on-error")
    if coe is not None and coe is not False and str(coe).strip().lower() not in ("false", "${{ false }}"):
        errors.append(f"step {step.get('name', 'unnamed')!r} in '{job_name}' must not have continue-on-error")


def validate_release_scanners(job: dict[str, Any], job_name: str) -> list[str]:
    """Ensure release workflow contains the required public safety and stability scanners."""
    steps = job.get("steps", [])
    if not isinstance(steps, list):
        return [f"job '{job_name}' steps must be a list"]

    found_scanners: set[str] = set()
    errors: list[str] = []

    for step in steps:
        if not isinstance(step, dict):
            continue
        run_cmd = step.get("run", "")
        if not run_cmd:
            continue
        _check_scanner_step_guards(step, job_name, errors)
        _scan_step_for_scanners(run_cmd, found_scanners, errors)

    for scanner in REQUIRED_RELEASE_SCANNERS:
        if scanner not in found_scanners:
            errors.append(f"job '{job_name}' missing required scanner: {scanner}")

    return errors


def _load_release_mapping(workflow_input: dict[str, Any] | str | Path) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(workflow_input, dict):
        return (workflow_input, [])
    p = Path(workflow_input)
    content = p.read_text(encoding="utf-8") if p.is_file() else str(workflow_input)
    try:
        doc = yaml.safe_load(content)
        if not isinstance(doc, dict):
            return (None, ["Release workflow must be a mapping"])
        return (doc, [])
    except Exception as exc:
        return (None, [f"Failed to parse YAML: {exc}"])


CANONICAL_CHECKOUT_WITH: dict[str, Any] = {
    "ref": "${{ github.event.workflow_run.head_sha || github.sha }}",
    "fetch-depth": 0,
}
CANONICAL_SETUP_PYTHON_WITH: dict[str, Any] = {
    "python-version": "3.12",
}
CANONICAL_PLAN_CMD: str = "python scripts/semantic_release.py --notes-file release-notes.md"
CANONICAL_TAG_CMD_LINES: tuple[str, ...] = (
    'git config user.name "github-actions[bot]"',
    'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"',
    'git tag -a "${{ steps.plan.outputs.next_tag }}" -F release-notes.md',
    'git push origin "${{ steps.plan.outputs.next_tag }}"',
)
CANONICAL_RELEASE_CMD: str = 'gh release create "${{ steps.plan.outputs.next_tag }}" --notes-file release-notes.md'


def _is_pinned_action(uses_val: Any, action_name: str) -> bool:
    if not isinstance(uses_val, str):
        return False
    val = uses_val.strip()
    return bool(re.fullmatch(rf"{re.escape(action_name)}@([0-9a-fA-F]{{40}})", val))


def _check_release_step_0(s0: Any, errors: list[str]) -> None:
    if not isinstance(s0, dict) or not _is_pinned_action(s0.get("uses"), "actions/checkout"):
        errors.append("job 'release' step 0 must use pinned actions/checkout")
        return
    if s0.get("with") != CANONICAL_CHECKOUT_WITH:
        errors.append("job 'release' step 0 checkout must specify exact fetch-depth and ref")
    if "env" in s0:
        errors.append("job 'release' step 0 must not declare env")


def _check_release_step_1(s1: Any, errors: list[str]) -> None:
    if not isinstance(s1, dict) or not _is_pinned_action(s1.get("uses"), "actions/setup-python"):
        errors.append("job 'release' step 1 must use pinned actions/setup-python")
        return
    if s1.get("with") != CANONICAL_SETUP_PYTHON_WITH:
        errors.append("job 'release' step 1 must specify python-version: '3.12'")
    if "env" in s1:
        errors.append("job 'release' step 1 must not declare env")


def _check_release_step_2(s2: Any, errors: list[str]) -> None:
    if not isinstance(s2, dict) or s2.get("name") != "Plan release" or s2.get("id") != "plan":
        errors.append("job 'release' step 2 must be Plan release")
        return
    if str(s2.get("run", "")).strip() != CANONICAL_PLAN_CMD:
        errors.append("job 'release' step 2 must run exact canonical semantic_release.py command")
    if "env" in s2:
        errors.append("job 'release' step 2 must not declare env")


def _check_release_step_3(s3: Any, errors: list[str]) -> None:
    if not isinstance(s3, dict) or s3.get("name") != "Create tag":
        errors.append("job 'release' step 3 must be Create tag")
        return
    if s3.get("if") != "steps.plan.outputs.release_needed == 'true'":
        errors.append("job 'release' step 3 must check release_needed")
    lines = tuple(line.strip() for line in str(s3.get("run", "")).strip().splitlines() if line.strip())
    if lines != CANONICAL_TAG_CMD_LINES:
        errors.append("job 'release' step 3 must run exact canonical git tag and push commands")
    if "env" in s3:
        errors.append("job 'release' step 3 must not declare env")


def _check_release_step_4(s4: Any, errors: list[str]) -> None:
    if not isinstance(s4, dict) or s4.get("name") != "Create GitHub release":
        errors.append("job 'release' step 4 must be Create GitHub release")
        return
    if s4.get("if") != "steps.plan.outputs.release_needed == 'true'":
        errors.append("job 'release' step 4 must check release_needed")
    if str(s4.get("run", "")).strip() != CANONICAL_RELEASE_CMD:
        errors.append("job 'release' step 4 must run canonical gh release create")
    if s4.get("env") != {"GH_TOKEN": "${{ github.token }}"}:
        errors.append("job 'release' step 4 env must be exactly GH_TOKEN")


def _check_release_publish_steps(steps: Any, errors: list[str]) -> None:
    if not isinstance(steps, list):
        errors.append("job 'release' steps must be a list")
        return
    if len(steps) != 5:
        errors.append(f"job 'release' must have exactly 5 canonical steps, got {len(steps)}")
        return
    _check_release_step_0(steps[0], errors)
    _check_release_step_1(steps[1], errors)
    _check_release_step_2(steps[2], errors)
    _check_release_step_3(steps[3], errors)
    _check_release_step_4(steps[4], errors)


def validate_release_publish_job(job: dict[str, Any], job_name: str = "release") -> list[str]:
    """Validate release publish job permissions, shell, env and canonical step allowlist."""
    errors: list[str] = list(validate_shell_configuration(f"job '{job_name}'", job))
    errors.extend(validate_env_configuration(f"job '{job_name}'", job, allowed_keys=frozenset()))
    perms = job.get("permissions")
    if perms != {"contents": "write"}:
        errors.append(f"job '{job_name}' permissions must be {{'contents': 'write'}}, got: {perms!r}")
    if job.get("runs-on") != "ubuntu-latest":
        errors.append(f"job '{job_name}' runs-on must be 'ubuntu-latest', got: {job.get('runs-on')!r}")
    if "container" in job:
        errors.append(f"job '{job_name}' must not declare a container")
    if "services" in job:
        errors.append(f"job '{job_name}' must not declare services")
    if job.get("continue-on-error") is not None:
        errors.append(f"job '{job_name}' must not declare continue-on-error")
    _check_release_publish_steps(job.get("steps", []), errors)
    return errors


def _validate_release_workflow_triggers(on_triggers: Any, errors: list[str]) -> None:
    allowed_triggers = {"workflow_run", "workflow_dispatch"}
    if isinstance(on_triggers, dict):
        extra = set(on_triggers.keys()) - allowed_triggers
        if extra:
            errors.append(f"release workflow triggers must only be {sorted(allowed_triggers)}, got: {sorted(extra)}")
    elif isinstance(on_triggers, list):
        extra = set(on_triggers) - allowed_triggers
        if extra:
            errors.append(f"release workflow triggers must only be {sorted(allowed_triggers)}, got: {sorted(extra)}")


def _validate_release_workflow_jobs(jobs: Any, errors: list[str]) -> None:
    if not isinstance(jobs, dict):
        errors.append("Release workflow jobs must be a mapping")
        return

    verify_job = jobs.get("verify")
    release_job = jobs.get("release")
    if verify_job is None and release_job is None:
        errors.append("release workflow missing 'verify' or 'release' target job")
        return

    extra_jobs = set(jobs.keys()) - {"verify", "release"}
    if extra_jobs:
        errors.append(
            f"release workflow jobs must contain only 'verify' and 'release', got unexpected: {sorted(extra_jobs)}"
        )

    if verify_job is not None:
        errors.extend(validate_job_gates(verify_job, "verify"))
        errors.extend(validate_release_scanners(verify_job, "verify"))

    if release_job is not None:
        errors.extend(validate_release_publish_job(release_job, "release"))


def validate_release_workflow(workflow_input: dict[str, Any] | str | Path) -> list[str]:
    """Validate release workflow contract."""
    doc, load_errs = _load_release_mapping(workflow_input)
    if doc is None:
        return load_errs

    errors = list(validate_shell_configuration("release workflow", doc))
    errors.extend(validate_env_configuration("release workflow", doc, allowed_keys=frozenset()))

    wf_perms = doc.get("permissions")
    if wf_perms != {"contents": "read"}:
        errors.append(f"release workflow permissions must be {{'contents': 'read'}}, got: {wf_perms!r}")

    on_triggers = doc.get("on") if "on" in doc else cast(dict[Any, Any], doc).get(True)
    _validate_release_workflow_triggers(on_triggers, errors)
    _validate_release_workflow_jobs(doc.get("jobs"), errors)
    errors.extend(validate_workflow_step_imports(doc))

    return errors


def main(argv: list[str] | None = None) -> int:
    """Validate marketplace files and release workflow contract."""
    root = Path(__file__).resolve().parents[1]
    mp_errors = validate_marketplace_files(root)
    workflow_path = root / ".github" / "workflows" / "release.yaml"
    wf_errors = validate_release_workflow(workflow_path) if workflow_path.is_file() else ["release.yaml not found"]
    quality_path = root / ".github" / "workflows" / "quality.yaml"
    q_errors = validate_quality_workflow(quality_path) if quality_path.is_file() else ["quality.yaml not found"]

    all_errors = mp_errors + wf_errors + q_errors
    if all_errors:
        for err in all_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print("Validated marketplace files and release workflow successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
