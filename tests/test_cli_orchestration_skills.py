"""Regression tests for interactive CLI orchestration skills."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("agy", "codex", "claude", "gemini")
TMUX = shutil.which("tmux")
REQUIRE_TMUX = pytest.mark.skipif(TMUX is None, reason="tmux is not installed")


def _script(tool: str, name: str) -> Path:
    return REPO_ROOT / "skills" / f"orchestrate-{tool}" / "scripts" / name


def _extract_field(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}=(.+)$", text, re.MULTILINE)
    return match.group(1) if match is not None else "<missing>"


def _wait_for(path: Path, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {path}")


@contextmanager
def _isolated_tmux(tmp_path: Path) -> Iterator[dict[str, str]]:
    tmpdir = tmp_path / "run_tmp"
    tmpdir.mkdir(mode=0o700, exist_ok=True)
    bin_shim = tmp_path / "bin"
    bin_shim.mkdir(mode=0o700, exist_ok=True)
    tmux_bin = TMUX or "tmux"
    shim = bin_shim / "tmux"
    shim.write_text(f'#!/usr/bin/env bash\nexec {shlex.quote(tmux_bin)} -f /dev/null "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    with tempfile.TemporaryDirectory(
        prefix="cli-tm-", dir="/tmp" if Path("/tmp").is_dir() else None
    ) as short_socket_dir:
        os.chmod(short_socket_dir, 0o700)
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env["TMUX_TMPDIR"] = short_socket_dir
        env["TMPDIR"] = str(tmpdir)
        env["PATH"] = f"{bin_shim}{os.pathsep}{env.get('PATH', '')}"
        try:
            yield env
        finally:
            subprocess.run([tmux_bin, "-f", "/dev/null", "kill-server"], env=env, check=False, capture_output=True)


def _wait_for_pane_text(session: str, env: dict[str, str], expected: str, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [TMUX or "tmux", "-f", "/dev/null", "capture-pane", "-p", "-J", "-t", f"{session}:0.0", "-S", "-"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and expected in result.stdout:
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {expected!r} in {session}")


def _write_fake_cli(tmp_path: Path) -> Path:
    fake_cli = tmp_path / "fake-cli.py"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys, time\n"
        "args = sys.argv[2:]\n"
        "expanded = pathlib.Path(args[-1][1:]).read_text() if args[-1].startswith('@') else args[-1]\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'argv': args, 'expanded': expanded}))\n"
        "time.sleep(3)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    return fake_cli


def _run_launch_probe(tmp_path: Path, tool: str, prompt_text: str) -> tuple[str, dict[str, object]]:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(prompt_text, encoding="utf-8")
    prompt.chmod(0o600)
    output = tmp_path / "argv.json"
    fake_cli = _write_fake_cli(tmp_path)
    session = f"test-{tool}-{uuid.uuid4().hex[:10]}"
    with _isolated_tmux(tmp_path) as env:
        result = subprocess.run(
            [
                str(_script(tool, "launch_tmux.sh")),
                session,
                str(tmp_path),
                str(prompt),
                str(fake_cli),
                str(output),
                "--model",
                "test-model",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        dispatch_id = _extract_field(result.stdout, "dispatch_id")
        _wait_for(output)
        return dispatch_id, json.loads(output.read_text(encoding="utf-8"))


def _run_existing_session_launch(tmp_path: Path, tool: str) -> subprocess.CompletedProcess[str]:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("bounded task\n", encoding="utf-8")
    prompt.chmod(0o600)
    session = f"test-existing-{tool}-{uuid.uuid4().hex[:8]}"
    with _isolated_tmux(tmp_path) as env:
        subprocess.run(
            [TMUX or "tmux", "-f", "/dev/null", "new-session", "-d", "-s", session, "sleep 10"],
            env=env,
            check=True,
        )
        return subprocess.run(
            [str(_script(tool, "launch_tmux.sh")), session, str(tmp_path), str(prompt), "/bin/echo"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


def _run_gemini_stat_probe(tmp_path: Path, surface: str) -> tuple[subprocess.CompletedProcess[str], str]:
    directive = tmp_path / "directive.txt"
    directive.write_text("bounded task\n", encoding="utf-8")
    directive.chmod(0o600)
    fake_bin = tmp_path / "stat_bin"
    fake_bin.mkdir()
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == -c ]]; then printf '600\\n'; exit 0; fi\n"
        "if [[ $1 == -f ]]; then printf 'gnu filesystem status\\n%.0s' {1..6}; exit 1; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text('#!/usr/bin/env bash\n[[ $1 == has-session ]] && exit "$FAKE_HAS_SESSION_EXIT"\nexit 99\n')
    fake_tmux.chmod(0o755)
    if surface == "launch_tmux.sh":
        script_args = ["existing-gemini-session", str(tmp_path), str(directive), "/bin/echo"]
        has_session_exit = "0"
        expected = "session already exists"
    else:
        script_args = ["missing-gemini-session", str(directive)]
        has_session_exit = "1"
        expected = "session does not exist"
    result = subprocess.run(
        [str(_script("gemini", surface)), *script_args],
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "FAKE_HAS_SESSION_EXIT": has_session_exit,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
        check=False,
    )
    return result, expected


def _run_retry_enter_probe(tmp_path: Path, tool: str) -> tuple[subprocess.CompletedProcess[str], str]:
    fake_tui = tmp_path / "fake-tui.py"
    fake_tui.write_text(
        "#!/usr/bin/env python3\n"
        "import re, sys, time\n"
        "print('• Working stale', flush=True)\n"
        "print('› ', end='', flush=True)\n"
        "directive = sys.stdin.readline().rstrip('\\n')\n"
        "dispatch_id = re.search(r'\\[dispatch-end:([^]]+)\\]', directive).group(1)\n"
        "marker = f'[dispatch-end:{dispatch_id}]'\n"
        "print(f'› [Pasted Content] {marker}', flush=True)\n"
        "sys.stdin.readline()\n"
        "print(directive, flush=True)\n"
        "print(f'[dispatch-accepted:{dispatch_id}]', flush=True)\n"
        "print('• Working fresh', flush=True)\n"
        "print('›', flush=True)\n"
        "time.sleep(3)\n",
        encoding="utf-8",
    )
    fake_tui.chmod(0o755)
    followup = tmp_path / "followup.txt"
    followup.write_text("Do the next bounded task.\n", encoding="utf-8")
    followup.chmod(0o600)
    session = f"test-enter-{tool}-{uuid.uuid4().hex[:8]}"
    dispatch_id = f"{tool}-double-{uuid.uuid4().hex[:8]}"
    with _isolated_tmux(tmp_path) as base_env:
        subprocess.run(
            [
                TMUX or "tmux",
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-x",
                "80",
                "-y",
                "24",
                "-s",
                session,
                "-c",
                str(tmp_path),
                f"exec {shlex.quote(str(fake_tui))}",
            ],
            env=base_env,
            check=True,
        )
        _wait_for_pane_text(session, base_env, "• Working stale")
        env = base_env | {
            "ORCHESTRATION_DISPATCH_ID": dispatch_id,
            "ORCHESTRATION_VERIFY_ATTEMPTS": "3",
            "ORCHESTRATION_VERIFY_DELAY_SECONDS": "0.05",
        }
        result = subprocess.run(
            [str(_script(tool, "submit_followup.sh")), session, str(followup)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return result, dispatch_id


def _run_accepted_active_probe(tmp_path: Path, tool: str) -> tuple[subprocess.CompletedProcess[str], str]:
    outcome = tmp_path / "outcome.txt"
    fake_tui = tmp_path / "accepted-active-tui.py"
    fake_tui.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, re, select, sys, time\n"
        "outcome = pathlib.Path(sys.argv[1])\n"
        "print('› ', end='', flush=True)\n"
        "directive = sys.stdin.readline().rstrip('\\n')\n"
        "dispatch_id = re.search(r'\\[dispatch-end:([^]]+)\\]', directive).group(1)\n"
        "print(directive, flush=True)\n"
        "print(f'[dispatch-accepted:{dispatch_id}]', flush=True)\n"
        "print('• Working fresh', flush=True)\n"
        "ready, _, _ = select.select([sys.stdin], [], [], 1.5)\n"
        "outcome.write_text('second-enter' if ready else 'no-second-enter')\n"
        "time.sleep(1)\n",
        encoding="utf-8",
    )
    fake_tui.chmod(0o755)
    followup = tmp_path / "followup.txt"
    followup.write_text("Continue the accepted task.\n", encoding="utf-8")
    followup.chmod(0o600)
    session = f"test-no-second-{tool}-{uuid.uuid4().hex[:8]}"
    dispatch_id = f"{tool}-accepted-{uuid.uuid4().hex[:8]}"
    with _isolated_tmux(tmp_path) as base_env:
        subprocess.run(
            [
                TMUX or "tmux",
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-x",
                "80",
                "-y",
                "24",
                "-s",
                session,
                "-c",
                str(tmp_path),
                f"exec {shlex.quote(str(fake_tui))} {shlex.quote(str(outcome))}",
            ],
            env=base_env,
            check=True,
        )
        _wait_for_pane_text(session, base_env, "›")
        env = base_env | {
            "ORCHESTRATION_DISPATCH_ID": dispatch_id,
            "ORCHESTRATION_VERIFY_ATTEMPTS": "4",
            "ORCHESTRATION_VERIFY_DELAY_SECONDS": "0.05",
        }
        result = subprocess.run(
            [str(_script(tool, "submit_followup.sh")), session, str(followup)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        _wait_for(outcome)
        return result, outcome.read_text(encoding="utf-8")


@REQUIRE_TMUX
@pytest.mark.parametrize("tool", TOOLS)
def test_launcher_preserves_atomic_shell_sensitive_multiline_prompt(tmp_path: Path, tool: str) -> None:
    # Arrange
    prompt_text = 'First $HOME $(not-executed) "quotes"\nSecond `backticks`; apostrophe\'s value\n'

    # Act
    dispatch_id, record = _run_launch_probe(tmp_path, tool, prompt_text)

    # Assert
    assert record["expanded"] == (
        f"[dispatch:{dispatch_id}]\n"
        f"Acknowledge this exact turn first by emitting [dispatch-accepted:{dispatch_id}].\n"
        f"{prompt_text.rstrip()}\n[dispatch-end:{dispatch_id}]"
    )


@REQUIRE_TMUX
def test_agy_launcher_omits_effort_flag(tmp_path: Path) -> None:
    # Arrange
    prompt_text = "Bounded agy task.\n"

    # Act
    _, record = _run_launch_probe(tmp_path, "agy", prompt_text)

    # Assert
    assert "--effort" not in cast(list[str], record["argv"])


@REQUIRE_TMUX
def test_gemini_launcher_passes_at_file_argument(tmp_path: Path) -> None:
    # Arrange
    prompt_text = "Bounded gemini task.\n"

    # Act
    _, record = _run_launch_probe(tmp_path, "gemini", prompt_text)

    # Assert
    assert cast(list[str], record["argv"])[-1].startswith("@")


@REQUIRE_TMUX
@pytest.mark.parametrize("tool", TOOLS)
def test_launcher_rejects_an_existing_session_with_exit_2(tmp_path: Path, tool: str) -> None:
    # Arrange
    probe_tool = tool

    # Act
    result = _run_existing_session_launch(tmp_path, probe_tool)

    # Assert
    assert result.returncode == 2


@REQUIRE_TMUX
@pytest.mark.parametrize("tool", TOOLS)
def test_launcher_existing_session_reports_reason_on_stderr(tmp_path: Path, tool: str) -> None:
    # Arrange
    probe_tool = tool

    # Act
    result = _run_existing_session_launch(tmp_path, probe_tool)

    # Assert
    assert "session already exists" in result.stderr


@pytest.mark.parametrize("surface", ("launch_tmux.sh", "submit_followup.sh"))
def test_gemini_owner_only_checks_prefer_gnu_stat_exit_code(tmp_path: Path, surface: str) -> None:
    # Arrange
    target_surface = surface

    # Act
    result, _ = _run_gemini_stat_probe(tmp_path, target_surface)

    # Assert
    assert result.returncode == 2


@pytest.mark.parametrize("surface", ("launch_tmux.sh", "submit_followup.sh"))
def test_gemini_owner_only_checks_proceed_past_gnu_stat(tmp_path: Path, surface: str) -> None:
    # Arrange
    target_surface = surface

    # Act
    result, expected = _run_gemini_stat_probe(tmp_path, target_surface)

    # Assert
    assert expected in result.stderr


@REQUIRE_TMUX
@pytest.mark.parametrize("tool", TOOLS)
def test_followup_retries_enter_twice_when_first_enter_holds_paste(tmp_path: Path, tool: str) -> None:
    # Arrange
    probe_tool = tool

    # Act
    result, _ = _run_retry_enter_probe(tmp_path, probe_tool)

    # Assert
    assert "enter_presses=2" in result.stdout


@REQUIRE_TMUX
@pytest.mark.parametrize("tool", TOOLS)
def test_followup_retry_records_end_marker_in_verify_second_capture(tmp_path: Path, tool: str) -> None:
    # Arrange
    probe_tool = tool

    # Act
    result, dispatch_id = _run_retry_enter_probe(tmp_path, probe_tool)
    evidence_dir = Path(_extract_field(result.stdout, "evidence_dir"))
    verify_file = evidence_dir / "verify-second.out"
    verify_output = verify_file.read_text(encoding="utf-8") if verify_file.is_file() else ""
    post_file = Path(_extract_field(verify_output, "post_capture"))
    post_text = post_file.read_text(encoding="utf-8") if post_file.is_file() else ""

    # Assert
    assert f"[dispatch-end:{dispatch_id}]" in post_text


@REQUIRE_TMUX
@pytest.mark.parametrize("tool", TOOLS)
def test_accepted_active_followup_exits_nonzero(tmp_path: Path, tool: str) -> None:
    # Arrange
    probe_tool = tool

    # Act
    result, _ = _run_accepted_active_probe(tmp_path, probe_tool)

    # Assert
    assert result.returncode == 1


@REQUIRE_TMUX
@pytest.mark.parametrize("tool", TOOLS)
def test_accepted_active_followup_without_clean_composer_never_gets_second_enter(tmp_path: Path, tool: str) -> None:
    # Arrange
    probe_tool = tool

    # Act
    _, observed = _run_accepted_active_probe(tmp_path, probe_tool)

    # Assert
    assert observed == "no-second-enter"


@pytest.mark.parametrize("tool", TOOLS)
def test_stale_working_before_dispatch_never_verifies(tmp_path: Path, tool: str) -> None:
    # Arrange
    dispatch_id = f"{tool}-stale-history-test"
    pre = tmp_path / "pre.txt"
    pre.write_text("• Working stale\n›\n", encoding="utf-8")
    post = tmp_path / "post.txt"
    post.write_text(f"• Working stale\n[dispatch:{dispatch_id}]\nbounded task\n[dispatch-end:{dispatch_id}]\n›\n")
    command = [str(_script(tool, "verify_dispatch.sh")), "-", dispatch_id, str(pre), str(post)]

    # Act
    stale = subprocess.run(command, capture_output=True, text=True, check=False)

    # Assert
    assert stale.returncode == 1


@pytest.mark.parametrize("tool", TOOLS)
def test_fresh_acceptance_after_stale_history_verifies(tmp_path: Path, tool: str) -> None:
    # Arrange
    dispatch_id = f"{tool}-fresh-history-test"
    pre = tmp_path / "pre.txt"
    pre.write_text("• Working stale\n›\n", encoding="utf-8")
    post = tmp_path / "post.txt"
    post.write_text(
        f"• Working stale\n[dispatch:{dispatch_id}]\nbounded task\n[dispatch-end:{dispatch_id}]\n"
        f"[dispatch-accepted:{dispatch_id}]\n• Working fresh\n›\n",
        encoding="utf-8",
    )
    command = [str(_script(tool, "verify_dispatch.sh")), "-", dispatch_id, str(pre), str(post)]

    # Act
    fresh = subprocess.run(command, capture_output=True, text=True, check=False)

    # Assert
    assert f"verified_dispatch={dispatch_id}" in fresh.stdout


@pytest.mark.parametrize("tool", TOOLS)
def test_missing_start_marker_never_verifies(tmp_path: Path, tool: str) -> None:
    # Arrange
    dispatch_id = f"{tool}-missing-start-test"
    pre = tmp_path / "pre.txt"
    pre.write_text("›\n", encoding="utf-8")
    post = tmp_path / "post.txt"
    post.write_text(
        f"[dispatch-end:{dispatch_id}]\n[dispatch-accepted:{dispatch_id}]\n• Working fresh\n›\n",
        encoding="utf-8",
    )
    command = [str(_script(tool, "verify_dispatch.sh")), "-", dispatch_id, str(pre), str(post)]

    # Act
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    # Assert
    assert result.returncode == 1


@pytest.mark.parametrize("tool", TOOLS)
def test_marker_held_in_composer_with_unrelated_activity_never_verifies(tmp_path: Path, tool: str) -> None:
    # Arrange
    dispatch_id = f"{tool}-held-composer-test"
    pre = tmp_path / "pre.txt"
    pre.write_text("• Working stale\n›\n", encoding="utf-8")
    post = tmp_path / "post.txt"
    post.write_text(
        f"› [dispatch:{dispatch_id}] bounded task [dispatch-end:{dispatch_id}]\n• Working unrelated-new-task\n›\n",
        encoding="utf-8",
    )
    command = [str(_script(tool, "verify_dispatch.sh")), "-", dispatch_id, str(pre), str(post)]

    # Act
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    # Assert
    assert result.returncode == 1


def test_gemini_submit_followup_rejects_world_readable_followup_file(tmp_path: Path) -> None:
    # Arrange
    followup = tmp_path / "followup.txt"
    followup.write_text("bounded task\n", encoding="utf-8")
    followup.chmod(0o644)
    command = [str(_script("gemini", "submit_followup.sh")), "missing-gemini-session", str(followup)]

    # Act
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    # Assert
    assert "owner-only" in result.stderr
