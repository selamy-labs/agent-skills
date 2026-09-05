"""Regression tests for contained Gemini CLI headless orchestration."""

from __future__ import annotations

import fcntl
import hashlib
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
PROVIDER_GUARD = REPO_ROOT / "skills" / "orchestrate-gemini" / "scripts" / "provider_guard.py"


def _run(command: list[str], cwd: Path) -> str:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir()
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.name", "Test Operator"], path)
    _run(["git", "config", "user.email", "operator@example.com"], path)
    (path / "README.md").write_text("fixture\n")
    (path / ".gitignore").write_text("ignored.log\n")
    _run(["git", "add", "README.md", ".gitignore"], path)
    _run(["git", "commit", "-qm", "fixture"], path)
    (path / "src").mkdir()
    (path / "tests").mkdir()
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
        "from datetime import UTC, datetime\n"
        "def event(kind, **fields):\n"
        "    return {'type': kind, 'timestamp': datetime.now(UTC).isoformat(), **fields}\n"
        "record = {\n"
        "    'argv': sys.argv[1:],\n"
        "    'home': os.environ.get('HOME'),\n"
        "    'gemini_home': os.environ.get('GEMINI_CLI_HOME'),\n"
        "    'system_settings': os.environ.get('GEMINI_CLI_SYSTEM_SETTINGS_PATH'),\n"
        "    'tmpdir': os.environ.get('TMPDIR'),\n"
        "    'sandbox': os.environ.get('GEMINI_SANDBOX'),\n"
        "    'sandbox_flags': os.environ.get('SANDBOX_FLAGS'),\n"
        "    'sandbox_mounts': os.environ.get('SANDBOX_MOUNTS'),\n"
        "    'api_key': os.environ.get('GEMINI_API_KEY'),\n"
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
        "        print(json.dumps(event('init', session_id='session-test', model=model)), flush=True)\n"
        "        time.sleep(60)\n"
        "    resolved_model = 'wrong-model' if behavior == 'model_mismatch' else model\n"
        "    print(json.dumps(event('init', session_id='session-test', model=resolved_model)))\n"
        "    print(json.dumps(event('message', role='user', content=prompt)))\n"
        "    if behavior not in {'empty', 'fatal_event'}:\n"
        "        print(json.dumps(event('message', role='assistant', content='fixture complete')))\n"
        "    if behavior == 'fatal_event':\n"
        "        print(json.dumps(event('error', severity='error', message='fatal fixture error')))\n"
        "    status = 'error' if behavior == 'error_status' else 'success'\n"
        "    stats = {'total_tokens': 2, 'input_tokens': 1, 'output_tokens': 1, 'cached': 0, 'input': 1, "
        "'duration_ms': 1, 'tool_calls': 0, 'models': {model: {'total_tokens': 2, 'input_tokens': 1, "
        "'output_tokens': 1, 'cached': 0, 'input': 1}}}\n"
        "    print(json.dumps(event('result', status=status, stats=stats)))\n"
        "    if behavior == 'duplicate_result':\n"
        "        print(json.dumps(event('result', status='success', stats=stats)))\n"
        "    if behavior == 'write_outside':\n"
        "        pathlib.Path('outside.txt').write_text('escaped scope\\n')\n"
        "    if behavior == 'write_ignored':\n"
        "        pathlib.Path('ignored.log').write_text('escaped ignored scope\\n')\n"
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
        "        pathlib.Path('src').mkdir(exist_ok=True)\n"
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
    provider.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == 'version':\n"
        "    print('29.0.0')\n"
        "elif len(args) >= 2 and args[:2] == ['image', 'inspect']:\n"
        "    print(args[-1])\n"
        "elif args and args[0] == 'run':\n"
        "    image_index = next(i for i, value in enumerate(args) if value.startswith('example.invalid/'))\n"
        "    workdir = args[args.index('--workdir') + 1]\n"
        "    result = subprocess.run(args[image_index + 1:], cwd=workdir, env=os.environ)\n"
        "    raise SystemExit(result.returncode)\n"
        "elif args and args[0] == 'inspect':\n"
        "    raise SystemExit(1)\n"
    )
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
        "auth_type": "gemini-api-key",
        "allow_paid_generation": True,
        "sandbox_image": "example.invalid/gemini-sandbox@sha256:" + "a" * 64,
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
    credential = tmp_path / "credential.env"
    _write_private(credential, "GEMINI_API_KEY=test-fixture-secret\n")
    return {
        "repo": repo,
        "base_sha": base_sha,
        "state": state,
        "goal": goal_file,
        "prompt": prompt,
        "credential": credential,
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
        "--credential-env-file",
        str(fixture["credential"]),
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
    env = (
        os.environ
        | {
            "PATH": f"{fixture['path']}:{os.environ['PATH']}",
            "ORCHESTRATE_GEMINI_PAID_GENERATION_ACK": "authorized",
        }
        | (extra_env or {})
    )
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


def _process_command_contains(marker: str) -> bool:
    result = subprocess.run(
        ["ps", "-axo", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and marker in result.stdout


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


def _load_provider_guard_module():
    spec = importlib.util.spec_from_file_location("test_orchestrate_gemini_provider_guard", PROVIDER_GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_provider_guard_rewrites_upstream_runtime_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _load_provider_guard_module()
    workspace = tmp_path / "checkout"
    state = tmp_path / "state"
    allowed = workspace / "src"
    settings = state / "gemini-home" / ".gemini"
    allowed.mkdir(parents=True)
    settings.mkdir(parents=True)
    image = "example.invalid/gemini-sandbox@sha256:" + "a" * 64
    monkeypatch.setenv("ORCHESTRATE_GEMINI_PROVIDER_REAL", "/usr/bin/false")
    monkeypatch.setenv("ORCHESTRATE_GEMINI_WORKSPACE", str(workspace))
    monkeypatch.setenv("ORCHESTRATE_GEMINI_STATE_DIR", str(state))
    monkeypatch.setenv("ORCHESTRATE_GEMINI_SANDBOX_IMAGE", image)
    monkeypatch.setenv("ORCHESTRATE_GEMINI_CONTAINER_LABEL", "io.selamy.orchestrate-gemini.lane=test")
    monkeypatch.setenv("ORCHESTRATE_GEMINI_ALLOWED_PATHS", json.dumps([str(allowed)]))
    monkeypatch.setenv("ORCHESTRATE_GEMINI_LIVE_MODE", "1")
    credential = state / "runtime-secret.env"
    _write_private(credential, "GEMINI_API_KEY=actual-secret\n")
    monkeypatch.setenv("ORCHESTRATE_GEMINI_CREDENTIAL_ENV_FILE", str(credential))

    rewritten = guard.guarded_run(
        [
            "run",
            "--network",
            "gemini-cli-sandbox",
            "--add-host",
            "host.docker.internal:host-gateway",
            "--volume",
            f"{workspace}:{workspace}",
            "--volume",
            f"{settings}:{settings}",
            image,
            "gemini",
        ]
    )

    assert "host.docker.internal:host-gateway" not in rewritten
    assert f"{workspace}:{workspace}:ro" in rewritten
    assert f"{settings}:{settings}" in rewritten
    assert f"{allowed}:{allowed}:rw" in rewritten
    assert "io.selamy.orchestrate-gemini.lane=test" in rewritten

    with pytest.raises(SystemExit):
        guard.guarded_run(["run", "--network", "gemini-cli-sandbox", "--env", "GEMINI_API_KEY=secret", image, "gemini"])
    secret_safe = guard.guarded_run(
        [
            "run",
            "--network",
            "gemini-cli-sandbox",
            "--env",
            "GEMINI_API_KEY=__ORCHESTRATE_GEMINI_RUNTIME_SECRET__",
            image,
            "gemini",
        ]
    )
    assert "GEMINI_API_KEY=actual-secret" not in secret_safe
    assert secret_safe[secret_safe.index("--env-file") + 1] == str(credential)


def test_provider_guard_binds_proxy_readiness_port_to_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _load_provider_guard_module()
    workspace = tmp_path / "checkout"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    image = "example.invalid/gemini-sandbox@sha256:" + "a" * 64
    values = {
        "ORCHESTRATE_GEMINI_PROVIDER_REAL": "/usr/bin/false",
        "ORCHESTRATE_GEMINI_WORKSPACE": str(workspace),
        "ORCHESTRATE_GEMINI_STATE_DIR": str(state),
        "ORCHESTRATE_GEMINI_SANDBOX_IMAGE": image,
        "ORCHESTRATE_GEMINI_CONTAINER_LABEL": "io.selamy.orchestrate-gemini.lane=test",
        "ORCHESTRATE_GEMINI_ALLOWED_PATHS": "[]",
        "ORCHESTRATE_GEMINI_LIVE_MODE": "1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    rewritten = guard.guarded_run(
        [
            "run",
            "--name",
            "gemini-cli-sandbox-proxy",
            "--network",
            "gemini-cli-sandbox-proxy",
            "-p",
            "8877:8877",
            image,
            "node",
        ]
    )

    assert f"{guard.LOOPBACK}:8877:8877" in rewritten


def test_provider_guard_rejects_unexpected_networks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _load_provider_guard_module()
    workspace = tmp_path / "checkout"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    image = "example.invalid/gemini-sandbox@sha256:" + "a" * 64
    values = {
        "ORCHESTRATE_GEMINI_PROVIDER_REAL": "/usr/bin/false",
        "ORCHESTRATE_GEMINI_WORKSPACE": str(workspace),
        "ORCHESTRATE_GEMINI_STATE_DIR": str(state),
        "ORCHESTRATE_GEMINI_SANDBOX_IMAGE": image,
        "ORCHESTRATE_GEMINI_CONTAINER_LABEL": "io.selamy.orchestrate-gemini.lane=test",
        "ORCHESTRATE_GEMINI_ALLOWED_PATHS": "[]",
        "ORCHESTRATE_GEMINI_LIVE_MODE": "1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit):
        guard.guarded_run(["run", "--network", "host", image, "gemini"])
    with pytest.raises(SystemExit):
        guard.guarded_run(["run", "--name", "gemini-cli-sandbox-proxy", "--network", "host", image, "node"])
    with pytest.raises(SystemExit):
        guard.guard_network(Path("/usr/bin/false"), ["network", "create", "gemini-cli-sandbox"])

    monkeypatch.setenv("ORCHESTRATE_GEMINI_LIVE_MODE", "0")
    validation = guard.guarded_run(["run", "--network", "none", image, "gemini"])
    assert validation[validation.index("--network") + 1] == "none"
    with pytest.raises(SystemExit):
        guard.guarded_run(["run", "--network", "gemini-cli-sandbox", image, "gemini"])


def test_provider_guard_rejects_a_preexisting_external_main_network(
    tmp_path: Path,
) -> None:
    guard = _load_provider_guard_module()
    provider = tmp_path / "docker"
    provider.write_text("#!/bin/sh\nprintf 'false\\n'\n")
    provider.chmod(0o700)

    with pytest.raises(SystemExit):
        guard.guard_network(provider, ["network", "inspect", "gemini-cli-sandbox"])


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


def test_preflight_requires_an_owner_only_goal_source(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    Path(fixture["goal"]).chmod(0o644)

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "invalid_goal"


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


def test_preflight_never_makes_git_control_metadata_writable(tmp_path: Path) -> None:
    direct_case = tmp_path / "direct"
    direct_case.mkdir()
    direct = _fixture(direct_case, allowed_paths=[".git"])

    result = _invoke(direct, "attempt-001")

    assert result.returncode != 0
    assert _status(direct, "attempt-001")["classification"] == "invalid_goal"

    symlink_case = tmp_path / "symlink"
    symlink_case.mkdir()
    linked = _fixture(symlink_case)
    repo = Path(linked["repo"])
    (repo / "metadata-link").symlink_to(".git")
    _run(["git", "add", "metadata-link"], repo)
    _run(["git", "commit", "-qm", "tracked metadata link"], repo)
    goal = json.loads(Path(linked["goal"]).read_text())
    goal["base_sha"] = _run(["git", "rev-parse", "HEAD"], repo)
    goal["allowed_paths"] = ["metadata-link"]
    _write_private(Path(linked["goal"]), json.dumps(goal))

    result = _invoke(linked, "attempt-001")

    assert result.returncode != 0
    assert _status(linked, "attempt-001")["classification"] == "invalid_goal"


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
    settings_path = Path(fixture["state"]) / "gemini-home" / ".gemini" / "system-settings.json"
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
    assert all(record.get("home") == str(Path(fixture["state"]) / "gemini-home") for record in records)
    assert all(record["tmpdir"] == str(Path(fixture["state"]) / "tmp") for record in records)
    assert all(record["sandbox"] == "docker" for record in records)
    assert all(record["sandbox_flags"] != "--privileged" for record in records)
    assert all(record["sandbox_mounts"] is None for record in records)
    assert settings["admin"]["mcp"]["enabled"] is False
    assert settings["admin"]["skills"]["enabled"] is False
    assert settings["hooksConfig"]["enabled"] is False
    assert settings["billing"]["overageStrategy"] == "never"
    assert settings["tools"]["sandbox"]["networkAccess"] is True
    trusted = json.loads((settings_path.parent / "trustedFolders.json").read_text())
    assert trusted == {str(Path(fixture["repo"]).resolve()): "TRUST_FOLDER"}


def test_preflight_rejects_a_missing_required_gemini_flag(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    gemini = Path(fixture["gemini"])
    gemini.write_text(gemini.read_text().replace(" --admin-policy", ""))

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "capability_mismatch"


def test_preflight_requires_exact_supported_gemini_version(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    gemini = Path(fixture["gemini"])
    gemini.write_text(gemini.read_text().replace("print('0.51.0')", "print('0.52.0')"))

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


@pytest.mark.parametrize(
    "policy_text",
    [
        '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = true\n\n'
        '[[rule]]\ntoolName = "read_file"\ndecision = "allow"\npriority = 100\n',
        '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\n\n'
        '[[rule]]\ntoolName = "read_*"\ndecision = "allow"\npriority = 100\n',
        '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\n\n'
        '[[rule]]\nmcpName = "*"\ndecision = "allow"\npriority = 100\n',
        '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\nmodes = ["plan"]\n\n'
        '[[rule]]\ntoolName = "read_file"\ndecision = "allow"\npriority = 100\n',
        '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\n\n'
        '[[rule]]\ntoolName = "*"\ndecision = "allow"\npriority = 200\n',
    ],
)
def test_preflight_rejects_non_narrow_or_boolean_priority_policy(tmp_path: Path, policy_text: str) -> None:
    fixture = _fixture(tmp_path)
    _write_private(Path(fixture["policy"]), policy_text)

    result = _invoke(fixture, "attempt-001")

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "invalid_policy"


def test_runner_requires_the_prompt_source_to_be_owner_only(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    Path(fixture["prompt"]).chmod(0o644)

    result = _invoke(fixture, "attempt-001", preflight=False)

    assert result.returncode != 0
    assert _status(fixture, "attempt-001")["classification"] == "invalid_prompt"


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
    assert status["reported_model"] == "gemini-test-model"
    assert invocations[-1]["api_key"] == "__ORCHESTRATE_GEMINI_RUNTIME_SECRET__"
    run_dir = Path(fixture["state"]) / "runs" / "attempt-001"
    assert "test-fixture-secret" not in "".join(
        path.read_text(errors="replace") for path in run_dir.iterdir() if path.is_file()
    )


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


def test_runner_rejects_ignored_changes_outside_the_goal_scope(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _set_behavior(fixture, "write_ignored")

    result = _invoke(fixture, "attempt-001", preflight=False)

    assert result.returncode != 0
    status = _status(fixture, "attempt-001")
    assert status["classification"] == "scope_violation"
    assert status["changed_paths"] == ["ignored.log"]


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
    assert not (Path(authorized["state"]) / "runtime-secret.env").exists()

    unsupported_case = tmp_path / "unsupported"
    unsupported_case.mkdir()
    unsupported = _fixture(unsupported_case, auth_type="oauth-personal", allow_paid_generation=True)
    result = _invoke(unsupported, "attempt-001", preflight=False)
    assert result.returncode != 0
    assert _status(unsupported, "attempt-001")["classification"] == "unsupported_auth"


def test_live_auth_rejects_exported_or_non_private_api_keys(tmp_path: Path) -> None:
    exported_case = tmp_path / "exported"
    exported_case.mkdir()
    exported = _fixture(exported_case)

    result = _invoke(
        exported,
        "attempt-001",
        preflight=False,
        extra_env={"GEMINI_API_KEY": "must-not-enter-runtime-argv"},
    )

    assert result.returncode != 0
    assert _status(exported, "attempt-001")["classification"] == "unsafe_auth_environment"
    assert not (Path(exported["state"]) / "runtime-secret.env").exists()

    public_case = tmp_path / "public-file"
    public_case.mkdir()
    public = _fixture(public_case)
    Path(public["credential"]).chmod(0o644)

    result = _invoke(public, "attempt-001", preflight=False)

    assert result.returncode != 0
    assert _status(public, "attempt-001")["classification"] == "invalid_credential"


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
    assert status["validation_fixture_sha256"] == hashlib.sha256(fake_responses.read_bytes()).hexdigest()
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


def test_runner_rejects_resume_when_the_model_or_terminal_outcome_differs(tmp_path: Path) -> None:
    model_case = tmp_path / "model"
    model_case.mkdir()
    model_fixture = _fixture(model_case)
    assert _invoke(model_fixture, "attempt-001", preflight=False).returncode == 0
    model_prior = Path(model_fixture["state"]) / "runs" / "attempt-001"

    result = _invoke(
        model_fixture,
        "attempt-002",
        preflight=False,
        resume_from=model_prior,
        model="different-model",
    )

    assert result.returncode != 0
    assert _status(model_fixture, "attempt-002")["classification"] == "invalid_resume"

    failure_case = tmp_path / "failure"
    failure_case.mkdir()
    failure_fixture = _fixture(failure_case)
    _set_behavior(failure_fixture, "cli_error")
    assert _invoke(failure_fixture, "attempt-001", preflight=False).returncode != 0
    failed_prior = Path(failure_fixture["state"]) / "runs" / "attempt-001"
    _set_behavior(failure_fixture, "success")

    result = _invoke(failure_fixture, "attempt-002", preflight=False, resume_from=failed_prior)

    assert result.returncode != 0
    assert _status(failure_fixture, "attempt-002")["classification"] == "invalid_resume"


def test_validation_sessions_cannot_be_resumed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    responses = tmp_path / "responses.json"
    _write_private(responses, '[{"response":"fixture"}]\n')
    assert (
        _invoke(
            fixture,
            "attempt-001",
            preflight=False,
            validation_fake_responses=responses,
        ).returncode
        == 0
    )
    prior = Path(fixture["state"]) / "runs" / "attempt-001"

    result = _invoke(
        fixture,
        "attempt-002",
        preflight=False,
        resume_from=prior,
        validation_fake_responses=responses,
    )

    assert result.returncode != 0
    assert _status(fixture, "attempt-002")["classification"] == "invalid_resume"


def test_runner_resumes_an_in_scope_dirty_checkout_from_the_exact_prior_session(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _set_behavior(fixture, "write_allowed")
    assert _invoke(fixture, "attempt-001", preflight=False).returncode == 0
    _set_behavior(fixture, "success")
    prior_run = Path(fixture["state"]) / "runs" / "attempt-001"

    result = _invoke(fixture, "attempt-002", preflight=False, resume_from=prior_run)

    assert result.returncode == 0, result.stderr
    assert _status(fixture, "attempt-002")["changed_paths"] == ["README.md"]


def test_runner_observes_all_supported_git_change_states(tmp_path: Path) -> None:
    expected = {
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

    for behavior in ("stage_allowed", "commit_allowed"):
        case = tmp_path / behavior
        case.mkdir()
        fixture = _fixture(case)
        _set_behavior(fixture, behavior)

        result = _invoke(fixture, "attempt-001", preflight=False)

        assert result.returncode != 0
        assert _status(fixture, "attempt-001")["classification"] == "git_metadata_violation"


def test_runner_harvests_after_sigterm_and_ignores_a_repeat_signal(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _set_behavior(fixture, "hang_child")
    command = _command(fixture, "attempt-001", preflight=False, timeout_seconds=20)
    env = os.environ | {
        "PATH": f"{fixture['path']}:{os.environ['PATH']}",
        "ORCHESTRATE_GEMINI_PAID_GENERATION_ACK": "authorized",
    }
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


def test_restart_reconciles_a_stale_worker_identity(tmp_path: Path) -> None:
    runner = _load_runner_module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    worker = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        info = None
        while info is None and time.monotonic() < deadline:
            info = runner.process_snapshot().processes.get(worker.pid)
        assert info is not None
        runner.write_atomic_private(
            state / "lease.json",
            {
                "worker_pid": worker.pid,
                "process_group_id": worker.pid,
                "worker_started_at": info.started_at,
                "worker_state": "running",
            },
        )

        evidence = runner.recover_stale_worker(state)

        assert evidence == {"worker_pid": worker.pid, "action": "harvested"}
        worker.wait(timeout=3)
        assert json.loads((state / "lease.json").read_text())["worker_state"] == "absent"
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


def test_global_v051_execution_lease_is_nonblocking() -> None:
    runner = _load_runner_module()
    with runner.global_execution_lease(), pytest.raises(runner.PreflightError) as raised:
        with runner.global_execution_lease():
            pass
    assert raised.value.classification == "transport_locked"


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


def test_capability_probe_harvests_a_detached_descendant_on_timeout(tmp_path: Path) -> None:
    runner = _load_runner_module()
    child_path = tmp_path / "probe-child.pid"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', sys.argv[1]], "
        "start_new_session=True)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    probe.chmod(0o755)
    child_pid: int | None = None
    try:
        with pytest.raises(runner.PreflightError):
            runner.run_probe(
                probe,
                [str(child_path)],
                tmp_path,
                dict(os.environ),
                time.monotonic() + 1,
                tmp_path / "probe.stdout",
                tmp_path / "probe.stderr",
            )
        _wait_for(child_path)
        child_pid = int(child_path.read_text())
        assert not _process_command_contains(str(child_path))
    finally:
        if child_pid is not None and _pid_is_running(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_verification_harvests_an_observed_detached_descendant(tmp_path: Path) -> None:
    child_path = tmp_path / "verification-child.pid"
    verifier = tmp_path / "verifier.py"
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(0.3)\n"
        "raise SystemExit(9)\n"
    )
    verifier.chmod(0o755)
    fixture = _fixture(
        tmp_path,
        verification_commands=[{"argv": [str(verifier), str(child_path)], "timeout_seconds": 5}],
    )
    child_pid: int | None = None
    try:
        result = _invoke(fixture, "attempt-001", preflight=False)
        _wait_for(child_path)
        child_pid = int(child_path.read_text())
        assert result.returncode != 0
        assert _status(fixture, "attempt-001")["classification"] == "verification_failed"
        assert not _pid_is_running(child_pid)
    finally:
        if child_pid is not None and _pid_is_running(child_pid):
            os.kill(child_pid, signal.SIGKILL)
