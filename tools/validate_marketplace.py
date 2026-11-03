#!/usr/bin/env python3
"""Marketplace and release workflow validator."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

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
    )

__all__ = [
    "ALLOWED_JOB_LEVEL_KEYS",
    "ALLOWED_USES_ACTIONS",
    "CANONICAL_QUALITY_RUN_ARGVS",
    "CANONICAL_TESTS_RUN_ARGVS",
    "validate_job_level_keys",
    "validate_marketplace_files",
    "validate_quality_workflow",
    "validate_release_scanners",
    "validate_release_workflow",
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


def validate_release_workflow(workflow_input: dict[str, Any] | str | Path) -> list[str]:
    """Validate release workflow contract."""
    doc, load_errs = _load_release_mapping(workflow_input)
    if doc is None:
        return load_errs

    errors = list(validate_shell_configuration("release workflow", doc))
    errors.extend(validate_env_configuration("release workflow", doc, allowed_keys=frozenset()))
    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return ["Release workflow jobs must be a mapping"]

    target_job = jobs.get("verify") or jobs.get("release")
    if target_job is None:
        errors.append("release workflow missing 'verify' or 'release' target job")
    else:
        target_name = "verify" if "verify" in jobs else "release"
        errors.extend(validate_job_gates(target_job, target_name))
        errors.extend(validate_release_scanners(target_job, target_name))

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
