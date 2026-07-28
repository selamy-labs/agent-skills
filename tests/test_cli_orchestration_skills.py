"""Regression tests for interactive CLI orchestration skills."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("agy", "codex", "claude")
TMUX = shutil.which("tmux")


def _script(tool: str, name: str) -> Path:
    return REPO_ROOT / "skills" / f"orchestrate-{tool}" / "scripts" / name


def _wait_for(path: Path, timeout: float = 4) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _kill_session(session: str) -> None:
    if TMUX:
        subprocess.run([TMUX, "kill-session", "-t", f"={session}"], check=False, capture_output=True)


def _wait_for_pane_text(session: str, expected: str, timeout: float = 4) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [TMUX, "capture-pane", "-p", "-J", "-t", f"{session}:0.0", "-S", "-"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and expected in result.stdout:
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {expected!r} in {session}")


@pytest.mark.skipif(TMUX is None, reason="tmux is not installed")
@pytest.mark.parametrize("tool", TOOLS)
def test_launcher_preserves_atomic_shell_sensitive_multiline_prompt(tmp_path: Path, tool: str) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text('First $HOME $(not-executed) "quotes"\nSecond `backticks`; apostrophe\'s value\n')
    output = tmp_path / "argv.json"
    fake_cli = tmp_path / "fake-cli.py"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
        "time.sleep(3)\n"
    )
    fake_cli.chmod(0o755)
    session = f"test-{tool}-{uuid.uuid4().hex[:10]}"
    try:
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
            check=True,
            capture_output=True,
            text=True,
        )
        dispatch_id = re.search(r"^dispatch_id=(.+)$", result.stdout, re.MULTILINE).group(1)
        _wait_for(output)
        argv = json.loads(output.read_text())
        if tool == "agy":
            assert "--effort" not in argv
        submitted = argv[-1]
        assert submitted == (
            f"[dispatch:{dispatch_id}]\n"
            f"Acknowledge this exact turn first by emitting [dispatch-accepted:{dispatch_id}].\n"
            f"{prompt.read_text().rstrip()}\n[dispatch-end:{dispatch_id}]"
        )
    finally:
        _kill_session(session)


@pytest.mark.skipif(TMUX is None, reason="tmux is not installed")
@pytest.mark.parametrize("tool", TOOLS)
def test_launcher_rejects_an_existing_session(tmp_path: Path, tool: str) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("bounded task\n")
    session = f"test-existing-{tool}-{uuid.uuid4().hex[:8]}"
    subprocess.run([TMUX, "new-session", "-d", "-s", session, "sleep 10"], check=True)
    try:
        result = subprocess.run(
            [str(_script(tool, "launch_tmux.sh")), session, str(tmp_path), str(prompt), "/bin/echo"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "session already exists" in result.stderr
    finally:
        _kill_session(session)


@pytest.mark.skipif(TMUX is None, reason="tmux is not installed")
@pytest.mark.parametrize("tool", TOOLS)
def test_followup_retries_enter_and_requires_fresh_post_marker_activity(tmp_path: Path, tool: str) -> None:
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
        "time.sleep(3)\n"
    )
    fake_tui.chmod(0o755)
    followup = tmp_path / "followup.txt"
    followup.write_text("Do the next bounded task.\n")
    session = f"test-enter-{tool}-{uuid.uuid4().hex[:8]}"
    command = f"exec {shlex.quote(str(fake_tui))}"
    subprocess.run(
        [TMUX, "new-session", "-d", "-x", "24", "-y", "12", "-s", session, "-c", str(tmp_path), command],
        check=True,
    )
    try:
        _wait_for_pane_text(session, "• Working stale")
        env = os.environ | {
            "ORCHESTRATION_DISPATCH_ID": f"{tool}-double-enter-test",
            "ORCHESTRATION_VERIFY_ATTEMPTS": "1",
            "ORCHESTRATION_VERIFY_DELAY_SECONDS": "0.05",
        }
        result = subprocess.run(
            [str(_script(tool, "submit_followup.sh")), session, str(followup)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=8,
        )
        assert "enter_presses=2" in result.stdout
        evidence_dir = Path(re.search(r"^evidence_dir=(.+)$", result.stdout, re.MULTILINE).group(1))
        verify_output = (evidence_dir / "verify-second.out").read_text()
        post_capture = Path(re.search(r"^post_capture=(.+)$", verify_output, re.MULTILINE).group(1))
        assert f"[dispatch-end:{tool}-double-enter-test]" in post_capture.read_text()
    finally:
        _kill_session(session)


@pytest.mark.parametrize("tool", TOOLS)
def test_stale_working_before_dispatch_never_verifies(tmp_path: Path, tool: str) -> None:
    dispatch_id = f"{tool}-stale-history-test"
    pre = tmp_path / "pre.txt"
    pre.write_text("• Working stale\n›\n")
    post = tmp_path / "post.txt"
    post.write_text(
        f"• Working stale\n[dispatch:{dispatch_id}]\nbounded task\n[dispatch-end:{dispatch_id}]\n›\n"
    )
    command = [str(_script(tool, "verify_dispatch.sh")), "-", dispatch_id, str(pre), str(post)]
    stale = subprocess.run(command, capture_output=True, text=True)
    assert stale.returncode == 1
    assert "lacks ordered start/end/acceptance/activity/clean-composer evidence" in stale.stderr

    post.write_text(
        post.read_text()
        + f"[dispatch-accepted:{dispatch_id}]\n"
        + "• Working fresh\n"
        + "›\n"
    )
    fresh = subprocess.run(command, check=True, capture_output=True, text=True)
    assert f"verified_dispatch={dispatch_id}" in fresh.stdout


@pytest.mark.parametrize("tool", TOOLS)
def test_missing_start_marker_never_verifies(tmp_path: Path, tool: str) -> None:
    dispatch_id = f"{tool}-missing-start-test"
    pre = tmp_path / "pre.txt"
    pre.write_text("›\n")
    post = tmp_path / "post.txt"
    post.write_text(
        f"[dispatch-end:{dispatch_id}]\n"
        f"[dispatch-accepted:{dispatch_id}]\n"
        "• Working fresh\n"
        "›\n"
    )
    command = [str(_script(tool, "verify_dispatch.sh")), "-", dispatch_id, str(pre), str(post)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1


@pytest.mark.parametrize("tool", TOOLS)
def test_marker_held_in_composer_with_unrelated_activity_never_verifies(tmp_path: Path, tool: str) -> None:
    dispatch_id = f"{tool}-held-composer-test"
    pre = tmp_path / "pre.txt"
    pre.write_text("• Working stale\n›\n")
    post = tmp_path / "post.txt"
    post.write_text(
        f"› [dispatch:{dispatch_id}] bounded task [dispatch-end:{dispatch_id}]\n"
        "• Working unrelated-new-task\n"
        "›\n"
    )
    command = [str(_script(tool, "verify_dispatch.sh")), "-", dispatch_id, str(pre), str(post)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
