"""Regression tests for contained Gemini CLI headless orchestration."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "skills" / "orchestrate-gemini" / "scripts" / "run_headless.py"


def _run(command: list[str], cwd: Path) -> str:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir()
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.name", "Test Operator"], path)
    _run(["git", "config", "user.email", "operator@example.com"], path)
    (path / "README.md").write_text("fixture\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-qm", "fixture"], path)
    return _run(["git", "rev-parse", "HEAD"], path)


def _write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def _fake_gemini(path: Path) -> Path:
    executable = path / "gemini"
    log = path / "gemini-invocations.jsonl"
    behavior = path / "gemini-behavior"
    prompt_capture = path / "gemini-prompt.txt"
    child_capture = path / "gemini-child.pid"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, sys, time\n"
        "record = {\n"
        "    'argv': sys.argv[1:],\n"
        "    'gemini_home': os.environ.get('GEMINI_CLI_HOME'),\n"
        "    'system_settings': os.environ.get('GEMINI_CLI_SYSTEM_SETTINGS_PATH'),\n"
        "    'tmpdir': os.environ.get('TMPDIR'),\n"
        "    'sandbox': os.environ.get('GEMINI_SANDBOX'),\n"
        "    'sandbox_flags': os.environ.get('SANDBOX_FLAGS'),\n"
        "    'sandbox_mounts': os.environ.get('SANDBOX_MOUNTS'),\n"
        "}\n"
        f"with pathlib.Path({str(log)!r}).open('a') as output:\n"
        "    output.write(json.dumps(record) + '\\n')\n"
        "if '--version' in sys.argv:\n"
        "    print('0.51.0')\n"
        "elif '--help' in sys.argv:\n"
        "    print('--model --output-format --approval-mode --sandbox --admin-policy --extensions --resume')\n"
        "else:\n"
        f"    behavior_path = pathlib.Path({str(behavior)!r})\n"
        "    behavior = behavior_path.read_text().strip() if behavior_path.exists() else 'success'\n"
        "    prompt_arg = sys.argv[sys.argv.index('--prompt') + 1]\n"
        "    prompt = pathlib.Path(prompt_arg[1:]).read_text() if prompt_arg.startswith('@') else prompt_arg\n"
        f"    pathlib.Path({str(prompt_capture)!r}).write_text(prompt)\n"
        "    model = sys.argv[sys.argv.index('--model') + 1]\n"
        "    if behavior == 'malformed':\n"
        "        print('not-json')\n"
        "        raise SystemExit(0)\n"
        "    if behavior in {'hang_child', 'detached_child'}:\n"
        "        child = subprocess.Popen(\n"
        "            [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "            start_new_session=behavior == 'detached_child',\n"
        "        )\n"
        f"        pathlib.Path({str(child_capture)!r}).write_text(str(child.pid))\n"
        "        print(json.dumps({'type': 'init', 'session_id': 'session-test', 'model': model}), flush=True)\n"
        "        time.sleep(60)\n"
        "    resolved_model = 'wrong-model' if behavior == 'model_mismatch' else model\n"
        "    print(json.dumps({'type': 'init', 'session_id': 'session-test', 'model': resolved_model}))\n"
        "    print(json.dumps({'type': 'message', 'role': 'user', 'content': prompt}))\n"
        "    if behavior not in {'empty', 'fatal_event'}:\n"
        "        print(json.dumps({'type': 'message', 'role': 'assistant', 'content': 'fixture complete'}))\n"
        "    if behavior == 'fatal_event':\n"
        "        print(json.dumps({'type': 'error', 'severity': 'error', 'message': 'fatal fixture error'}))\n"
        "    status = 'error' if behavior == 'error_status' else 'success'\n"
        "    print(json.dumps({'type': 'result', 'status': status}))\n"
        "    if behavior == 'duplicate_result':\n"
        "        print(json.dumps({'type': 'result', 'status': 'success'}))\n"
        "    if behavior == 'write_outside':\n"
        "        pathlib.Path('outside.txt').write_text('escaped scope\\n')\n"
        "    if behavior == 'write_allowed':\n"
        "        pathlib.Path('README.md').write_text('changed in scope\\n')\n"
        "    if behavior == 'stage_allowed':\n"
        "        pathlib.Path('README.md').write_text('staged in scope\\n')\n"
        "        subprocess.run(['git', 'add', 'README.md'], check=True)\n"
        "    if behavior == 'commit_allowed':\n"
        "        pathlib.Path('README.md').write_text('committed in scope\\n')\n"
        "        subprocess.run(['git', 'add', 'README.md'], check=True)\n"
        "        subprocess.run(['git', 'commit', '-qm', 'fixture delivery'], check=True)\n"
        "    if behavior == 'delete_allowed':\n"
        "        pathlib.Path('README.md').unlink()\n"
        "    if behavior == 'rename_allowed':\n"
        "        pathlib.Path('src').mkdir()\n"
        "        pathlib.Path('README.md').rename('src/README.md')\n"
        "    if behavior == 'cli_error':\n"
        "        raise SystemExit(7)\n"
    )
    executable.chmod(0o755)
    return executable


def _fake_provider(path: Path, name: str = "docker") -> Path:
    bindir = path / "bin"
    bindir.mkdir(exist_ok=True)
    provider = bindir / name
    provider.write_text("#!/bin/sh\nexit 0\n")
    provider.chmod(0o755)
    return bindir


def _goal(base_sha: str, **overrides: object) -> dict[str, object]:
    goal: dict[str, object] = {
        "schema_version": 1,
        "goal_id": "gemini-contained-test",
        "objective": "Make the bounded fixture satisfy its tests.",
        "base_sha": base_sha,
        "allowed_paths": ["README.md", "src/", "tests/"],
        "verification_commands": [
            {"argv": ["git", "status", "--short"], "timeout_seconds": 5},
        ],
        "stop_conditions": ["scope would expand", "credentials are unavailable"],
        "max_attempts": 3,
        "auth_type": "oauth-personal",
        "allow_paid_generation": False,
    }
    goal.update(overrides)
    return goal


def _policy(path: Path) -> Path:
    policy = path / "policy.toml"
    _write_private(
        policy,
        '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\n\n'
        '[[rule]]\ntoolName = "read_file"\ndecision = "allow"\npriority = 100\n',
    )
    return policy


def _fixture(tmp_path: Path, **goal_overrides: object) -> dict[str, object]:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    goal_file = tmp_path / "goal.json"
    _write_private(goal_file, json.dumps(_goal(base_sha, **goal_overrides)))
    prompt = tmp_path / "prompt.txt"
    _write_private(prompt, "Inspect the fixture without changing it.\n")
    return {
        "repo": repo,
        "base_sha": base_sha,
        "state": state,
        "goal": goal_file,
        "prompt": prompt,
        "policy": _policy(tmp_path),
        "gemini": _fake_gemini(tmp_path),
        "path": _fake_provider(tmp_path),
    }


def _command(
    fixture: dict[str, object],
    run_name: str,
    *,
    goal_file: Path | None = None,
    provider: str = "docker",
    preflight: bool = True,
    timeout_seconds: int = 5,
    resume_from: Path | None = None,
    model: str = "gemini-test-model",
    validation_fake_responses: Path | None = None,
) -> list[str]:
    run_dir = Path(fixture["state"]) / "runs" / run_name
    command = [
        sys.executable,
        str(RUNNER),
        "--run-dir",
        str(run_dir),
        "--state-dir",
        str(fixture["state"]),
        "--cwd",
        str(fixture["repo"]),
        "--goal-file",
        str(goal_file or fixture["goal"]),
        "--prompt-file",
        str(fixture["prompt"]),
        "--policy-file",
        str(fixture["policy"]),
        "--gemini",
        str(fixture["gemini"]),
        "--sandbox-provider",
        provider,
        "--timeout-seconds",
        str(timeout_seconds),
        "--model",
        model,
        "--approval-mode",
        "plan",
    ]
    if preflight:
        command.append("--preflight-only")
    if resume_from:
        command.extend(("--resume-from", str(resume_from)))
    if validation_fake_responses:
        command.extend(("--validation-fake-responses", str(validation_fake_responses)))
    return command


def _invoke(
    fixture: dict[str, object],
    run_name: str,
    *,
    goal_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
    provider: str = "docker",
    preflight: bool = True,
    timeout_seconds: int = 5,
    resume_from: Path | None = None,
    model: str = "gemini-test-model",
    validation_fake_responses: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = _command(
        fixture,
        run_name,
        goal_file=goal_file,
        provider=provider,
        preflight=preflight,
        timeout_seconds=timeout_seconds,
        resume_from=resume_from,
        model=model,
        validation_fake_responses=validation_fake_responses,
    )
    env = os.environ | {"PATH": f"{fixture['path']}:{os.environ['PATH']}"} | (extra_env or {})
    return subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)


def _status(fixture: dict[str, object], run_name: str) -> dict[str, object]:
    path = Path(fixture["state"]) / "runs" / run_name / "status.json"
    return json.loads(path.read_text())


def _set_behavior(fixture: dict[str, object], behavior: str) -> None:
    Path(fixture["gemini"]).with_name("gemini-behavior").write_text(behavior)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


def _wait_for(path: Path, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("test_orchestrate_gemini_runner", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preflight_persists_an_immutable_durable_goal(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    result = _invoke(fixture, "attempt-001")

    assert result.returncode == 0, result.stderr
    assert _status(fixture, "attempt-001")["classification"] == "preflight_succeeded"
    canonical_goal = Path(fixture["state"]) / "goal.json"
    assert json.loads(canonical_goal.read_text())["goal_id"] == "gemini-contained-test"
    assert canonical_goal.stat().st_mode & 0o777 == 0o600
    assert (Path(fixture["state"]) / "goal.sha256").stat().st_mode & 0o777 == 0o600


def test_preflight_rejects_a_changed_durable_goal(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    assert _invoke(fixture, "attempt-001").returncode == 0
    changed = tmp_path / "changed-goal.json"
    original = json.loads(Path(fixture["goal"]).read_text())
    original["objective"] = "A different objective"
    _write_private(changed, json.dumps(original))

    result = _invoke(fixture, "attempt-002", goal_file=changed)

    assert result.returncode != 0
    assert _status(fixture, "attempt-002")["classification"] == "goal_mismatch"


def test_preflight_rejects_an_exhausted_attempt_budget(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, max_attempts=1)
    assert _invoke(fixture, "attempt-001").returncode == 0

    result = _invoke(fixture, "attempt-002")

    assert result.returncode != 0
    assert _status(fixture, "attempt-002")["classification"] == "attempt_exhausted"


def test_preflight_rejects_escaping_allowed_paths(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, allowed_paths=["../outside"])

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "invalid_goal"


def test_preflight_requires_the_exact_clean_base(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (Path(fixture["repo"]) / "README.md").write_text("dirty\n")

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "git_dirty"


def test_preflight_rejects_shared_git_metadata(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    source = Path(fixture["repo"])
    linked = tmp_path / "linked"
    _run(["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)], source)
    fixture["repo"] = linked

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "shared_git_dir"


def test_preflight_uses_a_kernel_backed_single_writer_lease(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    lease = Path(fixture["state"]) / "lease.lock"
    lease.touch(mode=0o600)
    with lease.open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "lane_locked"


def test_preflight_rejects_state_nested_under_the_checkout(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    nested = Path(fixture["repo"]) / "lane-state"
    nested.mkdir(mode=0o700)
    fixture["state"] = nested

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert "invalid_path" in result.stderr
    assert not (nested / "runs" / "attempt-001").exists()


def test_preflight_builds_an_isolated_gemini_runtime(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    log = tmp_path / "gemini-invocations.jsonl"

    result = _invoke(
        fixture,
        "attempt-001",
        extra_env={
            "SANDBOX_FLAGS": "--privileged",
            "SANDBOX_MOUNTS": "/:/host:rw",
        },
    )

    assert result.returncode == 0, result.stderr
    run_dir = Path(fixture["state"]) / "runs" / "attempt-001"
    assert (run_dir / "gemini-version.stdout").read_text().strip() == "0.51.0"
    assert "--admin-policy" in (run_dir / "gemini-help.stdout").read_text()
    settings_path = Path(fixture["state"]) / "gemini-home" / "system-settings.json"
    settings = json.loads(settings_path.read_text())
    assert settings["admin"]["extensions"]["enabled"] is False
    assert settings["security"]["disableYoloMode"] is True
    assert settings["security"]["environmentVariableRedaction"]["enabled"] is True
    assert settings["advanced"]["ignoreLocalEnv"] is True
    assert settings["tools"]["sandbox"]["command"] == "docker"
    assert (settings_path.parent / "control-policy.toml").read_text() == Path(fixture["policy"]).read_text()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records
    assert all(record["gemini_home"] == str(Path(fixture["state"]) / "gemini-home") for record in records)
    assert all(record["tmpdir"] == str(Path(fixture["state"]) / "tmp") for record in records)
    assert all(record["sandbox"] == "docker" for record in records)
    assert all(record["sandbox_flags"] != "--privileged" for record in records)
    assert all(record["sandbox_mounts"] is None for record in records)


def test_preflight_rejects_a_missing_required_gemini_flag(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    gemini = Path(fixture["gemini"])
    gemini.write_text(gemini.read_text().replace(" --admin-policy", ""))

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "capability_mismatch"


def test_preflight_rejects_an_unavailable_sandbox_provider(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    result = _invoke(fixture, "attempt-001", provider="runsc")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "sandbox_unavailable"


def test_preflight_rejects_a_provider_with_an_inaccessible_daemon(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    provider = Path(fixture["path"]) / "docker"
    provider.write_text("#!/bin/sh\necho 'daemon denied' >&2\nexit 1\n")
    provider.chmod(0o755)

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "sandbox_unavailable"
    run_dir = Path(fixture["state"]) / "runs" / "attempt-001"
    assert "daemon denied" in (run_dir / "sandbox-provider.stderr").read_text()


def test_preflight_rejects_a_policy_without_default_deny(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _write_private(
        Path(fixture["policy"]),
        '[[rule]]\ntoolName = "read_file"\ndecision = "allow"\npriority = 100\n',
    )

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "invalid_policy"


def test_preflight_rejects_a_group_writable_policy(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    Path(fixture["policy"]).chmod(0o660)

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "invalid_policy"


def test_runner_keeps_prompt_content_out_of_process_arguments(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    result = _invoke(fixture, "attempt-001", preflight=False)

    assert result.returncode == 0, result.stderr
    assert Path(fixture["gemini"]).with_name("gemini-prompt.txt").read_text() == Path(fixture["prompt"]).read_text()
    invocations = [
        json.loads(line)
        for line in Path(fixture["gemini"]).with_name("gemini-invocations.jsonl").read_text().splitlines()
    ]
    argv = invocations[-1]["argv"]
    assert Path(fixture["prompt"]).read_text() not in argv
    assert argv[argv.index("--prompt") + 1].startswith("@")
    assert Path(argv[argv.index("--prompt") + 1][1:]).name.endswith(".prompt")
    assert argv[argv.index("--model") + 1] == "gemini-test-model"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--approval-mode") + 1] == "plan"
    assert "--sandbox" in argv
    assert argv[argv.index("--extensions") + 1] == "none"
    assert "--admin-policy" in argv
    status = _status(fixture, "attempt-001")
    assert status["classification"] == "succeeded"
    assert status["session_id"] == "session-test"
    assert status["resolved_model"] == "gemini-test-model"


def test_runner_rejects_invalid_or_undelivered_streams(tmp_path: Path) -> None:
    expected = {
        "malformed": "invalid_output",
        "empty": "no_output",
        "duplicate_result": "invalid_output",
        "error_status": "gemini_status_error",
        "model_mismatch": "model_mismatch",
        "fatal_event": "gemini_stream_error",
        "cli_error": "cli_error",
    }
    for index, (behavior, classification) in enumerate(expected.items(), start=1):
        case = tmp_path / behavior
        case.mkdir()
        fixture = _fixture(case)
        _set_behavior(fixture, behavior)

        result = _invoke(fixture, f"attempt-{index:03}", preflight=False)

        assert result.returncode != 0, behavior
        assert _status(fixture, f"attempt-{index:03}")["classification"] == classification


def test_runner_harvests_the_worker_group_on_timeout(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _set_behavior(fixture, "hang_child")

    result = _invoke(fixture, "attempt-001", preflight=False, timeout_seconds=1)

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "timed_out"
    child_path = Path(fixture["gemini"]).with_name("gemini-child.pid")
    _wait_for(child_path)
    assert not _pid_is_running(int(child_path.read_text()))


def test_runner_harvests_an_observed_detached_descendant(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _set_behavior(fixture, "detached_child")

    result = _invoke(fixture, "attempt-001", preflight=False, timeout_seconds=1)

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "timed_out"
    child_path = Path(fixture["gemini"]).with_name("gemini-child.pid")
    _wait_for(child_path)
    assert not _pid_is_running(int(child_path.read_text()))


def test_runner_rejects_changes_outside_the_goal_scope(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _set_behavior(fixture, "write_outside")

    result = _invoke(fixture, "attempt-001", preflight=False)

    assert result.returncode != 0
    status = _status(fixture, "attempt-001")
    assert status["classification"] == "scope_violation"
    assert status["changed_paths"] == ["outside.txt"]


def test_runner_accepts_in_scope_changes_after_immutable_verification(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _set_behavior(fixture, "write_allowed")

    result = _invoke(fixture, "attempt-001", preflight=False)

    assert result.returncode == 0, result.stderr
    status = _status(fixture, "attempt-001")
    assert status["classification"] == "succeeded"
    assert status["changed_paths"] == ["README.md"]
    assert status["verification"][0]["returncode"] == 0


def test_runner_rejects_failed_immutable_verification(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        verification_commands=[
            {"argv": [sys.executable, "-c", "raise SystemExit(9)"], "timeout_seconds": 5},
        ],
    )

    result = _invoke(fixture, "attempt-001", preflight=False)

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "verification_failed"


def test_runner_requires_two_part_authorization_for_paid_capable_auth(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, auth_type="gemini-api-key", allow_paid_generation=False)

    result = _invoke(fixture, "attempt-001", preflight=False)

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "billing_not_authorized"
    records = Path(fixture["gemini"]).with_name("gemini-invocations.jsonl").read_text().splitlines()
    assert len(records) == 2

    authorized_case = tmp_path / "authorized"
    authorized_case.mkdir()
    authorized = _fixture(authorized_case, auth_type="gemini-api-key", allow_paid_generation=True)
    result = _invoke(
        authorized,
        "attempt-001",
        preflight=False,
        extra_env={"ORCHESTRATE_GEMINI_PAID_GENERATION_ACK": "authorized"},
    )

    assert result.returncode == 0, result.stderr


def test_validation_fake_responses_never_claim_operational_success_or_require_billing_ack(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, auth_type="gemini-api-key", allow_paid_generation=False)
    fake_responses = tmp_path / "responses.json"
    _write_private(fake_responses, '[{"response":"fixture"}]\n')

    result = _invoke(
        fixture,
        "attempt-001",
        preflight=False,
        validation_fake_responses=fake_responses,
    )

    assert result.returncode == 0, result.stderr
    status = _status(fixture, "attempt-001")
    assert status["classification"] == "validation_succeeded"
    assert status["validation_mode"] is True
    command = json.loads((Path(fixture["state"]) / "runs" / "attempt-001" / "command.json").read_text())
    assert command["validation_mode"] is True
    assert "--fake-responses" in command["argv"]


def test_preflight_accepts_an_owner_controlled_gemini_launcher_symlink(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    executable = Path(fixture["gemini"])
    target = executable.with_name("gemini-real")
    executable.rename(target)
    executable.symlink_to(target)

    result = _invoke(fixture, "attempt-001")

    assert result.returncode == 0, result.stderr
    assert _status(fixture, "attempt-001")["gemini_executable_resolved"] == str(target)


def test_group_write_is_allowed_only_for_a_user_private_primary_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    executable = tmp_path / "gemini"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o775)
    private_user = SimpleNamespace(pw_name="lane-user", pw_gid=os.getgid())
    monkeypatch.setattr(runner.pwd, "getpwuid", lambda _uid: private_user)
    monkeypatch.setattr(runner.pwd, "getpwall", lambda: [private_user])
    monkeypatch.setattr(runner.grp, "getgrgid", lambda _gid: SimpleNamespace(gr_mem=[]))

    assert runner.resolve_controlled_executable(executable, "Gemini", "failure") == executable

    shared_user = SimpleNamespace(pw_name="other-user", pw_gid=os.getgid())
    monkeypatch.setattr(runner.pwd, "getpwall", lambda: [private_user, shared_user])
    with pytest.raises(runner.PreflightError):
        runner.resolve_controlled_executable(executable, "Gemini", "failure")


def test_runner_resumes_only_the_exact_prior_session(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    assert _invoke(fixture, "attempt-001", preflight=False).returncode == 0
    prior_run = Path(fixture["state"]) / "runs" / "attempt-001"

    result = _invoke(fixture, "attempt-002", preflight=False, resume_from=prior_run)

    assert result.returncode == 0, result.stderr
    command = json.loads((Path(fixture["state"]) / "runs" / "attempt-002" / "command.json").read_text())["argv"]
    assert command[command.index("--resume") + 1] == "session-test"
    assert "latest" not in command


def test_runner_observes_all_supported_git_change_states(tmp_path: Path) -> None:
    expected = {
        "stage_allowed": ["README.md"],
        "commit_allowed": ["README.md"],
        "delete_allowed": ["README.md"],
        "rename_allowed": ["README.md", "src/README.md"],
    }
    for behavior, changed in expected.items():
        case = tmp_path / behavior
        case.mkdir()
        fixture = _fixture(case)
        _set_behavior(fixture, behavior)

        result = _invoke(fixture, "attempt-001", preflight=False)

        assert result.returncode == 0, behavior
        assert _status(fixture, "attempt-001")["changed_paths"] == changed


def test_runner_harvests_after_sigterm_and_ignores_a_repeat_signal(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _set_behavior(fixture, "hang_child")
    command = _command(fixture, "attempt-001", preflight=False, timeout_seconds=20)
    env = os.environ | {"PATH": f"{fixture['path']}:{os.environ['PATH']}"}
    wrapper = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child_path = Path(fixture["gemini"]).with_name("gemini-child.pid")
    try:
        _wait_for(child_path)
        wrapper.send_signal(signal.SIGTERM)
        time.sleep(0.05)
        wrapper.send_signal(signal.SIGTERM)
        wrapper.communicate(timeout=8)
        assert wrapper.returncode != 0
        assert _status(fixture, "attempt-001")["classification"] == "interrupted"
        assert not _pid_is_running(int(child_path.read_text()))
    finally:
        if wrapper.poll() is None:
            wrapper.kill()


def test_process_identity_rejects_a_reused_pid() -> None:
    runner = _load_runner_module()
    tracked = {456: runner.ProcessIdentity("Mon Jan  1 00:00:01 2024")}
    reused = runner.ProcessInventory(
        {456: runner.ProcessInfo(1, 456, "S", "Tue Jan  2 00:00:01 2024")},
        True,
    )

    assert runner.tracked_descendant_state(123, tracked, reused) == ("absent", [])


def test_unknown_process_inventory_never_proves_absence() -> None:
    runner = _load_runner_module()
    tracked = {456: runner.ProcessIdentity("Mon Jan  1 00:00:01 2024")}

    assert runner.tracked_descendant_state(123, tracked, runner.ProcessInventory({}, False)) == ("unknown", [])
    assert runner.process_group_state(123, runner.ProcessInventory({}, False)) in {"absent", "unknown"}


def test_run_process_harvests_when_start_callback_raises(tmp_path: Path) -> None:
    runner = _load_runner_module()
    started: list[int] = []

    def fail_after_start(pid: int, _process_group_id: int) -> None:
        started.append(pid)
        raise RuntimeError("injected callback failure")

    try:
        runner.run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            tmp_path,
            dict(os.environ),
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
            5,
            on_start=fail_after_start,
        )
    except RuntimeError as error:
        assert str(error) == "injected callback failure"
    else:
        raise AssertionError("callback failure did not propagate")
    assert started and not _pid_is_running(started[0])
