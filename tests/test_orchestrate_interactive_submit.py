"""AAA, property, and isolated tmux PTY tests for interactive dispatch classify/submit."""

from __future__ import annotations

import hashlib
import json
import os
import pty
import random
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from tools.orchestrate_dispatch import (
    DEFAULT_COMPOSER_PATTERN,
    DeliveryState,
    check_pre_capture,
    classify_capture,
    main,
    markers_for_id,
    normalize_lines,
    segment_capture,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("agy", "claude", "codex", "gemini")
TMUX = shutil.which("tmux")
REQUIRE_TMUX = pytest.mark.skipif(TMUX is None, reason="tmux is not installed")
PTY_HARNESS = r"""#!/usr/bin/env python3
import json
import os
import re
import select
import sys
import termios
import time

scenario = sys.argv[1]
log_path = sys.argv[2]
fd = sys.stdin.fileno()
old_attrs = termios.tcgetattr(fd)
new_attrs = termios.tcgetattr(fd)
new_attrs[0] = new_attrs[0] & ~(termios.ICRNL | termios.IXON)
new_attrs[3] = new_attrs[3] & ~(termios.ICANON | termios.ECHO | termios.ISIG)
termios.tcsetattr(fd, termios.TCSANOW, new_attrs)

events = {"raw_hex": "", "enters": 0, "pastes": [], "rendered": []}


def flush_events() -> None:
    tmp = log_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(events, handle)
    os.replace(tmp, log_path)


def clear_and_print(lines: list[str]) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.write("\n".join(lines))
    sys.stdout.flush()


def wrap_box(text: str, width: int = 50) -> list[str]:
    clean = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    rows: list[str] = []
    prefix = "› "
    while clean:
        room = width - len(prefix)
        chunk = clean[:room]
        rows.append(f"{prefix}{chunk}".ljust(width + 2))
        clean = clean[room:]
        prefix = "  "
    return rows or ["› "]


def extract_id(payload: str) -> str:
    match = re.search(r"\[dispatch:([^]]+)\]", payload)
    return match.group(1) if match else "unknown"


def chip_box(payload: str) -> list[str]:
    did = extract_id(payload)
    return ["› ↑ 12 more lines", "  Do you want to proceed?", f"  [dispatch-end:{did}]"]


def render_after_paste(pasted: str) -> None:
    if scenario == "submitted_zero_enter":
        did = extract_id(pasted)
        events["rendered"].append("submitted")
        flush_events()
        clear_and_print(
            [
                "• Done",
                *wrap_box(pasted, 50),
                f"[dispatch-accepted:{did}]",
                "• Working",
                "› Ask Codex to do anything",
                "  GPT-6.1 · /workspace/app",
            ]
        )
        return
    events["rendered"].append("pasted")
    flush_events()
    body = chip_box(pasted) if scenario == "submitted_chip_redraw" else wrap_box(pasted, 50)
    clear_and_print(["• Done", "", *body, "  GPT-6.1 · /workspace/app"])


def render_post_enter(buf: str, enter_num: int) -> None:
    did = extract_id(buf)
    footer = "  GPT-6.1 · /workspace/app"
    prompt = "› Ask Codex to do anything"
    if scenario == "submitted_chip_redraw" and enter_num == 1:
        events["rendered"].append("unsubmitted")
        flush_events()
        clear_and_print(["• Done", "", *chip_box(buf), footer, "  ⚠ 1 warning"])
        return
    if scenario == "submitted_chip_redraw" and enter_num >= 2:
        events["rendered"].append("submitted")
        flush_events()
        clear_and_print(["• Done", *chip_box(buf), f"[dispatch-accepted:{did}]", "• Working", prompt, footer])
        return
    if scenario in ("submitted", "second_enter_blocked_modal") and enter_num == 1:
        events["rendered"].append("unsubmitted")
        flush_events()
        clear_and_print(["• Done", "", *wrap_box(buf, 50), footer, "  ⚠ 1 warning"])
        return
    if (scenario == "submitted" and enter_num >= 2) or scenario == "submitted_one_enter":
        events["rendered"].append("submitted")
        flush_events()
        clear_and_print(["• Done", *wrap_box(buf, 50), f"[dispatch-accepted:{did}]", "• Working", prompt, footer])
        return
    if scenario == "second_enter_blocked_modal" and enter_num >= 2:
        events["rendered"].append("blocked_modal")
        flush_events()
        clear_and_print(["Models & Quota", "  1. Model A", "  esc Close", "› "])
        return
    if scenario == "accepted_active":
        events["rendered"].append("accepted_active")
        flush_events()
        clear_and_print(["• Done", *wrap_box(buf, 50), f"[dispatch-accepted:{did}]", "▸ Working", "  esc to interrupt"])
        return
    if scenario == "tab_to_queue":
        events["rendered"].append("tab_to_queue")
        flush_events()
        clear_and_print(["• Working", *wrap_box(buf, 50), "  tab to queue · esc to interrupt"])
        return
    if scenario == "queued":
        events["rendered"].append("queued")
        flush_events()
        clear_and_print(["• Working stale", "  Queued messages:", *wrap_box(buf, 50), prompt, "  esc to interrupt"])
        return
    if scenario == "partial_paste":
        events["rendered"].append("partial_paste")
        flush_events()
        clear_and_print(
            [f"▸ Thought [dispatch:{did}] partial", "• Working", "────", f"› ↑ 12 more lines [dispatch-end:{did}]"]
        )
        return
    if scenario == "ambiguous":
        events["rendered"].append("ambiguous")
        flush_events()
        clear_and_print(["• Working", prompt, footer])


def _partial_end_suffix(buf: str) -> int:
    for size in range(min(5, len(buf)), 0, -1):
        if "\x1b[201~".startswith(buf[-size:]):
            return size
    return 0


def handle_chunk(chunk: bytes, state: dict[str, object]) -> None:
    events["raw_hex"] += chunk.hex()
    buf = str(state["buf"]) + chunk.decode("utf-8", errors="replace")
    while buf:
        if state["in_paste"]:
            end_idx = buf.find("\x1b[201~")
            if end_idx == -1:
                keep = _partial_end_suffix(buf)
                state["paste_buf"] = str(state["paste_buf"]) + buf[: len(buf) - keep]
                buf = buf[len(buf) - keep :]
                break
            pasted = str(state["paste_buf"]) + buf[:end_idx]
            events["pastes"].append(pasted)
            state["in_paste"] = False
            state["paste_buf"] = ""
            state["clean_paste"] = pasted
            buf = buf[end_idx + len("\x1b[201~") :]
            render_after_paste(pasted)
            continue
        start_idx = buf.find("\x1b[200~")
        enters = [idx for idx in (buf.find("\r"), buf.find("\n")) if idx != -1]
        first_enter = min(enters) if enters else -1
        if start_idx != -1 and (first_enter == -1 or start_idx < first_enter):
            buf = buf[start_idx + len("\x1b[200~") :]
            state["in_paste"] = True
            state["paste_buf"] = ""
            continue
        if first_enter != -1:
            buf = buf[first_enter + 1 :]
            events["enters"] = int(events["enters"]) + 1
            render_post_enter(str(state["clean_paste"]), int(events["enters"]))
            continue
        break
    state["buf"] = buf
    flush_events()


try:
    sys.stdout.write("\x1b[?2004h")
    if scenario == "blocked_modal":
        clear_and_print(["Models & Quota", "  1. Model A", "  esc Close"])
    else:
        clear_and_print(["• Done", "› Ask Codex to do anything", "  GPT-6.1 · /workspace/app"])
    flush_events()
    state: dict[str, object] = {"buf": "", "in_paste": False, "paste_buf": "", "clean_paste": ""}
    deadline = time.monotonic() + 35.0
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            data = os.read(fd, 4096)
            if not data:
                break
            handle_chunk(data, state)
finally:
    sys.stdout.write("\x1b[?2004l")
    sys.stdout.flush()
    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
    flush_events()
"""


def _wrap_logical_lines(
    logical_lines: list[str],
    width: int,
    left_bar: str,
    right_pad: str,
) -> list[str]:
    physical: list[str] = []
    for line in logical_lines:
        if not line:
            physical.append("")
            continue
        remaining = line
        while remaining:
            chunk = remaining[:width]
            physical.append(f"{left_bar}{chunk}{right_pad}")
            remaining = remaining[width:]
    return physical


def _kv_get(stdout: str, key: str) -> str:
    prefix = f"{key}="
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


@dataclass(frozen=True)
class LiveScenarioOutcome:
    returncode: int
    stdout: str
    stderr: str
    raw_bytes: bytes
    enters: int
    pastes: list[str]
    rendered: list[str]


def _wait_for_log_text(session: str, env: dict[str, str], log_path: Path, expected: str) -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if log_path.exists():
            capture = subprocess.run(
                [TMUX or "tmux", "-f", "/dev/null", "capture-pane", "-p", "-J", "-t", session, "-S", "-"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if expected in capture.stdout:
                return
        time.sleep(0.03)
    raise TimeoutError(f"timed out waiting for {expected!r} in isolated tmux session {session}")


def _wait_for_events_json(log_path: Path, min_rendered: int = 1) -> dict[str, object]:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if log_path.exists():
            try:
                payload = json.loads(log_path.read_text(encoding="utf-8"))
                rendered = payload.get("rendered", [])
                if min_rendered == 0 or len(rendered) >= min_rendered:
                    return dict(payload)
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for harness events in {log_path}")


@contextmanager
def _isolated_tmux_dir(
    tmp_path: Path,
    *,
    prefix: str = "orch-tm-",
    extra_env: dict[str, str] | None = None,
    shim_script: str | None = None,
) -> Iterator[dict[str, str]]:
    tmpdir = tmp_path / "run_tmp"
    tmpdir.mkdir(mode=0o700, exist_ok=True)
    bin_shim = tmp_path / "bin"
    bin_shim.mkdir(mode=0o700, exist_ok=True)
    real_tmux = TMUX or "tmux"
    shim = bin_shim / "tmux"
    shim.write_text(
        shim_script or f'#!/usr/bin/env bash\nexec {shlex.quote(real_tmux)} -f /dev/null "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    socket_parent = "/tmp" if Path("/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix=prefix, dir=socket_parent) as socket_dir:
        os.chmod(socket_dir, 0o700)
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env["TMUX_TMPDIR"] = socket_dir
        env["TMPDIR"] = str(tmpdir)
        env["PATH"] = f"{bin_shim}{os.pathsep}{env.get('PATH', '')}"
        if extra_env:
            env.update(extra_env)
        try:
            yield env
        finally:
            subprocess.run(
                [real_tmux, "-f", "/dev/null", "kill-server"],
                env=env,
                check=False,
                capture_output=True,
            )


def _run_live_pty_scenario(
    tmp_path: Path,
    scenario: str,
    followup_body: str,
    *,
    allow_queued: bool = False,
    tool: str = "codex",
    script_override: Path | None = None,
) -> LiveScenarioOutcome:
    with _isolated_tmux_dir(tmp_path, prefix="orch-tm-") as env:
        harness = tmp_path / "pty_harness.py"
        harness.write_text(PTY_HARNESS, encoding="utf-8")
        harness.chmod(0o755)
        log_path = tmp_path / "events.json"
        followup = tmp_path / "followup.txt"
        followup.write_text(followup_body, encoding="utf-8")
        followup.chmod(0o600)

        session = f"orch-test-{scenario}-{uuid.uuid4().hex[:8]}"
        cmd = f"exec python3 -B {shlex.quote(str(harness))} {shlex.quote(scenario)} {shlex.quote(str(log_path))}"
        subprocess.run(
            [TMUX or "tmux", "-f", "/dev/null", "new-session", "-d", "-x", "56", "-y", "24", "-s", session, cmd],
            env=env,
            check=True,
        )
        ready_marker = "esc Close" if scenario == "blocked_modal" else "GPT-6.1"
        _wait_for_log_text(session, env, log_path, ready_marker)
        submit_env = env | {
            "ORCHESTRATION_DISPATCH_ID": f"live-{scenario}-{uuid.uuid4().hex[:6]}",
            "ORCHESTRATION_PREPARE_ATTEMPTS": "20",
            "ORCHESTRATION_PREPARE_DELAY_SECONDS": "0.02",
            "ORCHESTRATION_VERIFY_ATTEMPTS": "3",
            "ORCHESTRATION_VERIFY_DELAY_SECONDS": "0.05",
        }
        if allow_queued:
            submit_env["ORCHESTRATION_ALLOW_QUEUED"] = "1"
        script = script_override or (REPO_ROOT / f"skills/orchestrate-{tool}/scripts/submit_followup.sh")
        res = subprocess.run(
            [str(script), session, str(followup)],
            env=submit_env,
            capture_output=True,
            text=True,
            timeout=35,
            check=False,
        )
        min_rendered = 0 if scenario == "blocked_modal" else 1
        payload = _wait_for_events_json(log_path, min_rendered=min_rendered)
        return LiveScenarioOutcome(
            returncode=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
            raw_bytes=bytes.fromhex(str(payload["raw_hex"])),
            enters=int(payload["enters"]),  # type: ignore[call-overload]
            pastes=cast(list[str], payload["pastes"]),
            rendered=cast(list[str], payload["rendered"]),
        )


class TestSanitizedIncidentRegressions:
    def test_codex_56col_wrapped_end_marker_is_unsubmitted(self) -> None:
        # Arrange
        did = "codex-20261114T205752Z-101-1"
        pre = "• Done\n› Ask Codex to do anything\n\n  GPT-6.1 · /workspace/app\n"
        post = (
            "• WORKER-READY 0123456789abcdef\n"
            "\n"
            f"› [dispatch:{did}]       \n"
            "  Acknowledge this exact turn first by emitting         \n"
            "  [dispatch-accepted:codex-20261114T205752Z-101-        \n"
            "  1]. Read /workspace/app/driver-note.md in full         \n"
            "  and follow it exactly. [dispatch-                     \n"
            f"  end:{did}]                           \n"
            "                          \n"
            "  GPT-6.1 · /workspace/app\n"
            "                              ⚠ 3 warnings · f2 to view\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_codex_56col_wrapped_accepted_marker_in_composer_is_unsubmitted(self) -> None:
        # Arrange
        did = "codex-20261114T225023Z-202-2"
        pre = "• Done\n› \n"
        post = (
            f"› [dispatch:{did}]           \n"
            "  Acknowledge this exact turn first by emitting         \n"
            "  [dispatch-accepted:codex-20261114T225023Z-202-        \n"
            "  2]. Read /workspace/app/note.md [dispatch-end:codex-  \n"
            "  20261114T225023Z-202-2]                               \n"
            "  GPT-6.1 · /workspace/app\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_adjacent_line_post_end_acceptance_without_blank_line_is_submitted(self) -> None:
        # Arrange
        did = "agy-20261115T010101Z-303-3"
        pre = "• Working stale\n› \n"
        post = (
            f"[dispatch:{did}] Acknowledge this exact turn first by emitting "
            f"[dispatch-accepted:{did}]. Task. [dispatch-end:{did}]\n"
            f"[dispatch-accepted:{did}]\n"
            "• Working fresh\n"
            "›\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_split_paste_across_transcript_and_composer_is_partial_paste(self) -> None:
        # Arrange
        did = "agy-20261114T203123Z-404-4"
        pre = "› Type your message\n"
        post = (
            f"▸ Thought running partial [dispatch:{did}] chunk\n"
            "• Working\n"
            "────────────────────────────────────\n"
            "> ↑ 32 more lines\n"
            f"  [dispatch-end:{did}]\n"
            "  Gemini 3 Pro · /workspace/app\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_tier_badge_carbon_pre_capture_is_ready(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = (
            "  • Completed prior verification sweep.\n\n"
            f"{rule}\n"
            "> " + " " * 148 + "\n"
            f"{rule}\n"
            f"? for shortcuts{' ' * 99}Coordinator (Carbon)\n"
        )

        # Act
        ok, reason = check_pre_capture(pre, did)

        # Assert
        assert (ok, reason) == (True, "ok")

    def test_tier_badge_carbon_filled_wrapped_composer_is_unsubmitted(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = f"• Done\n{rule}\n> \n{rule}\n? for shortcuts{' ' * 99}Coordinator (Carbon)\n"
        prepared = (
            "  • Completed prior verification sweep.\n\n"
            f"{rule}\n"
            f"> [dispatch:{did}] Acknowledge this exact turn first by emitting\n"
            f"  [dispatch-accepted:{did}]. Read /workspace/app/wake.txt fully and\n"
            "  continue the existing independent reviewer goal; deduplicate completed\n"
            f"  work before closing the turn. [dispatch-end:{did}]\n"
            f"{rule}\n"
            f"{' ' * 130}Coordinator (Carbon)\n"
        )

        # Act
        verdict = classify_capture(pre, prepared, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    @pytest.mark.parametrize(
        "footer_line",
        [
            "                                                                Coordinator (Neon)",
            "                                                                Coordinator (Argon)",
            "                                                                Supervisor (Hydrogen)",
            "  GPT-OSS 120B (Medium)                                       /workspace/app",
            "  /workspace/app · Coordinator (Carbon)",
            "  main · Supervisor (Hydrogen)",
        ],
    )
    def test_tier_badge_and_gpt_oss_footer_variants_classify_unsubmitted(self, footer_line: str) -> None:
        # Arrange
        did = "agy-20261119T040000Z-100-1"
        rule = "─" * 80
        pre = f"> \n{rule}\n? for shortcuts · Coordinator (Carbon)\n"
        post = (
            f"• Done\n{rule}\n"
            f"> [dispatch:{did}] Acknowledge this exact turn first by emitting\n"
            f"  [dispatch-accepted:{did}]. Execute wake check. [dispatch-end:{did}]\n"
            f"{rule}\n{footer_line}\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_tier_badge_carbon_completed_turn_classifies_submitted(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = f"> \n{rule}\n? for shortcuts{' ' * 99}Coordinator (Carbon)\n"
        post = (
            f"> [dispatch:{did}] Acknowledge this exact turn first by emitting\n"
            f"  [dispatch-accepted:{did}]. Read /workspace/app/wake.txt. [dispatch-end:{did}]\n"
            "▸ Thought for 3s\n"
            f"  [dispatch-accepted:{did}]\n"
            "• Explored 2 files (wake.txt, status.json)\n"
            f"{rule}\n> \n{rule}\n{' ' * 130}Coordinator (Carbon)\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_tier_badge_carbon_active_esc_to_cancel_without_queue_is_ambiguous(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = f"> \n{rule}\n? for shortcuts{' ' * 99}Coordinator (Carbon)\n"
        post = (
            f"• Done\n{rule}\n"
            f"> [dispatch:{did}] Acknowledge [dispatch-end:{did}]\n"
            f"{rule}\n"
            f"esc to cancel{' ' * 117}Coordinator (Carbon)\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_tier_badge_carbon_active_esc_to_cancel_with_tab_to_queue_sets_flag(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = f"> \n{rule}\n? for shortcuts{' ' * 99}Coordinator (Carbon)\n"
        post = (
            f"• Done\n{rule}\n"
            f"> [dispatch:{did}] Acknowledge [dispatch-end:{did}]\n"
            f"{rule}\n"
            f"esc to cancel · tab to queue{' ' * 102}Coordinator (Carbon)\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.tab_to_queue is True

    def test_tier_badge_carbon_queued_message_classifies_queued(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = f"> \n{rule}\n? for shortcuts{' ' * 99}Coordinator (Carbon)\n"
        post = (
            "• Working\n"
            "  Queued messages:\n"
            f"  [dispatch:{did}] Acknowledge [dispatch-end:{did}]\n"
            f"{rule}\n>   \n{rule}\n"
            f"{' ' * 130}Coordinator (Carbon)\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.QUEUED

    def test_tier_badge_carbon_unaccepted_transcript_with_clean_composer_is_ambiguous(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = f"> \n{rule}\n? for shortcuts{' ' * 99}Coordinator (Carbon)\n"
        post = (
            "• Done\n"
            f"  [dispatch:{did}] Acknowledge [dispatch-end:{did}]\n"
            f"{rule}\n>   \n{rule}\n"
            f"{' ' * 130}Coordinator (Carbon)\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_tier_badge_carbon_partial_paste_without_end_marker_is_partial_paste(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = f"> \n{rule}\n? for shortcuts{' ' * 99}Coordinator (Carbon)\n"
        post = (
            f"• Done\n{rule}\n> [dispatch:{did}] truncated paste without end\n{rule}\n{' ' * 130}Coordinator (Carbon)\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_tier_badge_carbon_dirty_composer_fails_pre_check(self) -> None:
        # Arrange
        did = "agy-20261119T035545Z-2109196-11097"
        rule = "─" * 150
        pre = f"• Done\n{rule}\n> unfinished operator draft\n{rule}\n{' ' * 130}Coordinator (Carbon)\n"

        # Act
        ok, _reason = check_pre_capture(pre, did)

        # Assert
        assert ok is False

    @pytest.mark.parametrize(
        "non_footer_line",
        [
            "Coordinator (Carbon) approved the PR.",
            "Approved by Coordinator (Carbon)",
            "coordinator (Carbon)",
            "Coordinator (carbon)",
            "Summary · claude fixed the bug.",
            "Summary · Claude reviewed the PR.",
            "Note · Gemini 3 failed the check.",
            "Note · GPT-OSS is mentioned.",
            "Result · coordinator (Carbon)",
        ],
    )
    def test_non_footer_prose_and_case_mismatches_block_pre_check(self, non_footer_line: str) -> None:
        # Arrange
        did = "agy-20261119T040500Z-101-1"
        pre = f"• Done\n> \n{non_footer_line}\n"

        # Act
        ok, _reason = check_pre_capture(pre, did)

        # Assert
        assert ok is False

    @pytest.mark.parametrize(
        "non_footer_line",
        [
            "Coordinator (Carbon) approved the PR.",
            "Approved by Coordinator (Carbon)",
            "Summary · Claude reviewed the PR.",
            "Note · Gemini 3 failed the check.",
        ],
    )
    def test_non_footer_prose_below_composer_does_not_classify_unsubmitted(self, non_footer_line: str) -> None:
        # Arrange
        did = "agy-20261119T040500Z-101-2"
        rule = "─" * 80
        pre = f"> \n{rule}\n? for shortcuts · Coordinator (Carbon)\n"
        post = (
            f"• Done\n{rule}\n"
            f"> [dispatch:{did}] Acknowledge this turn. [dispatch-end:{did}]\n"
            f"{rule}\n{non_footer_line}\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_open_models_quota_modal_pre_check_blocks_input(self) -> None:
        # Arrange
        did = "agy-modal-pre-1"
        pre = "Models & Quota\n  1. Model A (active)\n  esc Close\n> \n"

        # Act
        ok, _reason = check_pre_capture(pre, did)

        # Assert
        assert ok is False

    def test_open_models_quota_modal_classify_returns_blocked_modal(self) -> None:
        # Arrange
        did = "agy-modal-post-1"
        pre = "› \n"
        post = "Models & Quota\n  1. Model A\n  esc Close\n› \n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_pre_check_cli_exits_75_on_modal(self, tmp_path: Path) -> None:
        # Arrange
        pre_file = tmp_path / "pre.txt"
        pre_file.write_text("Do you want to proceed?\n  Allow once\n  esc Close\n", encoding="utf-8")

        # Act
        rc = main(["pre-check", "--dispatch-id", "d-modal", "--pre", str(pre_file)])

        # Assert
        assert rc == 75

    def test_pre_check_cli_exits_75_on_dirty_composer(self, tmp_path: Path) -> None:
        # Arrange
        pre_file = tmp_path / "pre.txt"
        pre_file.write_text("› unfinished operator draft in composer\n", encoding="utf-8")

        # Act
        rc = main(["pre-check", "--dispatch-id", "d-dirty", "--pre", str(pre_file)])

        # Assert
        assert rc == 75

    def test_pre_check_cli_exits_1_on_marker_collision(self, tmp_path: Path) -> None:
        # Arrange
        did = "d-collide"
        pre_file = tmp_path / "pre.txt"
        pre_file.write_text(f"• Prior output [dispatch:{did}]\n› \n", encoding="utf-8")

        # Act
        rc = main(["pre-check", "--dispatch-id", did, "--pre", str(pre_file)])

        # Assert
        assert rc == 1

    def test_pre_check_cli_exits_0_on_placeholder_composer(self, tmp_path: Path) -> None:
        # Arrange
        pre_file = tmp_path / "pre.txt"
        pre_file.write_text("• Done\n› Ask Codex to do anything\n  GPT-6.1 · /workspace/app\n", encoding="utf-8")

        # Act
        rc = main(["pre-check", "--dispatch-id", "d-clean", "--pre", str(pre_file)])

        # Assert
        assert rc == 0

    def test_classify_cli_json_emits_delivery_state(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Arrange
        did = "d-json"
        pre_file = tmp_path / "pre.txt"
        cap_file = tmp_path / "cap.txt"
        pre_file.write_text("› \n", encoding="utf-8")
        cap_file.write_text(
            f"[dispatch:{did}] Body [dispatch-end:{did}]\n\n[dispatch-accepted:{did}]\n• Working\n› \n",
            encoding="utf-8",
        )

        # Act
        main(["classify", "--dispatch-id", did, "--pre", str(pre_file), "--capture", str(cap_file), "--json"])
        payload = json.loads(capsys.readouterr().out)

        # Assert
        assert payload["delivery_state"] == "submitted"

    def test_classify_cli_returns_zero(self, tmp_path: Path) -> None:
        # Arrange
        did = "d-cli-rc"
        pre_file = tmp_path / "pre.txt"
        cap_file = tmp_path / "cap.txt"
        pre_file.write_text("› \n", encoding="utf-8")
        cap_file.write_text(
            f"[dispatch:{did}] Body [dispatch-end:{did}]\n\n[dispatch-accepted:{did}]\n• Working\n› \n",
            encoding="utf-8",
        )

        # Act
        rc = main(["classify", "--dispatch-id", did, "--pre", str(pre_file), "--capture", str(cap_file)])

        # Assert
        assert rc == 0

    def test_classify_cli_plain_emits_composer_clean_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange
        did = "d-plain-clean"
        pre_file = tmp_path / "pre.txt"
        cap_file = tmp_path / "cap.txt"
        pre_file.write_text("› \n", encoding="utf-8")
        cap_file.write_text(
            f"[dispatch:{did}] Body [dispatch-end:{did}]\n\n[dispatch-accepted:{did}]\n• Working\n› \n",
            encoding="utf-8",
        )

        # Act
        main(["classify", "--dispatch-id", did, "--pre", str(pre_file), "--capture", str(cap_file)])
        stdout = capsys.readouterr().out

        # Assert
        assert "composer_clean=1" in stdout

    def test_classify_cli_plain_emits_tab_to_queue_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange
        did = "d-plain-tab"
        pre_file = tmp_path / "pre.txt"
        cap_file = tmp_path / "cap.txt"
        pre_file.write_text("• Working\n› \n", encoding="utf-8")
        cap_file.write_text(
            f"• Working\n› [dispatch:{did}] Hold [dispatch-end:{did}]\n  tab to queue · esc to interrupt\n",
            encoding="utf-8",
        )

        # Act
        main(["classify", "--dispatch-id", did, "--pre", str(pre_file), "--capture", str(cap_file)])
        stdout = capsys.readouterr().out

        # Assert
        assert "tab_to_queue=1" in stdout

    def test_pre_check_cli_json_emits_reason(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Arrange
        pre_file = tmp_path / "pre.txt"
        pre_file.write_text("Models & Quota\n  esc Close\n› \n", encoding="utf-8")

        # Act
        main(["pre-check", "--dispatch-id", "d-json-pre", "--pre", str(pre_file), "--json"])
        payload = json.loads(capsys.readouterr().out)

        # Assert
        assert payload["ok"] is False

    def test_classify_cli_without_capture_exits_nonzero(self, tmp_path: Path) -> None:
        # Arrange
        pre_file = tmp_path / "pre.txt"
        pre_file.write_text("› \n", encoding="utf-8")

        # Act
        with pytest.raises(SystemExit) as exc_info:
            main(["classify", "--dispatch-id", "d-nocap", "--pre", str(pre_file)])

        # Assert
        assert exc_info.value.code == 2


class TestNegativeControlsAndBoundaries:
    def test_generic_working_without_acceptance_is_ambiguous(self) -> None:
        # Arrange
        did = "neg-working-only"
        pre = "› \n"
        post = f"[dispatch:{did}] Body [dispatch-end:{did}]\n• Working\n› Ask Codex to do anything\n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_prompt_embedded_acceptance_before_end_does_not_count(self) -> None:
        # Arrange
        did = "neg-self-match"
        pre = "› \n"
        post = (
            f"[dispatch:{did}] Acknowledge this exact turn first by emitting "
            f"[dispatch-accepted:{did}]. Body [dispatch-end:{did}]\n"
            "• Working\n"
            "› \n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_acceptance_without_activity_is_accepted_active(self) -> None:
        # Arrange
        did = "neg-no-activity"
        pre = "› \n"
        post = f"[dispatch:{did}] Body [dispatch-end:{did}]\n\n[dispatch-accepted:{did}]\n› Ask Codex to do anything\n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_acceptance_and_activity_with_dirty_composer_is_accepted_active(self) -> None:
        # Arrange
        did = "neg-dirty-tail"
        pre = "› \n"
        post = (
            f"[dispatch:{did}] Body [dispatch-end:{did}]\n\n"
            f"[dispatch-accepted:{did}]\n"
            "• Working\n"
            "› still typing draft\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_word_queued_in_prompt_body_without_banner_is_not_queued(self) -> None:
        # Arrange
        did = "neg-word-queued"
        pre = "› \n"
        post = f"[dispatch:{did}] Inspect queued background jobs [dispatch-end:{did}]\n› \n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_queued_banner_with_clean_composer_is_queued(self) -> None:
        # Arrange
        did = "pos-queued"
        pre = "• Working\n› \n"
        post = (
            "• Working\n"
            "  Queued messages:\n"
            f"  [dispatch:{did}] Task [dispatch-end:{did}]\n"
            "› Ask Codex to do anything\n"
            "  esc to interrupt\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.QUEUED

    def test_tab_to_queue_footer_sets_flag_on_unsubmitted_composer(self) -> None:
        # Arrange
        did = "pos-tab-queue"
        pre = "• Working\n› \n"
        post = (
            f"• Working\n› [dispatch:{did}] Hold in composer [dispatch-end:{did}]\n  tab to queue · esc to interrupt\n"
        )

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.tab_to_queue is True

    def test_pre_existing_marker_forces_ambiguous_even_after_full_turn(self) -> None:
        # Arrange
        did = "neg-pre-existing"
        pre = f"[dispatch-accepted:{did}]\n› \n"
        post = f"[dispatch:{did}] Task [dispatch-end:{did}]\n\n[dispatch-accepted:{did}]\n• Working\n› \n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_composer_missing_end_marker_is_partial_paste(self) -> None:
        # Arrange
        did = "neg-missing-end"
        pre = "› \n"
        post = f"› [dispatch:{did}] Truncated prompt without closing token\n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_composer_paste_chip_plus_running_turn_without_end_is_partial_paste(self) -> None:
        # Arrange
        did = "neg-chip-split"
        pre = "• Done\n› \n"
        post = "• Working on early chunk\n› [Pasted Content 24 lines]\n  esc to interrupt\n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_paste_chip_with_end_marker_alone_is_unsubmitted(self) -> None:
        # Arrange
        did = "pos-chip-intact"
        pre = "› \n"
        post = f"› [Pasted Content] [dispatch-end:{did}]\n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_out_of_order_transcript_markers_is_ambiguous(self) -> None:
        # Arrange
        did = "neg-order"
        pre = "› \n"
        post = f"[dispatch-end:{did}]\n\n[dispatch:{did}] Backwards\n\n[dispatch-accepted:{did}]\n• Working\n› \n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_markdown_blockquote_in_composer_preserves_unsubmitted_start(self) -> None:
        # Arrange
        did = "pos-markdown-quote"
        post = f"› [dispatch:{did}] Quoted note:\n> cited text inside prompt\n[dispatch-end:{did}]\n"

        # Act
        regions = segment_capture(post, re.compile(DEFAULT_COMPOSER_PATTERN), f"[dispatch:{did}]")

        # Assert
        assert regions.composer_start == 0

    def test_normalize_lines_collapses_right_and_left_box_borders(self) -> None:
        # Arrange
        did = "norm-box"
        _, _, end_m = markers_for_id(did)
        lines = ["│ prefix [dispatch-  │", f"│ end:{did}]   │"]

        # Act
        norm = normalize_lines(lines)

        # Assert
        assert end_m in norm.text

    def test_normalize_lines_preserves_blank_line_barrier(self) -> None:
        # Arrange
        did = "norm-barrier"
        _, _, end_m = markers_for_id(did)
        lines = ["[dispatch-", "", f"end:{did}]"]

        # Act
        norm = normalize_lines(lines)

        # Assert
        assert end_m not in norm.text

    def test_bottom_boxed_blank_line_keeps_clean_composer_detection(self) -> None:
        # Arrange
        did = "box-blank-footer"
        post = f"[dispatch:{did}] [dispatch-end:{did}]\n\n[dispatch-accepted:{did}]\n• Working\n› \n│     │\n"

        # Act
        regions = segment_capture(post, re.compile(DEFAULT_COMPOSER_PATTERN), f"[dispatch:{did}]")

        # Assert
        assert regions.composer_clean is True

    def test_composer_line_containing_pasted_chip_text_not_treated_as_footer(self) -> None:
        # Arrange
        did = "footer-pasted-guard"
        post = "› \n  GPT-6 [Pasted Content]\n"

        # Act
        regions = segment_capture(post, re.compile(DEFAULT_COMPOSER_PATTERN), f"[dispatch:{did}]")

        # Assert
        assert regions.composer_clean is False

    def test_prompt_line_with_approval_phrase_and_start_marker_not_modal(self) -> None:
        # Arrange
        did = "modal-skip-start"
        post = f"› [dispatch:{did}] Discuss Allow once behavior\n  [dispatch-end:{did}]\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_composer_only_end_marker_without_chip_is_partial_paste(self) -> None:
        # Arrange
        did = "comp-end-only"
        post = f"› trailing half [dispatch-end:{did}]\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_paste_chip_without_end_or_transcript_change_is_ambiguous(self) -> None:
        # Arrange
        did = "chip-no-end"
        pre = "• Done\n› \n"
        post = "• Done\n› [Pasted Content 5 lines]\n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_scrollback_redraw_with_adjacent_unaccepted_frame_is_unsubmitted(self) -> None:
        # Arrange
        did = "redraw-adj"
        s_m, a_m, e_m = markers_for_id(did)
        post = f"{s_m}{a_m}{e_m}\n\n› {s_m}{e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_scrollback_backwards_frame_with_intact_composer_is_partial_paste(self) -> None:
        # Arrange
        did = "redraw-backwards"
        s_m, _, e_m = markers_for_id(did)
        post = f"{e_m} {s_m}\n\n› {s_m} Body {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_scrollback_post_end_acceptance_plus_composer_draft_is_partial_paste(self) -> None:
        # Arrange
        did = "redraw-post-acc"
        s_m, a_m, e_m = markers_for_id(did)
        post = f"{s_m} Body {e_m}\n{a_m}\n\n› {s_m} Body {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_transcript_adjacent_start_and_end_without_gap_is_submitted(self) -> None:
        # Arrange
        did = "trans-adj"
        s_m, a_m, e_m = markers_for_id(did)
        post = f"{s_m}{e_m}\n{a_m}\n• Working\n›\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_composer_promoted_immediately_after_end_without_clean_prompt_is_accepted_active(self) -> None:
        # Arrange
        did = "promo-adj"
        s_m, a_m, e_m = markers_for_id(did)
        post = f"› {s_m}{e_m}\n{a_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_scrollback_unequal_marker_counts_with_composer_draft_is_partial_paste(self) -> None:
        # Arrange
        did = "redraw-unequal"
        s_m, _, e_m = markers_for_id(did)
        post = f"{s_m} {e_m}\n{s_m}\n\n› {s_m} Body {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_scrollback_zero_gap_without_acceptance_is_unsubmitted(self) -> None:
        # Arrange
        did = "redraw-zero-gap"
        s_m, _, e_m = markers_for_id(did)
        post = f"{s_m}{e_m}\n\n› {s_m} Body {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_scrollback_balanced_plus_composer_chip_only_end_is_partial_paste(self) -> None:
        # Arrange
        did = "redraw-chip-only-end"
        s_m, _, e_m = markers_for_id(did)
        post = f"{s_m} Body {e_m}\n\n› [Pasted Content] {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_transcript_only_start_marker_lacks_ordered_evidence(self) -> None:
        # Arrange
        did = "only-start-trans"
        post = f"[dispatch:{did}] missing end\n› \n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert "lacks ordered" in verdict.reason


class TestReviewerRegressionsAndOverrides:
    def test_multi_paragraph_composer_with_blank_before_blockquote_is_unsubmitted(self) -> None:
        # Arrange
        did = "r1-blockquote-blank"
        post = f"• Done\n\n› [dispatch:{did}] First paragraph.\n\n> quoted note\n[dispatch-end:{did}]\n"

        # Act
        verdict = classify_capture("• Done\n› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_multi_paragraph_composer_with_divider_before_blockquote_is_unsubmitted(self) -> None:
        # Arrange
        did = "r1-blockquote-divider"
        post = f"• Done\n\n› [dispatch:{did}] Top section\n---\n> cited context\n[dispatch-end:{did}]\n"

        # Act
        verdict = classify_capture("• Done\n› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_composer_with_bullet_line_stays_unsubmitted(self) -> None:
        # Arrange
        did = "r1-composer-bullet"
        post = (
            f"• Done\n\n› [dispatch:{did}] Checklist:\n"
            f"• bullet item inside draft\n└ nested tree item\n[dispatch-end:{did}]\n"
        )

        # Act
        verdict = classify_capture("• Done\n› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_composer_with_bullet_line_rejected_by_pre_check(self) -> None:
        # Arrange
        pre = "• Done\n\n› Unfinished draft\n• bullet item in composer\n"

        # Act
        ok, _reason = check_pre_capture(pre, "r1-dirty-bullet")

        # Assert
        assert ok is False

    def test_worked_for_barrier_with_leading_spaces_stops_composer_scan(self) -> None:
        # Arrange
        did = "r1-worked-for-space"
        post = f"  Worked for 4m 26s • 9:40 PM\n\n› [dispatch:{did}] Body [dispatch-end:{did}]\n"

        # Act
        regions = segment_capture(post, re.compile(DEFAULT_COMPOSER_PATTERN), f"[dispatch:{did}]")

        # Assert
        assert regions.composer_start == 2

    def test_worked_for_barrier_inside_box_stops_composer_scan(self) -> None:
        # Arrange
        did = "r1-worked-for-box"
        post = f"│  Worked for 4m 26s  │\n\n│ › [dispatch:{did}] Body [dispatch-end:{did}] │\n"

        # Act
        regions = segment_capture(post, re.compile(DEFAULT_COMPOSER_PATTERN), f"[dispatch:{did}]")

        # Assert
        assert regions.composer_start == 2

    def test_wrapped_prompt_body_mentioning_allow_once_is_unsubmitted(self) -> None:
        # Arrange
        did = "r1-wrapped-allow-once"
        post = (
            f"• Done\n\n› [dispatch:{did}] Explain why\n"
            "  Allow once vs Always allow buttons appear\n"
            "  and Do you want to proceed? [y/n]\n"
            f"  [dispatch-end:{did}]\n"
        )

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_idle_transcript_with_y_n_above_placeholder_passes_pre_check(self) -> None:
        # Arrange
        pre = "• Previous assistant message asked Do you want to proceed? [y/n]\n• Done\n› Ask Codex to do anything\n"

        # Act
        ok, _reason = check_pre_capture(pre, "r1-idle-history-yn")

        # Assert
        assert ok is True

    def test_wrapped_modal_above_clean_bare_prompt_blocks(self) -> None:
        # Arrange
        did = "r1-wrapped-modal-clean"
        post = "• Output\nDo you want to\nproceed? [y/n]\n› \n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_interleaved_transcript_pairs_plus_composer_draft_is_partial_paste(self) -> None:
        # Arrange
        did = "r1-interleaved"
        s_m, a_m, e_m = markers_for_id(did)
        post = f"{s_m} one {s_m} two {e_m} {a_m} {e_m}\n\n› {s_m} draft {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_composer_two_starts_one_end_is_partial_paste(self) -> None:
        # Arrange
        did = "r1-two-starts"
        s_m, _, e_m = markers_for_id(did)
        post = f"› {s_m} Body {e_m} {s_m} truncated\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_composer_one_start_zero_ends_with_chip_is_partial_paste(self) -> None:
        # Arrange
        did = "r1-start-no-end"
        post = f"› [dispatch:{did}] [Pasted Content 12 lines]\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_composer_end_before_start_is_partial_paste(self) -> None:
        # Arrange
        did = "r1-end-before-start"
        s_m, _, e_m = markers_for_id(did)
        post = f"› {e_m} garbage {s_m} Body {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_composer_two_ends_without_chip_is_partial_paste(self) -> None:
        # Arrange
        did = "r1-two-ends-no-chip"
        s_m, _, e_m = markers_for_id(did)
        post = f"› {s_m} Body {e_m} {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_composer_one_start_and_chip_echoed_two_ends_is_unsubmitted(self) -> None:
        # Arrange
        did = "r2-chip-echo-two-ends"
        s_m, _, e_m = markers_for_id(did)
        post = f"› {s_m} Body {e_m}\n› [Pasted Content] {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_echoed_dispatch_start_in_active_tool_output_keeps_submitted(self) -> None:
        # Arrange
        did = "r1-echoed-start"
        s_m, a_m, e_m = markers_for_id(did)
        post = f'{s_m} Task {e_m}\n{a_m}\n• Ran echo "{s_m}"\n• Working\n› \n'

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_echoed_dispatch_start_keeps_accepted_active(self) -> None:
        # Arrange
        did = "r1-echoed-active"
        s_m, a_m, e_m = markers_for_id(did)
        post = f'{s_m} Task {e_m}\n{a_m}\n• Ran echo "{s_m}"\n  esc to interrupt\n'

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_completed_tool_verb_ran_satisfies_submitted_activity(self) -> None:
        # Arrange
        did = "r1-verb-ran"
        s_m, a_m, e_m = markers_for_id(did)
        post = f"{s_m} Task {e_m}\n{a_m}\n• Ran pytest -q\n› \n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_completed_tool_verb_edited_satisfies_submitted_activity(self) -> None:
        # Arrange
        did = "r1-verb-edited"
        s_m, a_m, e_m = markers_for_id(did)
        post = f"{s_m} Task {e_m}\n{a_m}\n• Edited tools/app.py\n› \n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_press_up_to_edit_inside_prompt_line_does_not_self_match_queued(self) -> None:
        # Arrange
        did = "r1-press-up-self-match"
        s_m, _, e_m = markers_for_id(did)
        post = f"• Working\n{s_m} Discuss Press ↑ to edit behavior {e_m}\n› \n  esc to interrupt\n"

        # Act
        verdict = classify_capture("• Working\n› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_press_up_to_edit_footer_preserves_clean_composer_for_queued(self) -> None:
        # Arrange
        did = "r1-press-up-footer"
        s_m, _, e_m = markers_for_id(did)
        post = (
            f"• Working\n  Queued messages:\n  {s_m} Queued task {e_m}\n"
            "› Ask Codex to do anything\n  Press ↑ to edit\n  esc to interrupt\n"
        )

        # Act
        verdict = classify_capture("• Working\n› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.QUEUED

    def test_queued_messages_banner_four_lines_above_is_queued(self) -> None:
        # Arrange
        did = "r1-queued-window"
        s_m, _, e_m = markers_for_id(did)
        post = (
            "• Working\n"
            "  Queued messages:\n"
            "  - earlier msg 1\n"
            "  - earlier msg 2\n"
            "  - earlier msg 3\n"
            f"  {s_m} Latest queued {e_m}\n"
            "› \n"
            "  esc to interrupt\n"
        )

        # Act
        verdict = classify_capture("• Working\n› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.QUEUED

    def test_shifted_blank_line_with_chip_and_stale_working_is_ambiguous(self) -> None:
        # Arrange
        did = "r1-pre-post-seg"
        pre = "• Working stale\n› \n"
        post = "• Working stale\n\n› [Pasted Content 5 lines]\n  GPT-6.1 · /workspace/app\n"

        # Act
        verdict = classify_capture(pre, post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_split_partial_paste_followed_by_accepted_marker_stays_partial_paste(self) -> None:
        # Arrange
        did = "r1-promo-chip"
        s_m, a_m, e_m = markers_for_id(did)
        post = f"{s_m} partial\n• Working\n› ↑ 12 more lines {e_m}\n{a_m}\n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_env_activity_regex_posix_space_class_matches_submitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        did = "r2-env-posix"
        s_m, a_m, e_m = markers_for_id(did)
        monkeypatch.setenv("ORCHESTRATION_ACTIVITY_REGEX", r"^[[:space:]]*•[[:space:]]+Working")
        post = f"{s_m} Body {e_m}\n{a_m}\n  •  Working cleanly\n› \n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_env_activity_regex_override_blocks_unmatched_default_activity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        did = "r2-env-act-override"
        s_m, a_m, e_m = markers_for_id(did)
        monkeypatch.setenv("ORCHESTRATION_ACTIVITY_REGEX", r"CUSTOM_ACTIVITY_TOKEN")
        post = f"{s_m} Body {e_m}\n{a_m}\n• Working\n› \n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_env_composer_regex_override_recognizes_custom_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        did = "r2-env-comp-override"
        s_m, a_m, e_m = markers_for_id(did)
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*CUSTOM_PROMPT>[[:space:]]*$")
        post = f"{s_m} Body {e_m}\n{a_m}\n• Working\nCUSTOM_PROMPT> \n"

        # Act
        verdict = classify_capture("CUSTOM_PROMPT> \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_env_queued_regex_override_recognizes_custom_banner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        did = "r2-env-queue-override"
        s_m, _, e_m = markers_for_id(did)
        monkeypatch.setenv("ORCHESTRATION_QUEUED_REGEX", r"PENDING QUEUE:")
        post = f"PENDING QUEUE:\n{s_m} Body {e_m}\n› \n"

        # Act
        verdict = classify_capture("› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.QUEUED

    def test_env_queued_regex_does_not_self_match_prompt_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        did = "r16-env-queue-self"
        s_m, _, e_m = markers_for_id(did)
        monkeypatch.setenv("ORCHESTRATION_QUEUED_REGEX", r"PENDING QUEUE:")
        post = f"• Working\n{s_m} Inspect PENDING QUEUE: {e_m}\n› \n  esc to interrupt\n"

        # Act
        verdict = classify_capture("• Working\n› \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    @pytest.mark.parametrize("tool", TOOLS)
    def test_bundled_dispatch_helper_matches_canonical_sha256(self, tool: str) -> None:
        # Arrange
        canonical = hashlib.sha256((REPO_ROOT / "tools/orchestrate_dispatch.py").read_bytes()).hexdigest()
        bundled_path = REPO_ROOT / f"skills/orchestrate-{tool}/scripts/orchestrate_dispatch.py"

        # Act
        bundled = hashlib.sha256(bundled_path.read_bytes()).hexdigest()

        # Assert
        assert bundled == canonical

    def test_pre_check_detects_dirty_modal_above_five_trailing_lines(self) -> None:
        # Arrange
        pre = "Allow once\noption line 2\noption line 3\noption line 4\noption line 5\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-002")

        # Assert
        assert ok is False

    def test_classify_queued_detects_press_up_banner_four_lines_after_end(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-004")
        post = f"• Working\n{s_m} task {e_m}\nextra 1\nextra 2\nextra 3\n  Press ↑ to edit\n› \n"

        # Act
        verdict = classify_capture("› \n", post, "m-004")

        # Assert
        assert verdict.state is DeliveryState.QUEUED

    def test_classify_all_blank_capture_returns_ambiguous(self) -> None:
        # Arrange
        post = "   \n   \n"

        # Act
        verdict = classify_capture("", post, "m-064")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_pre_check_rejects_multiparagraph_draft_with_unicode_prompt(self) -> None:
        # Arrange
        pre = "• Done\n› unfinished draft\n\nmore draft text\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-068")

        # Assert
        assert ok is False

    def test_pre_check_allows_boxed_non_prompt_transcript_line(self) -> None:
        # Arrange
        pre = "│ non-prompt text\n  other line\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-014a")

        # Assert
        assert ok is True

    def test_pre_check_rejects_boxed_ascii_prompt_draft_with_blank_below(self) -> None:
        # Arrange
        pre = "• Done\n│ > draft line\n\n│ continuation\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-014b")

        # Assert
        assert ok is False

    def test_pre_check_allows_plain_summary_below_working_spinner(self) -> None:
        # Arrange
        pre = "• Working\nSummary line without prompt\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-015")

        # Assert
        assert ok is True

    def test_classify_unsubmitted_detects_bare_ascii_prompt_with_dispatch(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-019")
        post = f"• Working\n\n> {s_m} ASCII prompt {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "m-019")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_prompt_starts_dispatch_prefers_specific_id_over_other_dispatch_token(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-017")
        post = f"• Done\n› {s_m} Top\n> [dispatch:other-id] note\n\n{e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "m-017")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_pre_check_allows_markdown_quote_separated_by_blank_line(self) -> None:
        # Arrange
        pre = "> markdown quote\n\nTrailing plain output\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-127")

        # Assert
        assert ok is True

    def test_dirty_composer_ignores_divider_before_any_prompt_candidate(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-128")
        post = f"• Done\n› {s_m} top\n────────\nbottom {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "m-128")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_segment_capture_empty_body_marks_composer_unclean(self) -> None:
        # Arrange
        composer_re = re.compile(DEFAULT_COMPOSER_PATTERN)

        # Act
        regions = segment_capture("\n", composer_re)

        # Assert
        assert regions.composer_clean is False

    def test_pre_check_ignores_numbered_menu_above_earlier_prompt(self) -> None:
        # Arrange
        pre = "Models & Quota\n• Answered\n› prior prompt\n• Reply ok\n› \n"

        # Act
        ok, _ = check_pre_capture(pre, "m-170")

        # Assert
        assert ok is True

    def test_classify_excludes_approval_phrase_quoted_inside_dispatch_span(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("m-023")
        post = f"• Done\n{s_m}\nAllow once\n{e_m}\n{a_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "m-023")

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_classify_detects_modal_after_adjacent_empty_dispatch_markers(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("m-131")
        post = f"{s_m}{e_m}\nAllow once\n{e_m}\n{a_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "m-131")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_pre_check_detects_dirty_modal_header_above_details_line(self) -> None:
        # Arrange
        pre = "Allow access to this file?\nDetails line\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-024")

        # Assert
        assert ok is False

    def test_pre_check_stops_clean_modal_scan_at_working_spinner(self) -> None:
        # Arrange
        pre = "Models & Quota\n• Working\n› \n"

        # Act
        ok, _ = check_pre_capture(pre, "m-079")

        # Assert
        assert ok is True

    def test_pre_check_bounds_clean_modal_scan_to_six_nonempty_lines(self) -> None:
        # Arrange
        pre = "Allow once\nL2\nL3\nL4\nL5\nL6\nL7\n› \n"

        # Act
        ok, _ = check_pre_capture(pre, "m-082")

        # Assert
        assert ok is True

    def test_pre_check_detects_modal_text_inside_dirty_composer(self) -> None:
        # Arrange
        pre = "› Allow once\n"

        # Act
        _, reason = check_pre_capture(pre, "m-027")

        # Assert
        assert "modal" in reason

    def test_classify_detects_modal_in_dirty_composer_without_dispatch_markers(self) -> None:
        # Arrange
        post = "• Output\n› prompt\nAllow once\n"

        # Act
        verdict = classify_capture("› \n", post, "m-028")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_classify_partial_paste_ignores_approval_phrase_inside_composer_start_only(self) -> None:
        # Arrange
        s_m, _, _ = markers_for_id("m-084")
        post = f"› {s_m} broken paste\nAllow once\n"

        # Act
        verdict = classify_capture("› \n", post, "m-084")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_classify_detects_dirty_modal_spanning_prompt_like_line(self) -> None:
        # Arrange
        post = "Do you want to proceed?\n> press enter\n2. Cancel\n"

        # Act
        verdict = classify_capture("› \n", post, "m-134")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_pre_check_allows_empty_dirty_composer_after_transcript_only(self) -> None:
        # Arrange
        pre = "• Done\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-030")

        # Assert
        assert ok is True

    def test_balanced_unaccepted_pairs_allow_adjacent_dispatch_spans_before_composer(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-141")
        post = f"• Done\n{s_m} 1 {e_m}{s_m} 2 {e_m}\n\n› {s_m} 3 {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "m-141")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_ordered_dispatch_spans_reject_acceptance_before_closing_end_marker(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("m-114")
        post = f"Queued messages:\n{s_m} A {a_m} {e_m}\n{s_m} truncated\n› \n"

        # Act
        verdict = classify_capture("› \n", post, "m-114")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_queued_verdict_sets_composer_clean_true(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-046")
        post = f"• Working\n  Queued messages:\n  {s_m} body {e_m}\n› \n"

        # Act
        verdict = classify_capture("› \n", post, "m-046")

        # Assert
        assert verdict.composer_clean is True

    def test_pre_existing_marker_verdict_sets_composer_clean_false(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-156")
        pre = f"{s_m} stale {e_m}\n› \n"

        # Act
        verdict = classify_capture(pre, "› \n", "m-156")

        # Assert
        assert verdict.composer_clean is False

    def test_partial_paste_verdict_sets_composer_clean_false(self) -> None:
        # Arrange
        s_m, _, _ = markers_for_id("m-158")
        post = f"› {s_m} truncated\n"

        # Act
        verdict = classify_capture("› \n", post, "m-158")

        # Assert
        assert verdict.composer_clean is False

    def test_unsubmitted_verdict_sets_composer_clean_false(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-161")
        post = f"› {s_m} body {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "m-161")

        # Assert
        assert verdict.composer_clean is False

    def test_submitted_verdict_defaults_tab_to_queue_false(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("m-163")
        post = f"{s_m} body {e_m}\n{a_m}\n• Working\n› \nGPT-6.1 · /repo\n"

        # Act
        verdict = classify_capture("› \n", post, "m-163")

        # Assert
        assert verdict.tab_to_queue is False

    def test_cli_classify_missing_pre_exits_with_usage_error(self, tmp_path: Path) -> None:
        # Arrange
        cap = tmp_path / "cap.txt"
        cap.write_text("› \n", encoding="utf-8")

        # Act
        with pytest.raises(SystemExit) as exc:
            main(["classify", "--dispatch-id", "m-166", "--capture", str(cap)])

        # Assert
        assert exc.value.code == 2

    def test_cli_pre_check_missing_dispatch_id_exits_with_usage_error(self, tmp_path: Path) -> None:
        # Arrange
        pre = tmp_path / "pre.txt"
        pre.write_text("› \n", encoding="utf-8")

        # Act
        with pytest.raises(SystemExit) as exc:
            main(["pre-check", "--pre", str(pre)])

        # Assert
        assert exc.value.code == 2

    def test_classify_submitted_recognizes_agy_generating_and_circle_thought_activity(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("m-agy-sub")
        post = f"{s_m} Check standard {e_m}\n{a_m}\n○ Thought for 2s\n● Checked 1 task\n⣷  Generating...\n>\n"

        # Act
        verdict = classify_capture(">\n", post, "m-agy-sub")

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_classify_ambiguous_when_agy_generating_without_acceptance_marker(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-agy-neg")
        post = (
            f"{s_m} Check standard {e_m}\n"
            "⣷  Generating...\n"
            "└ Tip: Use /settings to configure your environment.\n"
            ">\n"
            "esc to cancel   accept-edits · Workspace (main)\n"
        )

        # Act
        verdict = classify_capture(">\n", post, "m-agy-neg")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_pre_existing_marker_verdict_defaults_tab_to_queue_false(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-041")
        pre = f"{s_m} stale {e_m}\n› \n"

        # Act
        verdict = classify_capture(pre, "› \n", "m-041")

        # Assert
        assert verdict.tab_to_queue is False

    def test_dirty_composer_scan_stops_at_transcript_activity_above_unmatched_draft(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("m-066")
        post = f"› {s_m} earlier {e_m}\n• Working\n› current draft\n"

        # Act
        verdict = classify_capture("› \n", post, "m-066")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_pre_check_allows_markdown_numbered_list_above_claude_prompt(self) -> None:
        # Arrange
        pre = "• Done checking files.\n1. Fixed bug A\n2. Added tests\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r2-num-claude")

        # Assert
        assert ok is True

    def test_classify_submitted_with_markdown_numbered_list_above_agy_prompt(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r2-num-agy")
        post = f"{s_m} Fix {e_m}\n{a_m}\n• Checked 2 files\n1. Fixed bug A\n2. Added tests\n>\n"

        # Act
        verdict = classify_capture(">\n", post, "r2-num-agy")

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_pre_check_detects_emg_agy_modal_with_scroll_bar_and_fresh_id(self) -> None:
        # Arrange
        pre = (
            "[dispatch:turn-1] Task [dispatch-end:turn-1]\n"
            "────────\n"
            ">\n"
            "────────\n"
            "└ Models & Quota\n"
            "  Weekly Limit Remaining 99.69%\n"
            "  ↑/↓ Scroll · pgup/pgdown Page · esc Close\n"
            "  accept-edits · Workspace (main)\n"
        )

        # Act
        _, reason = check_pre_capture(pre, "fresh-id-123")

        # Assert
        assert "modal" in reason

    def test_classify_emg_agy_modal_with_scroll_bar_is_blocked_modal(self) -> None:
        # Arrange
        post = "────────\n>\n────────\n└ Models & Quota\n  Select a model:\n  > 1. Model A\n  ↑/↓ Scroll · esc Close\n"

        # Act
        verdict = classify_capture(">\n", post, "fresh-id-124")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_classify_emg_agy_collapsed_chip_with_blanks_fresh_id_is_partial_paste(self) -> None:
        # Arrange
        s_old, _, e_old = markers_for_id("old-turn-1")
        post = (
            f"> {s_old} old turn {e_old}\n"
            "⣾  Working...\n"
            "────────────────────────────────────────\n"
            "> ↑ 32 more lines\n"
            "  first paragraph line\n"
            "\n"
            "  As soon as you take\n"
            "esc to cancel   accept-edits · Workspace (main)\n"
        )

        # Act
        verdict = classify_capture("• Done\n> \n", post, "fresh-id-125")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_classify_18col_wrapped_dispatch_id_across_four_lines_is_unsubmitted(self) -> None:
        # Arrange
        did = "codex-20261114T205752Z-3938707-17321"
        post = (
            "• Done\n"
            f"> [dispatch:codex-\n"
            "  20261114T205752Z\n"
            "  -3938707-17321]\n"
            "  Run checks.\n"
            "\n"
            f"  [dispatch-end:{did}]\n"
        )

        # Act
        verdict = classify_capture("> \n", post, did)

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_pre_check_rejects_draft_containing_tool_verb_bullets(self) -> None:
        # Arrange
        pre = "• Done\n› Draft prompt:\n• Read AGENTS.md\n• Working directory status\n"

        # Act
        ok, _ = check_pre_capture(pre, "r2-bullet-pre")

        # Assert
        assert ok is False

    def test_classify_unsubmitted_preserves_composer_with_tool_verb_bullets(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("r2-bullet-unsub")
        post = f"• Done\n› {s_m} Task:\n• Read AGENTS.md\n• Working directory status\n{e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "r2-bullet-unsub")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_pre_check_detects_dirty_custom_composer_regex_with_posix_space(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", "^[[:space:]]*CUSTOM_PROMPT>[[:space:]]*$")

        # Act
        ok, _ = check_pre_capture("• Done\nCUSTOM_PROMPT> unfinished work\n", "cust-pre")

        # Assert
        assert ok is False

    def test_classify_unsubmitted_on_custom_composer_regex_with_posix_space(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", "^[[:space:]]*CUSTOM_PROMPT>[[:space:]]*$")
        s_m, _, e_m = markers_for_id("cust-unsub")
        post = f"• Done\nCUSTOM_PROMPT> {s_m} Hello {e_m}\n"

        # Act
        verdict = classify_capture("CUSTOM_PROMPT> \n", post, "cust-unsub")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_submitted_ignores_echoed_complete_pair_after_acceptance(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r2-echo-pair")
        post = f"{s_m} go {e_m}\n{a_m}\n• Ran cat directive\n{s_m} go {e_m}\n› \n"

        # Act
        verdict = classify_capture("› \n", post, "r2-echo-pair")

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_env_activity_regex_posix_punct_class_matches_submitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_ACTIVITY_REGEX", "^[[:punct:]]+[[:space:]]+Running")
        s_m, a_m, e_m = markers_for_id("posix-punct")
        post = f"{s_m} task {e_m}\n{a_m}\n*> Running suite\n› \n"

        # Act
        verdict = classify_capture("› \n", post, "posix-punct")

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_pre_check_rejects_single_line_bare_ascii_draft(self) -> None:
        # Arrange
        pre = "• Done\n> bare ascii draft\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-020")

        # Assert
        assert ok is False

    def test_pre_check_allows_markdown_quote_at_top_before_trailing_divider(self) -> None:
        # Arrange
        pre = "> markdown quote\n\nplain output\n──────────\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-068-wrap")

        # Assert
        assert ok is True

    def test_ordered_dispatch_spans_prefers_latest_adjacent_accepted_turn(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("m-170-adj")
        post = f"{s_m} t1 {e_m}\n{a_m}\n• Working\n{s_m} t2 {e_m}{a_m}\n› \n"

        # Act
        verdict = classify_capture("› \n", post, "m-170-adj")

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_classify_unsubmitted_preserves_ran_bullet_inside_dispatch(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("r3-b1")
        post = f"› {s_m} Fix this failure:\n  • Ran pytest and saw exit 1\n  {e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "r3-b1")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_unsubmitted_preserves_divider_and_blockquote_inside_dispatch(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("r3-b2")
        post = f"> {s_m} Check:\n  ────────────────────\n  > quoted line\n  {e_m}\n"

        # Act
        verdict = classify_capture("> \n", post, "r3-b2")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_pre_check_rejects_draft_with_indented_ran_bullet_below_prompt(self) -> None:
        # Arrange
        pre = "› Draft:\n  • Ran pytest\n"

        # Act
        ok, _ = check_pre_capture(pre, "r3-b3")

        # Assert
        assert ok is False

    def test_classify_submitted_ignores_prose_discussing_allow_once_buttons(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r3-m-prose")
        post = (
            f"{s_m} Audit modal {e_m}\n{a_m}\n"
            "• Checked src/modal.tsx\n"
            'The dialog renders "Allow once" and "Always allow" buttons.\n'
            "❯ \n"
        )

        # Act
        verdict = classify_capture("❯ \n", post, "r3-m-prose")

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_pre_check_allows_proceed_sentence_in_assistant_summary(self) -> None:
        # Arrange
        pre = "• Done checking\nDo you want to proceed with phase 2?\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r3-proceed-prose")

        # Assert
        assert ok is True

    def test_classify_accepted_active_ignores_yn_above_trailing_spinner(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r3-apt-yn")
        post = f"{s_m} Run test {e_m}\n{a_m}\n• Checked apt output\nContinue? [y/n]\n⣷ Working...\n"

        # Act
        verdict = classify_capture("❯ \n", post, "r3-apt-yn")

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_custom_composer_regex_preserves_trailing_prompt_bracket_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", "^[[:space:]]*CUSTOM_PROMPT[>:][[:space:]]*$")
        s_m, _, e_m = markers_for_id("r3-cust-class")
        post = f"CUSTOM_PROMPT> {s_m} hi {e_m}\n"

        # Act
        verdict = classify_capture("CUSTOM_PROMPT> \n", post, "r3-cust-class")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_custom_composer_regex_preserves_dollar_prompt_class_in_pre_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", "^[[:space:]]*[$%][[:space:]]*$")

        # Act
        ok, _ = check_pre_capture("$ unfinished\n", "r3-dollar-pre")

        # Assert
        assert ok is False

    def test_pre_check_detects_modal_with_ctrl_c_esc_close_bar(self) -> None:
        # Arrange
        pre = "Choose an option:\n> 1. Option A\n  2. Option B\n  ctrl+c to cancel · esc Close\n"

        # Act
        _, reason = check_pre_capture(pre, "r3-ctrlc-modal")

        # Assert
        assert "modal" in reason

    def test_classify_unsubmitted_preserves_unindented_ran_bullet_inside_dispatch(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("r4-col0-ran")
        post = f"› {s_m} Check:\n• Ran pytest\n{e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "r4-col0-ran")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_unsubmitted_preserves_unindented_ran_bullet_with_custom_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", "^[[:space:]]*CUSTOM_PROMPT>[[:space:]]*$")
        s_m, _, e_m = markers_for_id("r4-col0-cust")
        post = f"CUSTOM_PROMPT> {s_m} Check:\n• Ran pytest\n{e_m}\n"

        # Act
        verdict = classify_capture("CUSTOM_PROMPT> \n", post, "r4-col0-cust")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_ambiguous_when_prior_dispatch_followed_by_new_prompt(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("r4-prior-end")
        post = f"› {s_m} old\n• Ran pytest\n{e_m}\n• Working\n› current draft\n"

        # Act
        verdict = classify_capture("› \n", post, "r4-prior-end")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_pre_check_allows_equal_indented_working_line_below_previous_prompt(self) -> None:
        # Arrange
        pre = "  › previous prompt\n  • Working\n"

        # Act
        ok, _ = check_pre_capture(pre, "r4-eq-indent")

        # Assert
        assert ok is True

    def test_pre_check_stops_indented_scan_at_equal_indented_working_line(self) -> None:
        # Arrange
        pre = "› Draft:\n  • Working\n  • Working\n"

        # Act
        ok, _ = check_pre_capture(pre, "r4-eq-stop")

        # Assert
        assert ok is True

    def test_pre_check_rejects_draft_spanning_divider_before_prompt_candidate(self) -> None:
        # Arrange
        pre = "• Done\n› draft top\n────────\ndraft bottom\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-151")

        # Assert
        assert ok is False

    def test_pre_check_rejects_indented_ran_bullet_with_plain_line_below_draft_prompt(self) -> None:
        # Arrange
        pre = "› Draft:\n  plain line\n  • Ran pytest\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-022")

        # Assert
        assert ok is False

    def test_pre_check_allows_markdown_quote_at_top_before_indented_working_and_divider(self) -> None:
        # Arrange
        pre = "> markdown quote\n  • Working\n──────────\n"

        # Act
        ok, _ = check_pre_capture(pre, "m-082")

        # Assert
        assert ok is True

    def test_classify_unsubmitted_when_accepted_embedded_inside_dispatch(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("m-192")
        post = f"› {s_m}{a_m}\n• Ran pytest\n{e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "m-192")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_unsubmitted_when_accepted_abuts_start_above_rule_and_blockquote(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r5-abut-start-rule")
        post = f"› {s_m}{a_m}\n────────────────────\n> quoted\n{e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "r5-abut-start-rule")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_accepted_active_when_acceptance_abuts_end_marker(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r5-abut-acc")
        post = f"› {s_m}\n• Ran pytest\n{e_m}{a_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "r5-abut-acc")

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_classify_partial_paste_when_accepted_abuts_end_below_chip_and_rule(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r5-abut-end-chip")
        post = f"› {s_m} body\n↑ 12 more lines\n────────────────────\n> note\n{e_m}{a_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "r5-abut-end-chip")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_pre_check_rejects_draft_with_blank_line_above_indented_working_line(self) -> None:
        # Arrange
        pre = "› Draft:\n\n  • Working\n"

        # Act
        ok, _ = check_pre_capture(pre, "r5-blank-working")

        # Assert
        assert ok is False

    def test_pre_check_rejects_draft_with_plain_line_above_indented_working_line(self) -> None:
        # Arrange
        pre = "› Draft:\n  plain line\n  • Working\n"

        # Act
        ok, _ = check_pre_capture(pre, "r5-plain-working")

        # Assert
        assert ok is False

    def test_classify_unsubmitted_real_submit_followup_header_codex_col0_ran(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("cx-real")
        post = f"› {s_m} Acknowledge this exact turn first by emitting {a_m}. Investigate:\n• Ran pytest\n{e_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "cx-real")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_unsubmitted_real_submit_followup_header_claude_col0_ran(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("cl-1")
        post = f"❯ {s_m} Acknowledge this exact turn first by emitting {a_m}. Fix:\n• Ran pytest and failed\n{e_m}\n"

        # Act
        verdict = classify_capture("❯ \n", post, "cl-1")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_unsubmitted_real_submit_followup_indented_rule_and_ran(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("cx-1")
        post = (
            f"› {s_m} Acknowledge this exact turn first by emitting {a_m}.\n"
            "  ────────────────────\n"
            "  • Ran pytest\n"
            f"  {e_m}\n"
        )

        # Act
        verdict = classify_capture("› \n", post, "cx-1")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_unsubmitted_real_submit_followup_bare_prompt_indented_rule_quote(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("ag-1")
        post = (
            f"> {s_m} Acknowledge this exact turn first by emitting {a_m}. Check:\n"
            "  ────────────────────\n"
            "  > quoted line\n"
            f"  {e_m}\n"
        )

        # Act
        verdict = classify_capture("> \n", post, "ag-1")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_unsubmitted_real_submit_followup_col0_rule_and_blockquote(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("cx-rq")
        post = (
            f"› {s_m} Acknowledge this exact turn first by emitting {a_m}. Investigate:\n"
            "────────────────────\n"
            "> quoted line\n"
            f"{e_m}\n"
        )

        # Act
        verdict = classify_capture("› \n", post, "cx-rq")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_classify_partial_paste_when_truncated_prompt_dispatch_contains_ran_bullet(self) -> None:
        # Arrange
        s_m, _, _ = markers_for_id("d-p")
        post = f"› {s_m} Fix:\n• Ran pytest\ntruncated\n"

        # Act
        verdict = classify_capture("› \n", post, "d-p")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_classify_partial_paste_when_truncated_prompt_dispatch_embeds_accepted_adjacent(self) -> None:
        # Arrange
        s_m, a_m, _ = markers_for_id("r5-trunc-acc")
        post = f"› {s_m}{a_m} truncated\n"

        # Act
        verdict = classify_capture("› \n", post, "r5-trunc-acc")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_classify_ambiguous_when_truncated_prompt_dispatch_shows_live_working(self) -> None:
        # Arrange
        s_m, _, _ = markers_for_id("r5-trunc-work")
        post = f"› {s_m} head\n• Working\n"

        # Act
        verdict = classify_capture("› \n", post, "r5-trunc-work")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_ambiguous_when_prior_dispatch_followed_by_unindented_prompt(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r5-later-prompt")
        post = f"› {s_m} {a_m}\n{e_m}\n────────\n❯ later draft\n"

        # Act
        verdict = classify_capture("› \n", post, "r5-later-prompt")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_pre_check_rejects_codex_draft_with_col0_ran_bullet(self) -> None:
        # Arrange
        pre = "› Draft:\n• Ran pytest\n"

        # Act
        ok, _ = check_pre_capture(pre, "d-b3")

        # Assert
        assert ok is False

    def test_pre_check_rejects_claude_draft_with_col0_edited_bullet(self) -> None:
        # Arrange
        pre = "❯ Draft:\n• Edited foo.py\n"

        # Act
        ok, _ = check_pre_capture(pre, "cl-2")

        # Assert
        assert ok is False

    def test_pre_check_rejects_bare_prompt_draft_with_indented_ran_bullet(self) -> None:
        # Arrange
        pre = "> Draft:\n  • Ran pytest\n"

        # Act
        ok, _ = check_pre_capture(pre, "d-b3-bare")

        # Assert
        assert ok is False

    def test_pre_check_allows_ran_bullet_above_active_esc_to_interrupt_footer(self) -> None:
        # Arrange
        pre = "› prompt\n• Ran pytest\nesc to interrupt\n"

        # Act
        ok, _ = check_pre_capture(pre, "r5-active-footer")

        # Assert
        assert ok is True

    def test_classify_detects_modal_below_unsubmitted_dispatch_paste(self) -> None:
        # Arrange
        s1, _, e1 = markers_for_id("turn-1")
        s2, a2, e2 = markers_for_id("t2")
        post = (
            f"{s1} Task {e1}\n"
            "────────\n"
            f"> {s2} Acknowledge {a2}. Go {e2}\n"
            "────────\n"
            "└ Models & Quota\n"
            "  Weekly Limit Remaining 99.69%\n"
            "  ↑/↓ Scroll · pgup/pgdown Page · esc Close\n"
            "  accept-edits · Workspace (main)\n"
        )

        # Act
        verdict = classify_capture(">\n", post, "t2")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_classify_detects_modal_below_truncated_paste_separated_by_rule(self) -> None:
        # Arrange
        s_m, _, _ = markers_for_id("r5-trunc-modal")
        post = f"> {s_m} partial top\n────────\n└ Models & Quota\n  ↑/↓ Scroll · esc Close\n"

        # Act
        verdict = classify_capture(">\n", post, "r5-trunc-modal")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_pre_check_blocks_modal_with_numbered_cursor_option_above_clean_prompt(self) -> None:
        # Arrange
        pre = "Do you want to proceed?\n› 1. Yes\n  2. No\n> \n"

        # Act
        _, reason = check_pre_capture(pre, "r5-menu-cursor")

        # Assert
        assert "modal" in reason

    def test_pre_check_ignores_multiline_wrapped_quoted_modal_phrase(self) -> None:
        # Arrange
        pre = 'The dialog asks "Do you want to\nproceed?" with buttons.\n❯ \n'

        # Act
        ok, _ = check_pre_capture(pre, "r5-multiline-quote")

        # Assert
        assert ok is True

    def test_pre_check_ignores_single_quoted_modal_phrase_after_apostrophe(self) -> None:
        # Arrange
        pre = "The user's prompt shows 'Do you want to proceed?' here.\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r5-apostrophe-quote")

        # Assert
        assert ok is True

    def test_pre_check_ignores_markdown_bullet_allow_once_list(self) -> None:
        # Arrange
        pre = "• Summary\n* Allow once\n* Always allow\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r5-md-bullet")

        # Assert
        assert ok is True

    def test_pre_check_ignores_numbered_markdown_allow_once_list(self) -> None:
        # Arrange
        pre = "• Summary\n1. Allow once\n2. Always allow\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r5-md-numbered")

        # Assert
        assert ok is True

    def test_classify_accepted_active_ignores_numbered_markdown_allow_once_and_apt_yn(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("m5")
        post = (
            f"{s_m} Run {e_m}\n{a_m}\n• Running apt\n  1. Allow once\nDo you want to continue? [y/n]\nesc to cancel\n"
        )

        # Act
        verdict = classify_capture("❯ \n", post, "m5")

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_custom_composer_regex_unanchored_escaped_dollar_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*PROMPT\$")

        # Act
        ok, _ = check_pre_capture("PROMPT\n", "r5-escaped-dollar-no-raise")

        # Assert
        assert ok is True

    def test_custom_composer_regex_preserves_escaped_dollar_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*PROMPT\$[[:space:]]*$")

        # Act
        ok, _ = check_pre_capture("PROMPT$ unfinished draft\n", "r5-escaped-dollar")

        # Assert
        assert ok is False

    def test_custom_composer_regex_rewrites_alternation_branches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*A[[:space:]]*$|^[[:space:]]*B[[:space:]]*$")
        s_m, _, e_m = markers_for_id("r5-alt")
        post = f"A {s_m} hello {e_m}\n"

        # Act
        verdict = classify_capture("A \n", post, "r5-alt")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_pre_check_detects_cursor_numbered_allow_once_with_unlisted_header(self) -> None:
        # Arrange
        pre = "Allow execution of `pytest`?\n› 1. Allow once\n  2. Always allow\n  3. No\n› \n"

        # Act
        _, reason = check_pre_capture(pre, "r6-fn7")

        # Assert
        assert "modal" in reason

    def test_pre_check_detects_numbered_allow_once_without_cursor_or_known_header(self) -> None:
        # Arrange
        lines = "\n".join(f"  line {idx}" for idx in range(18))
        pre = f"Run this command?\n{lines}\n  1. Allow once\n  2. Always allow\n"

        # Act
        _, reason = check_pre_capture(pre, "r6-fn8")

        # Assert
        assert "modal" in reason

    def test_classify_detects_cursor_numbered_allow_once_after_unaccepted_paste(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-fn9")
        post = f"› {s_m} Ack {a_m}. Go {e_m}\nCustom header?\n› 1. Allow once\n  2. Always allow\n"

        # Act
        verdict = classify_capture("› \n", post, "r6-fn9")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_pre_check_detects_modal_after_closing_markdown_code_fence(self) -> None:
        # Arrange
        pre = "  x = 1\n```\nDo you want to proceed?\n  Run `pytest`\n› 1. Yes\n  2. No\n› \n"

        # Act
        _, reason = check_pre_capture(pre, "r6-fn1")

        # Assert
        assert "modal" in reason

    def test_classify_detects_modal_with_unclosed_dquote_before_proceed_dialog(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-fn5")
        post = (
            f"› {s_m} Ack {a_m}. Go {e_m}\n"
            '  err: unterminated "\n'
            'Do you want to proceed?\n  echo "hi"\n  1. Yes\n  2. No\n'
        )

        # Act
        verdict = classify_capture("› \n", post, "r6-fn5")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_pre_check_ignores_inline_backtick_prose_after_closing_code_fence(self) -> None:
        # Arrange
        pre = "```\nThe dialog asks `Do you want to proceed?`\n› \n"

        # Act
        ok, _ = check_pre_capture(pre, "r6-fp1")

        # Assert
        assert ok is True

    def test_pre_check_ignores_plural_possessive_inside_single_quoted_prose(self) -> None:
        # Arrange
        pre = "The guide says 'See users' prompt: Do you want to proceed?' here.\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r6-fp2")

        # Assert
        assert ok is True

    def test_pre_check_blocks_seven_line_double_quoted_col0_modal(self) -> None:
        # Arrange
        pre = '• Done\n"Line 1\nLine 2\nLine 3\nLine 4\nLine 5\nDo you want to proceed?\nLine 7"\n❯ \n'

        # Act
        _, reason = check_pre_capture(pre, "r6-fp3")

        # Assert
        assert "modal" in reason

    def test_pre_check_ignores_seven_line_fenced_block_with_modal(self) -> None:
        # Arrange
        pre = "• Done\n```text\nLine 1\nLine 2\nLine 3\nLine 4\nLine 5\nDo you want to proceed?\nLine 7\n```\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r6-fp3-fenced")

        # Assert
        assert ok is True

    def test_pre_check_ignores_multiline_quote_starting_on_transcript_stop_line(self) -> None:
        # Arrange
        pre = '• Ran python3 -c "print(\nDo you want to\nproceed?"\n❯ \n'

        # Act
        ok, _ = check_pre_capture(pre, "r6-fp4")

        # Assert
        assert ok is True

    def test_pre_check_ignores_allow_once_mid_sentence_prose(self) -> None:
        # Arrange
        pre = "• Done\nAllow once is an option in the dialog.\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r6-nb2-1")

        # Assert
        assert ok is True

    def test_classify_detects_modal_below_truncated_paste_separated_by_blank_line(self) -> None:
        # Arrange
        s_m, _, _ = markers_for_id("r6-fn10")
        post = f"❯ {s_m} partial\n\nDo you want to proceed?\n› 1. Yes\n  2. No\n"

        # Act
        verdict = classify_capture("❯ \n", post, "r6-fn10")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_classify_detects_models_quota_below_truncated_paste_separated_by_blank_line(self) -> None:
        # Arrange
        s_m, _, _ = markers_for_id("r6-fn11")
        post = f"> {s_m} partial\n\n└ Models & Quota\n  ↑/↓ Scroll · esc Close\n"

        # Act
        verdict = classify_capture("> \n", post, "r6-fn11")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_classify_ambiguous_when_claude_asterisk_spinner_follows_dispatch_end(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-s1")
        post = f"❯ {s_m} Ack {a_m}. Go {e_m}\n* Working…\nesc to interrupt\n"

        # Act
        verdict = classify_capture("❯ \n", post, "r6-s1")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_ambiguous_when_claude_brewing_spinner_follows_dispatch_end(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-s2")
        post = f"❯ {s_m} Ack {a_m}. Go {e_m}\n✻ Brewing…\nesc to interrupt\n"

        # Act
        verdict = classify_capture("❯ \n", post, "r6-s2")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_ambiguous_when_bare_tool_call_follows_dispatch_end(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-s3")
        post = f"❯ {s_m} Ack {a_m}. Go {e_m}\nBash(pytest -q)\nesc to interrupt\n"

        # Act
        verdict = classify_capture("❯ \n", post, "r6-s3")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_ambiguous_when_braille_quarter_spinner_follows_dispatch_end(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-s4")
        post = f"› {s_m} Ack {a_m}. Go {e_m}\n⠸ Thinking...\nesc to interrupt\n"

        # Act
        verdict = classify_capture("› \n", post, "r6-s4")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_ambiguous_when_bullet_tool_call_follows_dispatch_end(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-s5")
        post = f"› {s_m} Ack {a_m}. Go {e_m}\n• Bash(pytest -q)\nesc to interrupt\n"

        # Act
        verdict = classify_capture("› \n", post, "r6-s5")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_ambiguous_when_prose_reply_follows_dispatch_end(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-s6")
        post = f"› {s_m} Ack {a_m}. Go {e_m}\n\nInvestigating now.\n"

        # Act
        verdict = classify_capture("› \n", post, "r6-s6")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_ambiguous_when_adjacent_empty_dispatch_followed_by_ran_bullet(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("r6-m094")
        post = f"› {s_m}{e_m}\n• Ran pytest\n"

        # Act
        verdict = classify_capture("› \n", post, "r6-m094")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_accepted_active_when_prompt_has_adjacent_empty_markers_and_acceptance(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r6-m149")
        post = f"› {s_m}{e_m}{a_m}\n"

        # Act
        verdict = classify_capture("› \n", post, "r6-m149")

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_pre_check_allows_trailing_closing_code_fence_at_eof(self) -> None:
        # Arrange
        pre = "Here is code:\n```python\nx = 1\n```\n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b1-fence-eof")

        # Assert
        assert ok is True

    def test_pre_check_allows_trailing_code_fence_above_esc_to_interrupt(self) -> None:
        # Arrange
        pre = "Here is code:\n```\nesc to interrupt\n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b1-fence-esc")

        # Assert
        assert ok is True

    def test_pre_check_allows_trailing_standalone_double_quoted_allow_once(self) -> None:
        # Arrange
        pre = '• Done\n"Allow once"\n'

        # Act
        ok, _ = check_pre_capture(pre, "r7-b1-dquote-eof")

        # Assert
        assert ok is True

    def test_pre_check_allows_trailing_standalone_backticked_allow_once(self) -> None:
        # Arrange
        pre = "• Done\n`Allow once`\n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b1-btick-eof")

        # Assert
        assert ok is True

    def test_pre_check_allows_trailing_multiline_double_quoted_prose_at_eof(self) -> None:
        # Arrange
        pre = '• Done\n"line 1\nline 2"\n'

        # Act
        ok, _ = check_pre_capture(pre, "r7-b1-multiline-eof")

        # Assert
        assert ok is True

    def test_classify_accepted_active_with_trailing_code_fence_above_esc_to_interrupt(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r7-c1")
        post = f"{s_m} Go {e_m}\n{a_m}\n```python\nx = 1\n```\nesc to interrupt\n"

        # Act
        verdict = classify_capture("› \n", post, "r7-c1")

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_pre_check_ignores_models_quota_inside_closed_fenced_block(self) -> None:
        # Arrange
        pre = "```\nModels & Quota\n  esc Close\n```\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b2-fence-models")

        # Assert
        assert ok is True

    def test_pre_check_ignores_proceed_dialog_inside_closed_fenced_block(self) -> None:
        # Arrange
        pre = "```text\nDo you want to proceed?\n  1. Yes\n  2. No\n```\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b2-fence-proceed")

        # Assert
        assert ok is True

    def test_pre_check_ignores_allow_once_buttons_inside_closed_fenced_block(self) -> None:
        # Arrange
        pre = "```\nAllow once\nAlways allow\n```\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b2-fence-buttons")

        # Assert
        assert ok is True

    def test_classify_submitted_ignores_proceed_dialog_inside_closed_fenced_block(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r7-d4")
        post = (
            f"› {s_m} Go {e_m}\n{a_m}\n• Working\n"
            "Here is an example:\n```text\nDo you want to proceed?\n  1. Yes\n  2. No\n```\n❯ \n"
        )

        # Act
        verdict = classify_capture("› \n", post, "r7-d4")

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_pre_check_blocks_proceed_modal_between_unclosed_and_wrapped_dquotes(self) -> None:
        # Arrange
        pre = 'err: unterminated "\nDo you want to proceed?\n  echo "hello\n  world"\n  1. Yes\n  2. No\n'

        # Act
        _, reason = check_pre_capture(pre, "r7-b2-unclosed-wrap")

        # Assert
        assert "modal" in reason

    def test_pre_check_blocks_run_command_modal_between_unclosed_dquotes(self) -> None:
        # Arrange
        pre = 'err: unterminated "\nRun this command?\n  python3 -c "print(\n  1. Yes\n  2. No\n'

        # Act
        _, reason = check_pre_capture(pre, "r7-b2-unclosed-py")

        # Assert
        assert "modal" in reason

    def test_pre_check_blocks_modal_flanked_by_single_quoted_tokens_on_same_line(self) -> None:
        # Arrange
        pre = "Run 'cmd1': Allow execution of cmd? 'help'\n  1. Yes\n  2. No\n"

        # Act
        _, reason = check_pre_capture(pre, "r7-b2-squote-flank")

        # Assert
        assert "modal" in reason

    def test_classify_blocks_modal_below_excluded_dispatch_with_unclosed_dquote(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("r7-d1")
        post = f'› {s_m} Fix "unclosed\n  {e_m}\nDo you want to proceed?\n  echo "hi\n  there"\n  1. Yes\n  2. No\n'

        # Act
        verdict = classify_capture("› \n", post, "r7-d1")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_pre_check_ignores_multiline_single_quoted_modal_phrase(self) -> None:
        # Arrange
        pre = "The dialog asks 'Do you want to\nproceed?' with buttons.\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b2-multi-squote")

        # Assert
        assert ok is True

    def test_pre_check_ignores_multiline_backtick_quoted_modal_phrase(self) -> None:
        # Arrange
        pre = "The dialog asks `Do you want to\nproceed?` with buttons.\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b2-multi-btick")

        # Assert
        assert ok is True

    def test_pre_check_blocks_fourteen_line_double_quoted_col0_modal(self) -> None:
        # Arrange
        lines = "\n".join(f"Line {idx}" for idx in range(1, 13))
        pre = f'• Done\n"{lines}\nDo you want to proceed?\nLine 14"\n❯ \n'

        # Act
        _, reason = check_pre_capture(pre, "r7-b2-14line-dquote")

        # Assert
        assert "modal" in reason

    def test_pre_check_ignores_fourteen_line_fenced_modal_prose(self) -> None:
        # Arrange
        lines = "\n".join(f"Line {idx}" for idx in range(1, 13))
        pre = f"• Done\n```text\n{lines}\nDo you want to proceed?\nLine 14\n```\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-b2-14line-fenced")

        # Assert
        assert ok is True

    def test_pre_check_ignores_allow_once_recommended_prose_sentence(self) -> None:
        # Arrange
        pre = "• Done\nAllow once (recommended) is safer.\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r7-nb3-prose")

        # Assert
        assert ok is True

    def test_pre_check_blocks_cursor_allow_once_with_recommended_tag(self) -> None:
        # Arrange
        pre = "Allow execution?\n› 1. Allow once (recommended)\n  2. Always allow\n› \n"

        # Act
        _, reason = check_pre_capture(pre, "r7-nb3-button")

        # Assert
        assert "modal" in reason

    def test_classify_ambiguous_when_esc_to_interrupt_footer_without_tab_to_queue(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r7-a1")
        post = f"› {s_m} Ack {a_m}. Go {e_m}\nesc to interrupt\n"

        # Act
        verdict = classify_capture("› \n", post, "r7-a1")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_ambiguous_when_esc_to_cancel_footer_without_tab_to_queue(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r7-a2")
        post = f"> {s_m} Ack {a_m}. Go {e_m}\nesc to cancel\n"

        # Act
        verdict = classify_capture("> \n", post, "r7-a2")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_unsubmitted_when_tab_to_queue_and_esc_to_interrupt_present(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r7-a4")
        post = f"› {s_m} Ack {a_m}.\n• Ran pytest\n{e_m}\ntab to queue · esc to interrupt\n"

        # Act
        verdict = classify_capture("› \n", post, "r7-a4")

        # Assert
        assert verdict.state is DeliveryState.UNSUBMITTED

    def test_pre_check_blocks_modal_between_plural_squote_and_quoted_path(self) -> None:
        # Arrange
        pre = "Run 'tests' and Allow execution of '/workspace/repo'?\n  1. Yes\n  2. No\n"

        # Act
        _, reason = check_pre_capture(pre, "r8-sq1")

        # Assert
        assert "modal" in reason

    def test_pre_check_blocks_modal_between_plural_squote_and_quoted_flag_with_cursor(self) -> None:
        # Arrange
        pre = "Run 'tests' to Allow execution of cmd? '--all'\n› 1. Yes\n  2. No\n› \n"

        # Act
        _, reason = check_pre_capture(pre, "r8-sq2")

        # Assert
        assert "modal" in reason

    def test_classify_blocks_modal_between_plural_squote_and_quoted_flag(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r8-d1")
        post = f"› {s_m} Ack {a_m}. Go {e_m}\nRun 'tests' to Allow execution of cmd? '--all'\n› 1. Yes\n  2. No\n"

        # Act
        verdict = classify_capture("› \n", post, "r8-d1")

        # Assert
        assert verdict.state is DeliveryState.BLOCKED_MODAL

    def test_pre_check_blocks_modal_when_plural_squote_precedes_possessive_noun(self) -> None:
        # Arrange
        pre = "Run 'tests' and Allow execution of users' script?\n  1. Yes\n  2. No\n"

        # Act
        _, reason = check_pre_capture(pre, "r8-x5")

        # Assert
        assert "modal" in reason

    def test_pre_check_ignores_multiple_plural_possessives_inside_single_quoted_prose(self) -> None:
        # Arrange
        pre = "The guide says 'See users' and admins' prompt: Do you want to proceed?' here.\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r8-x3")

        # Assert
        assert ok is True

    def test_pre_check_blocks_modal_between_unclosed_backtick_and_command_substitution(self) -> None:
        # Arrange
        pre = "err: missing `x\nRun this command?\n  echo `date\n  1. Yes\n  2. No\n"

        # Act
        _, reason = check_pre_capture(pre, "r8-mq-bt")

        # Assert
        assert "modal" in reason

    def test_pre_check_blocks_modal_between_unclosed_squote_and_plural_possessive(self) -> None:
        # Arrange
        pre = "SyntaxError: unterminated 'x\nDo you want to proceed?\n  Check users' config\n› 1. Yes\n  2. No\n› \n"

        # Act
        _, reason = check_pre_capture(pre, "r8-mq-sq-poss")

        # Assert
        assert "modal" in reason

    def test_pre_check_blocks_modal_between_unclosed_dquote_and_trailing_dquote(self) -> None:
        # Arrange
        pre = 'err: unterminated "x\nDo you want to proceed?\n  echo foo"\n  1. Yes\n  2. No\n'

        # Act
        _, reason = check_pre_capture(pre, "r8-mq-dq-trail")

        # Assert
        assert "modal" in reason

    def test_pre_check_blocks_modal_after_col0_unclosed_dquote_above_indented_quote(self) -> None:
        # Arrange
        pre = '"unterminated error\nDo you want to proceed?\n  echo foo"\n'

        # Act
        _, reason = check_pre_capture(pre, "r8-col0-unclosed")

        # Assert
        assert "modal" in reason

    def test_pre_check_ignores_box_bordered_closed_fenced_code_block(self) -> None:
        # Arrange
        pre = "│ ```text │\n│ Do you want to proceed? │\n│   1. Yes │\n│   2. No │\n│ ``` │\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r8-bf1")

        # Assert
        assert ok is True

    def test_classify_submitted_ignores_box_bordered_closed_fenced_code_block(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r8-bx")
        post = (
            f"│ › {s_m} Go {e_m} │\n"
            f"│ {a_m} │\n"
            "│ • Working │\n"
            "│ ```bash │\n"
            "│ Do you want to proceed? │\n"
            "│   1. Yes │\n"
            "│   2. No │\n"
            "│ ``` │\n"
            "│ ❯ │\n"
        )

        # Act
        verdict = classify_capture("› \n", post, "r8-bx")

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_pre_check_ignores_tilde_fenced_code_block_with_modal_example(self) -> None:
        # Arrange
        pre = "~~~text\nDo you want to proceed?\n  1. Yes\n  2. No\n~~~\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r8-tilde")

        # Assert
        assert ok is True

    def test_pre_check_ignores_four_backtick_fenced_block_wrapping_three_backticks(self) -> None:
        # Arrange
        pre = "````md\n```\n```\nDo you want to proceed?\n````\n❯ \n"

        # Act
        ok, _ = check_pre_capture(pre, "r8-4btick")

        # Assert
        assert ok is True

    def test_pre_check_ignores_escaped_json_double_quotes_containing_modal_phrase(self) -> None:
        # Arrange
        pre = '• Output:\n  "{\\"prompt\\": \\"Do you want to proceed?\\"}"\n› \n'

        # Act
        ok, _ = check_pre_capture(pre, "r8-json")

        # Assert
        assert ok is True

    def test_classify_submitted_ignores_escaped_json_double_quotes_containing_modal_phrase(self) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r8-jcls")
        post = f'› {s_m} Go {e_m}\n{a_m}\n• Ran tool\n  "{{\\"prompt\\": \\"Do you want to proceed?\\"}}"\n› \n'

        # Act
        verdict = classify_capture("› \n", post, "r8-jcls")

        # Assert
        assert verdict.state is DeliveryState.SUBMITTED

    def test_pre_check_blocks_codex_cursor_yes_proceed_approval_menu(self) -> None:
        # Arrange
        pre = "Would you like to run the following command?\n  $ git status\n› 1. Yes, proceed (y)\n  2. No (esc)\n› \n"

        # Act
        _, reason = check_pre_capture(pre, "r8-codex-yes")

        # Assert
        assert "modal" in reason

    @pytest.mark.parametrize(
        "pre",
        [
            "Run 'tests' and Allow execution of users' Script?\n  1. Yes\n  2. No\n› \n",
            "Run 'see tests' and Allow execution of users' Script?\n  1. Yes\n  2. No\n› \n",
            "Run 'tests' and Allow execution of cmd'?\n  1. Yes\n  2. No\n› \n",
            "Run 'tests' and Allow execution of docs'?\n  1. Yes\n  2. No\n› \n",
            "Run 'tests' and Allow execution of flag='--all'?\n  1. Yes\n  2. No\n› \n",
            "Run 'tests' and Allow access to path='/workspace/repo'?\n  1. Yes\n  2. No\n› \n",
            "Run 'tests' to Allow execution of ('/tmp/run.sh')?\n  1. Yes\n  2. No\n› \n",
            "Run 'tests' to Allow execution of arg=''?\n  1. Yes\n  2. No\n› \n",
            "Run 'tests' to Allow execution of cmd:'--all'?\n  1. Yes\n  2. No\n› \n",
            "Run 'tests' and Allow execution of ' '\n  1. Yes\n  2. No\n› \n",
            'err: unterminated "x\nDo you want to proceed? (y/n)\n  echo foo"\n  1. Yes\n  2. No\n› \n',
            "err: missing `x\nRun this command? (y/n)\n  echo foo`\n  1. Yes\n  2. No\n› \n",
            "err: missing `x\nConfirm?\n  Allow once\n  echo foo`\n› \n",
            'err: unterminated "x\nConfirm?\n  Enter to confirm\n  echo foo"\n› \n',
            "SyntaxError: unterminated 'x\nSelect a model: GPT-5\n  Check users' Config\n  1. Yes\n  2. No\n› \n",
            '"unterminated error\nDo you want to proceed?\narg"\n  1. Yes\n  2. No\n› \n',
            "'unterminated error\nDo you want to proceed?\narg'\n  1. Yes\n  2. No\n› \n",
            "`unterminated error\nDo you want to proceed?\narg`\n  1. Yes\n  2. No\n› \n",
            'err: unterminated "x\nDo you want to\nproceed?\n  echo foo"\n› \n',
            'err: unterminated "x\nDo you want to proceed? foo"\n› \n',
            'err: unterminated "x\n⚠ Do you want to proceed?\n  echo foo"\n  1. Yes\n  2. No\n› \n',
            "• Output: ```bash\nDo you want to proceed? (y/n)\n  echo foo`\n  1. Yes\n  2. No\n› \n",
            "Section\n~~~~~~~\nDo you want to proceed?\n~~~~~~~\n› \n",
            '"x\nDo you want to proceed?\n"y\n› \n',
            '"Do you want to\nproceed?" Do you want to proceed? foo"\n› \n',
            "Run `a` Do you want to proceed? `b`\n› \n",
            "Run 'a' Do you want to proceed? 'b'\n› \n",
            'err: "x\n  "y\nDo you want to proceed?\n› \n',
            'First "hello\nworld"\nDo you want to proceed?\n  foo"\n› \n',
            '"start\n' + "line\n" * 24 + 'Do you want to proceed?\nend"\n❯ \n',
            '• Output: "C:\\\\temp\\\\" Do you want to proceed?\n  1. Yes\n  2. No\n',
            "• Path 'C:\\\\temp\\\\' Allow access to this file?\n  1. Yes\n",
            "    ```\nDo you want to proceed?\n    ```\n❯ \n",
            "Run 'see tests' and Allow execution of cmd'?\n  1. Yes\n  2. No\n› \n",
            "Run 'see tests' and Allow execution of cmd'?\n› \n",
            "Run 'my tests' and Allow execution of user's tool'?\n  1. Yes\n  2. No\n› \n",
            "│ Run 'see tests' and Allow execution of cmd'? │\n│   1. Yes │\n│   2. No │\n│ › │\n",
            "Run 'see tests' and Allow execution of path/'bar'?\n  1. Yes\n  2. No\n› \n",
            "Run 'see tests' and Allow execution of $'rm -rf'?\n  1. Yes\n  2. No\n› \n",
            "Check 'my tests' results: Allow execution of cmd?'\n› \n",
            "Check 'my tests' results: Do you want to proceed? (y/n) foo' here\n› \n",
            "Run 'my tests' guide: allow running /tmp/x'? here\n› \n",
            "err: `unterminated and Allow execution of `cmd`?\n› \n",
            'err: "unterminated and Allow execution of cmd"?\n  1. Yes\n  2. No\n› \n',
            'err: "unterminated and allow running /tmp/x"?\n› \n',
            "Run 'echo `a' and Allow execution of 'echo b`?\n  1. Yes\n  2. No\n› \n",
            'Run `echo "a` and Allow execution of `echo b"`?\n  1. Yes\n  2. No\n› \n',
            '"unterminated error\nDo you want to proceed?\n  1. Yes\n  2. No\narg"\n› \n',
            "'unterminated error\nDo you want to proceed?\n  1. Yes\n  2. No\narg'\n› \n",
            "`unterminated error\nDo you want to proceed?\n  1. Yes\n  2. No\narg`\n› \n",
            '│ "unterminated error │\n│ Do you want to proceed? │\n│   1. Yes │\n│   2. No │\n│ arg" │\n│ › │\n',
            '"unterminated error\nModels & Quota\n↑/↓ Scroll · esc Close"\n› \n',
            '"unterminated error\nDo you want to proceed? (y/n)\narg"\n› \n',
            '"unterminated error\nDo you want to proceed?\n1. Yes\n2. No"\n› \n',
            '"unterminated error\nDo you want to proceed?\narg"\n› \n',
            '"err 1\nerr 2\nerr 3\nerr 4\nDo you want to proceed?\narg"\n› \n',
            '  "err 1\nerr 2\nerr 3\nerr 4\nerr 5\nDo you want to proceed?\narg"\n› \n',
            '"err 1\nerr 2\nerr 3\nerr 4\nerr 5\nDo you want to proceed?\narg" extra\n› \n',
            '"err 1\nerr 2\nerr 3\nerr 4\nerr 5\nAllow execution of /tmp/x.sh?\narg"\n› \n',
            '"err 1\nerr 2\nerr 3\nerr 4\nerr 5\nSelect a model: GPT-5\narg"\n› \n',
            'err: unterminated "Do you want to proceed?\n  echo foo"\n› \n',
            'err: "Do you want to\nproceed?\n  echo foo"\n› \n',
            'err: unterminated "x\nDo you want to\nproceed?"\n› \n',
            'err: unterminated "x\nAllow execution\nof /tmp/run.sh?"\n› \n',
            "err: missing `x\nDo you want to\nproceed?`\n› \n",
            '│ err: unterminated "x │\n│ Do you want to │\n│ proceed?" │\n│ › │\n',
            "Check 'my cmd' notes: Allow execution of foo bar' here\n› \n",
            "Check 'tests' notes: Allow execution of foo bar' here\n› \n",
            '• Ran python3 -c "print(\nDo you want to\nproceed?\n"more\n› \n',
            '"start\n' + "line\n" * 22 + 'Do you want to proceed?\nend"\n❯ \n',
            "Run 'my tests' first: Allow execution'\nof /tmp/run.sh?\n  1. Yes\n  2. No\n› \n",
            "Check 'my tests' notes: Do you want to'\nproceed?\n  1. Yes\n  2. No\n› \n",
            "Check 'my tests' notes: Do you want to proceed'\n(y/n)\n› \n",
            "Check 'my tests' notes: Allow execution of cmd' on host?\n› \n",
            "Check 'my tests' notes: Allow execution of users' script?\n› \n",
            "Check 'my tests' notes: Allow execution of cmd' on host?\n  1. Proceed\n  2. Cancel\n› \n",
            "Check 'my tests' notes: Allow execution of foo bar' here\n› \n",
            '"unterminated error\nline 2\nline 3\nline 4\nline 5\nDo you want to proceed?\narg"\n› \n',
            (
                '│ "unterminated error │\n│ line 2 │\n│ line 3 │\n│ line 4 │\n'
                '│ line 5 │\n│ Do you want to proceed? │\n│ arg" │\n│ › │\n'
            ),
            (
                '"unterminated error\nline 2\nline 3\nline 4\nline 5\n'
                'Do you want to proceed?\narg"\nctx 1\nctx 2\nctx 3\nctx 4\n1. Yes\n2. No\n› \n'
            ),
            '"unterminated error\nline 2\nline 3\nline 4\nline 5\nRun this\ncommand?\narg"\n› \n',
            '"unterminated error\nline 2\nline 3\nline 4\nline 5\nAllow\nexecution of /tmp/x?\narg"\n› \n',
            '"unterminated error\nline 2\nline 3\nline 4\nline 5\nModels &\nQuota\narg"\n› \n',
            "err: `unclosed and Allow execution of #`foo`?\n› \n",
            'err: `unclosed and Allow execution of "`foo`?\n› \n',
            "err: `unclosed and Allow execution of '`foo`?\n› \n",
            "err: 'unclosed and Allow execution of #'foo'?\n› \n",
            "Guide says 'See users' and admins' Do you want to proceed?\n› \n",
            "Guide says 'See users' and admins's Allow execution of cmd' here\n› \n",
            "Guide says 'See users' and admins' 'Do you want to proceed?\n› \n",
            "Check 'my tests' notes: Do you want to proceed?' now (y/n)\n› \n",
            "Check 'my tests' notes: Allow execution of foo bar?' here.\n  1. Yes\n  2. No\n› \n",
            "a'\nDo you want to proceed?\n› \n",
            "Guide says 'See users' prompt: Models & Quota' here\n› \n",
            "Run 'my tests' first: Allow running' /tmp/" + "a" * 100 + ".sh?\n› \n",
            "err: 'unclosed and allow running foo'\"bar\"?\n› \n",
            'err: `unclosed and allow running foo`"bar"?\n› \n',
            "err: \"unclosed and allow running foo\"'bar'?\n› \n",
            'err: "unterminated and allow running cmd" on host?\n› \n',
            "err: `unterminated and allow running cmd` on host?\n› \n",
            "err: 'unterminated and allow running cmd' on host?\n› \n",
            'err: "unterminated and Do you want to"\nproceed?\n› \n',
            "err: `unterminated and Do you want to`\nproceed?\n› \n",
            "err: 'unterminated and Do you want to'\nproceed?\n› \n",
            'err: "unterminated and Allow execution of cmd"?\n› \n',
            "err: `unclosed and Do you want to proceed? `note`\n› \n",
            'err: "unterminated and Do you want to"\nproceed?"\n› \n',
            'err: "Do you want to"\nproceed?"\n› \n',
            'err: "allow execution" of\n/tmp/x?"\n› \n',
            "err: 'Do you want to'\nproceed?'\n› \n",
            "err: `Do you want to`\nproceed?`\n› \n",
            '"unterminated error\nDo you want to\nproceed" ?\n› \n',
            '"unterminated error\nDo you want to\nproceed" (y/n)\n› \n',
            '"unterminated error\nallow\nexecution of /tmp/foo"?\n› \n',
            '"unterminated error\nRun this\ncommand" (y/n)\n› \n',
            "Models & Quota\nL2\nL3\nL4\nL5\nL6\n› 1. Model A\n  2. Model B\n› \n",
            "Allow execution of /tmp/x?\nL2\nL3\nL4\nL5\nL6\n› 1. Proceed\n  2. Cancel\n› \n",
            "Allow execution of /tmp/x?\nL2\nL3\nL4\nL5\nL6\n  1. Proceed\n  2. Cancel\n› \n",
            "Do you want to 'proceed?'\n› \n",
            'Do you want to "proceed?"\n› \n',
            "err: allow running 'cmd?'\n› \n",
            "allow execution of '/tmp/x?'\n› \n",
            "Check 'my tests' notes: Do you want to proceed?' please\n› 1. Confirm\n  2. Cancel\n› \n",
            'Do you want to "\nproceed?"\n› \n',
            "err: 'Allow execution of users' script?' now?\n› \n",
            'err: "Do you want to\nproceed?"\n› \n',
            'err: "Do you want to\nproceed" (y/n)\n› \n',
            "Do you want to\n'proceed?'\n› \n",
            'Do you want to\n"proceed?"\n› \n',
            "Do you want to\n`proceed?`\n› \n",
            'Do you want to\n"proceed?"\n  1. Yes\n  2. No\n› \n',
            "Do you want to\n'proceed?'\n  1. Yes\n  2. No\n› \n",
            'Allow running\n"/tmp/foo?"\n› \n',
            'allow execution of\n"/tmp/foo?"\n› \n',
            "Run this\n'command?'\n› \n",
            "Do you want to\n'make this edit?'\n› \n",
            'Do you want to\n"proceed" ?\n› \n',
            'Do you want to\n"proceed" (y/n)\n› \n',
            (
                "Select a model:\n› 1. Model A\n  2. Model B\n  3. Model C\n"
                "  4. Model D\n  5. Model E\n  6. Model F\n  7. Model G\n› \n"
            ),
            (
                "Models & Quota\n› 1. GPT-5.1\n  2. GPT-5.2\n  3. GPT-5.3\n"
                "  4. Claude Sonnet\n  5. Claude Opus\n  6. Gemini Pro\n  7. Gemini Flash\n› \n"
            ),
            "Models & Quota\nL2\nL3\nL4\nL5\nL6\n1. Item A\n2. Item B\n› \n",
            "Do you want to proceed?\n› Allow once\n› \n",
            "Allow execution of /tmp/x?\n› Trust this directory\n› \n",
            "Would you like to run?\n› 1. Allow for this session\n  2. Deny\n› \n",
            "Would you like to run?\n› 1. Allow once for this session\n  2. Deny\n› \n",
            "Allow execution of /tmp/x?\nL2\nL3\nL4\nL5\nL6\n  1. Trust this directory\n  2. Exit\n› \n",
            "```Do you want to proceed?\n  1. Yes\n  2. No\n› \n",
            "~~~Do you want to proceed?\n  1. Yes\n  2. No\n› \n",
            "```a\n```\n```Do you want to proceed?\n› \n",
            '"""\nDo you want to proceed?\n  1. Yes\n  2. No\n"""\n› \n',
            '"""\nDo you want to proceed?\n"""\n  1. Yes\n  2. No\n› \n',
            '"""\nDo you want to proceed?\n› \n',
            'err: "Do you want to\nproceed?" now\n(y/n)\n› \n',
            "'Do you want to proceed?' (y/n)\n› \n",
            "• See 'Do you want to proceed?'\n(y/n)\n› \n",
            "'a " + "users' " * 8000 + "Allow execution of foo?\n› \n",
        ],
    )
    def test_pre_check_blocks_modal_across_delimiter_and_quote_boundaries(self, pre: str) -> None:
        # Arrange
        pane_text = pre

        # Act
        _, reason = check_pre_capture(pane_text, "r9-block-delim")

        # Assert
        assert "modal" in reason

    @pytest.mark.parametrize(
        "pre",
        [
            "Here is ``Do you want to proceed? `yes` `` in markdown.\n❯ \n",
            "• Summary:\n  1. Yes, all tests pass.\n  2. No, no regressions found.\n› \n",
            "• Note: we do not allow execution of untrusted code.\n❯ \n",
            "Guide says 'See users' Guide: Do you want to proceed?' here.\n❯ \n",
            "~~~\nDo you want to proceed?\n~~~\n❯ \n",
            "• Example:\n~~~text\nDo you want to proceed?\n~~~\n❯ \n",
            "```text\nx = 1\nDo you want to proceed?\n```\n❯ \n",
            "users' note: 'Do you want to proceed?' here.\n❯ \n",
            "Note 'Do you want to proceed? yes' in docs.\n❯ \n",
            'The dialog asks "Do you want to\nproceed? yes" here.\n❯ \n',
            'The dialog asks "Do you want to\nproceed?" and "open\n❯ \n',
            'First "Do you want to\nproceed?" and "Models &\nQuota" here.\n❯ \n',
            "```text\n" + "line\n" * 22 + "Do you want to proceed?\n```\n❯ \n",
            '• Ran python3 -c "print(\n' + "line\n" * 22 + 'Do you want to\nproceed?"\n❯ \n',
            '• Ran python3 -c "print(\narg "tag"\nDo you want to\nproceed?"\n❯ \n',
            "• Ran python3 -c 'print(\narg 'my users' end'\nL1\nL2\nL3\nL4\nL5\nDo you want to\nproceed?'\n❯ \n",
            (
                "• Ran python3 -c 'print(\n\"Do you want to proceed?\" 'my users' end'\n"
                "L1\nL2\nL3\nL4\nL5\nDo you want to\nproceed?'\n❯ \n"
            ),
            "• Ran 'print(\nas'\nDo you want to\nproceed?'\n❯ \n",
            "Do you want to proceed?\n› Tell me the summary\n• Summary:\n"
            "  1. Yes, all tests pass.\n  2. No, no regressions found.\n› \n",
            "Note `Do you want a`b to proceed?` here.\n❯ \n",
            r"• Note: 'It\'s asking: Do you want to proceed?' here." + "\n❯ \n",
            "Guide says 'See users' guide: Do you want to proceed? yes' here.\n❯ \n",
            "Guide says 'See users' and admins' guide: Do you want to proceed?' here.\n❯ \n",
            "Guide says 'See users' and Admins' guide: Do you want to proceed?' here.\n❯ \n",
            "Guide says 'See users' and admins's guide: Do you want to proceed?' here.\n❯ \n",
            "The guide says 'See users' docs' here and foo?\n❯ \n",
            "Guide says 'See users' prompt: Do you want to proceed?' here.\n  See note.\n❯ \n",
            "users' " * 4000 + "\n› \n",
            "'a " * 4000 + "\n› \n",
            "```foo\n" * 4000 + "› \n",
            "~~~foo\n" * 4000 + "› \n",
            "a" + " " * 20000 + "X\n› \n",
            "─" * 20000 + "X\n› \n",
            "Note `Do you want to `` proceed?` here.\n❯ \n",
            "Note ''``Do you want to proceed?`` here.\n❯ \n",
            'note: "Do you want to\nproceed?" was logged earlier\n❯ \n',
            "See 'Do you want to proceed?' and 'ok' in docs.\n❯ \n",
            '• Note: \\\\"Do you want to proceed?\\\\"\n› \n',
            "• Note: \\\\'Do you want to proceed?\\\\'\n› \n",
            "• Note: \\\\`Do you want to proceed?`\n› \n",
            '• Note: path C:\\\\temp and "Do you want to proceed?"\n› \n',
            "• Check \\word outside quotes.\n❯ \n",
            "```a\n```\n```\nDo you want to proceed?\n```\n› \n",
            '    """Do you want to proceed?"""\n› \n',
            '    """\n    Do you want to proceed?\n    """\n› \n',
            "    '''Do you want to proceed?'''\n› \n",
            "› prompt\n  • Ran pytest\n  esc to interrupt\n",
        ],
    )
    def test_pre_check_allows_quoted_and_fenced_modal_examples_across_boundaries(self, pre: str) -> None:
        # Arrange
        pane_text = pre

        # Act
        ok, _ = check_pre_capture(pane_text, "r9-allow-delim")

        # Assert
        assert ok is True

    @pytest.mark.parametrize(
        ("pre", "post", "dispatch_id", "expected"),
        [
            (
                "› \n",
                "› [dispatch:r9-c1] Ack [dispatch-accepted:r9-c1]. Go [dispatch-end:r9-c1]\n"
                "Run 'tests' and Allow execution of flag='--all'?\n  1. Yes\n  2. No\n› \n",
                "r9-c1",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [dispatch:r9-m1] Ack [dispatch-accepted:r9-m1]. Go [dispatch-end:r9-m1]\n"
                'err: unterminated "x\nDo you want to proceed? (y/n)\n  echo foo"\n  1. Yes\n  2. No\n› \n',
                "r9-m1",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "• Summary:\n  1. Yes, all tests pass.\n  2. No, no regressions found.\n› \n",
                "• Summary:\n  1. Yes, all tests pass.\n  2. No, no regressions found.\n"
                "› [dispatch:r9-ny1] Follow up [dispatch-end:r9-ny1]\n  GPT-6.1 · /workspace/app\n",
                "r9-ny1",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "• Summary:\n  1. Yes, all tests pass.\n  2. No, no regressions found.\n› \n",
                "• Summary:\n  1. Yes, all tests pass.\n  2. No, no regressions found.\n"
                "› [dispatch:r9-nya] Follow up [dispatch-end:r9-nya]\n[dispatch-accepted:r9-nya]\n",
                "r9-nya",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "• Answers:\n  1. Yes\n  2. No\n› [dispatch:r9-ny3] Next [dispatch-end:r9-ny3]\n",
                "r9-ny3",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                "› [dispatch:r9-bt2] Go [dispatch-end:r9-bt2]\n[dispatch-accepted:r9-bt2]\n"
                "• Working\nHere is ``Do you want to proceed? `yes` `` in markdown.\n❯ \n",
                "r9-bt2",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r9-chip1]\n  esc to interrupt\n",
                "r9-chip1",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "> \n",
                "> ↑ 12 more lines [dispatch-end:r9-chip2]\n  esc to cancel\n",
                "r9-chip2",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "• Working\n› \n",
                "• Working\n› [Pasted text #1 +15 lines] [dispatch-end:r9-chip3]\n  tab to queue · esc to interrupt\n",
                "r9-chip3",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                "› [dispatch:r10-sq] Ack [dispatch-accepted:r10-sq]. Go [dispatch-end:r10-sq]\n"
                "Run 'see tests' and Allow execution of cmd'?\n  1. Yes\n  2. No\n› \n",
                "r10-sq",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [dispatch:r10-c0] Ack [dispatch-accepted:r10-c0]. Go [dispatch-end:r10-c0]\n"
                '"unterminated error\nDo you want to proceed?\n  1. Yes\n  2. No\narg"\n› \n',
                "r10-c0",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r10-p1]\n[dispatch-accepted:r10-p1]\n",
                "r10-p1",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r10-p2]\n[dispatch-accepted:r10-p2]\n• Ran git status\n",
                "r10-p2",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r10-p3]\n[dispatch-accepted:r10-p3]\n• Edited app.py\n",
                "r10-p3",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r10-p4]\n[dispatch-accepted:r10-p4]\nSure, checking now.\n",
                "r10-p4",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r10-p5]\n[dispatch-accepted:r10-p5]\n  esc to interrupt\n",
                "r10-p5",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r10-p6]\n"
                "[dispatch-accepted:r10-p6]\n• Ran git status\n› \n",
                "r10-p6",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "• Old [Pasted text #1 +3 lines]\n• Done\n• Done\n• Done\n• Done\n• Done\n• Done\n"
                "[dispatch-end:r10-p7]\n[dispatch-accepted:r10-p7]\n• Working\n› \n",
                "r10-p7",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "[dispatch-end:r10-p8]\n• Working\n"
                "› [Pasted text #1 +15 lines] [dispatch-end:r10-p8]\n[dispatch-accepted:r10-p8]\n",
                "r10-p8",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines]\n  tail 1\n  tail 2\n  tail 3\n  tail 4\n"
                "  [dispatch-end:r11-p1]\n[dispatch-accepted:r11-p1]\n• Ran pytest\n",
                "r11-p1",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines]\n  tail 1\n  tail 2\n  tail 3\n  tail 4\n"
                "  [dispatch-end:r11-p2]\n[dispatch-accepted:r11-p2]\n• Ran pytest\n› \n",
                "r11-p2",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "• Old [Pasted text #1 +3 lines]\n• Done\n[dispatch-end:r11-p3]\n"
                "[dispatch-accepted:r11-p3]\n• Working\n› \n",
                "r11-p3",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "• Old [Pasted text #1 +3 lines]\n[dispatch-end:r11-p4]\n[dispatch-accepted:r11-p4]\n• Working\n› \n",
                "r11-p4",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r11-p5]\n"
                "[dispatch-accepted:r11-p5]\n• Ran echo [dispatch-end:r11-p5]\n",
                "r11-p5",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r11-p6]\n"
                "[dispatch-accepted:r11-p6]\n• Ran echo [dispatch-end:r11-p6]\n› \n",
                "r11-p6",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [dispatch:r11-p7] Fix [Pasted text #1] [dispatch-end:r11-p7] [dispatch-accepted:r11-p7]\n",
                "r11-p7",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "[dispatch-end:r11-p8]\n• Working\n"
                "› [dispatch:r11-p8] body [dispatch-end:r11-p8][dispatch-accepted:r11-p8]\n",
                "r11-p8",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\n────────────────────\n"
                "[dispatch-end:r11-p9]\n[dispatch-accepted:r11-p9]\n• Working\n› \n",
                "r11-p9",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "Here is [Pasted text #1] note\n[dispatch-end:r11-p10]\n[dispatch-accepted:r11-p10]\n• Working\n› \n",
                "r11-p10",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "  [Pasted text #1 +15 lines]\n  tail\n[dispatch-end:r11-p11]\n[dispatch-accepted:r11-p11]\n› \n",
                "r11-p11",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r11-p12]"
                "[dispatch-accepted:r11-p12][dispatch-end:r11-p12]\n",
                "r11-p12",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r11-p13]\n"
                "[dispatch-end:r11-p13]\n[dispatch-accepted:r11-p13]\n",
                "r11-p13",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\n• Done\n"
                "[dispatch-end:r11-p14]\n[dispatch-accepted:r11-p14]\n• Working\n› \n",
                "r11-p14",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\nWorked for 5s\n"
                "[dispatch-end:r11-p15]\n[dispatch-accepted:r11-p15]\n• Working\n› \n",
                "r11-p15",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\n╰──────────────╯\n"
                "[dispatch-end:r11-p16]\n[dispatch-accepted:r11-p16]\n• Working\n› \n",
                "r11-p16",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "> ↑ 32 more lines\n  • Check tests first\n  [dispatch-end:r12-ida]\n[dispatch-accepted:r12-ida]\n",
                "r12-ida",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "> ↑ 32 more lines\n  ────────────────────\n  [dispatch-end:r12-idb]\n[dispatch-accepted:r12-idb]\n",
                "r12-idb",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "> ↑ 32 more lines\n  • Ran pytest\n  [dispatch-end:r12-idc]\n[dispatch-accepted:r12-idc]\n",
                "r12-idc",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "│ › ↑ 32 more lines         │\n│   • bullet in preview     │\n"
                "│   [dispatch-end:r12-idbx] │\n│ [dispatch-accepted:r12-idbx] │\n",
                "r12-idbx",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\n│ ─────────────── │\n"
                "[dispatch-end:r12-id1]\n[dispatch-accepted:r12-id1]\n• Working\n› \n",
                "r12-id1",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\n----------------\n"
                "[dispatch-end:r12-id2]\n[dispatch-accepted:r12-id2]\n• Working\n› \n",
                "r12-id2",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old task\nI finished checking that.\n"
                "[dispatch-end:r12-id3]\n[dispatch-accepted:r12-id3]\n• Working\n› \n",
                "r12-id3",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old\n› [dispatch:other] task\n"
                "[dispatch-end:r12-idh2]\n[dispatch-accepted:r12-idh2]\n• Working\n› \n",
                "r12-idh2",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "Status" + " " * 20000 + "OK\n",
                "r12-space-cls",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [dispatch:r12-dot] Body [dispatch-end:r12-dot]\n[dispatch-accepted:r12-dot]\n"
                "• Ran echo ok\n› \nmain · /workspace/app\n",
                "r12-dot",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\nBash(echo [dispatch-end:r12-stop1])\n[dispatch-accepted:r12-stop1]\n› \n",
                "r12-stop1",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\n• Echoed [dispatch-end:r12-stop2]\n[dispatch-accepted:r12-stop2]\n› \n",
                "r12-stop2",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\n› manual [dispatch-end:r12-stop3]\n[dispatch-accepted:r12-stop3]\n› \n",
                "r12-stop3",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "  [dispatch-end:r12-nochip]\n[dispatch-accepted:r12-nochip]\n› \n",
                "r12-nochip",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n"
                "  • Working on step 1\n› partial line [dispatch-end:chip-e1]\n",
                "chip-e1",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n  • Ran pytest\n› partial line [dispatch-end:chip-e2]\n",
                "chip-e2",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "• Done\n› \n",
                "› [dispatch:turn-1] old [dispatch-end:turn-1]\n"
                "[dispatch-accepted:turn-1]\n  • Checked 1 file\n"
                "› [Pasted text #1 +5 lines]\n  esc to interrupt\n",
                "turn-2",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\nOutput: [dispatch-end:r13-c2]\n[dispatch-accepted:r13-c2]\n› \n",
                "r13-c2",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› Draft 1\n• Working\n  • Checked\n› [dispatch:r13-eq] ok [dispatch-end:r13-eq]\n",
                "r13-eq",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines]\n  • Checked\n  › [dispatch-end:r13-m33]\n",
                "r13-m33",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [dispatch:d1] Go [dispatch-end:d1]\n[dispatch-accepted:d1]\n"
                "Do you want to proceed?\n› Yes\n  No\n› \n",
                "d1",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n• Done\n  › partial line [dispatch-end:r14-f3a]\n",
                "r14-f3a",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n  › partial line [dispatch-end:r14-f3b]\n",
                "r14-f3b",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n"
                "  › manual [dispatch-end:r14-f3c]\n[dispatch-accepted:r14-f3c]\n› \n",
                "r14-f3c",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n"
                "  Sure, done.\n  › [dispatch-end:r14-f3d]\n[dispatch-accepted:r14-f3d]\n",
                "r14-f3d",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "• Working stale\n› \n",
                "• Working stale\n› [Pasted text #1 +24 lines]\n  esc to interrupt\n",
                "r14-d4",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "• Working stale\n› [Pasted text #1 +24 lines]\n  esc to interrupt\n",
                "• Working stale\n› [Pasted text #1 +24 lines]\n  esc to interrupt\n",
                "r14-d4-neg",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "› [dispatch:r14-m33]\n  • Working\n│ › draft\n",
                "r14-m33",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                " │ › [Pasted text #1 +3 lines] old\n │ > manual [dispatch-end:r14-m37]\n[dispatch-accepted:r14-m37]\n",
                "r14-m37",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n• Done\n  > [dispatch-end:r15-f5a]\n",
                "r15-f5a",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n  • Working\n  > [dispatch-end:r15-f5b]\n",
                "r15-f5b",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +3 lines] old prompt\n  • Ran pytest\n  > [dispatch-end:r15-f5c]\n",
                "r15-f5c",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "> [Pasted text #1 +3 lines] old prompt\n  • Ran pytest\n  > [dispatch-end:r15-f5d]\n",
                "r15-f5d",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "• Working stale\n› \n  esc to interrupt\n",
                "• Working stale\n› [Pasted text #1 +24 lines]\n  esc to interrupt\n",
                "r15-f6a",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r15-echo1]\n"
                '[dispatch-accepted:r15-echo1]\n• Ran echo "[dispatch:r15-echo1]"\n› \n',
                "r15-echo1",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r15-echo2]\n"
                '[dispatch-accepted:r15-echo2]\n• Ran echo "[dispatch:r15-echo2]"\n',
                "r15-echo2",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines] [dispatch-end:r15-echo3]\n"
                "[dispatch-accepted:r15-echo3][dispatch:r15-echo3]\n• Working\n› \n",
                "r15-echo3",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "[dispatch:r15-bal][dispatch-accepted:r15-bal][dispatch-end:r15-bal]\n"
                "› [dispatch:r15-bal] body [dispatch-end:r15-bal]\n",
                "r15-bal",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                " │ > old [dispatch-end:r15-brk1]\n │ > draft\n",
                "r15-brk1",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "│> old [dispatch-end:r15-brk2]\n│  › nested\n",
                "r15-brk2",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "Select a model:\n  [dispatch:m190] run [dispatch-end:m190]\n"
                "› Do this task\nNo indented options here.\n› \n",
                "m190",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "[dispatch:r15-m312] first [dispatch-end:r15-m312]"
                "[dispatch-accepted:r15-m312][dispatch:r15-m312] unclosed\n",
                "r15-m312",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                'x = """path\\\\"""\nAllow execution of /tmp/x?\n"""\n› \n',
                "r16-2a",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                '"""\nAllow execution of /tmp/x?\nx = """\n› \n',
                "r16-2b",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "Select a model:\n"
                + "\n".join(f"  {i}. Model {i}" if i != 19 else "› 19. Model 19" for i in range(1, 20))
                + "\n",
                "r16-m19",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "Select a model:\n"
                + "\n".join(f"  {i}. Model {i}\n    desc A\n    desc B" for i in range(1, 7))
                + "\n› 7. Model 7\n",
                "r16-mml",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "In Codex you can Press up to edit your prompt.\n"
                "[dispatch:r16-qfp] Acknowledge [dispatch-end:r16-qfp]\n› \n",
                "r16-qfp",
                DeliveryState.AMBIGUOUS,
            ),
            (
                "› \n",
                "• Done\n"
                "› [dispatch:r16-ufp] Acknowledge this exact turn first by emitting "
                "[dispatch-accepted:r16-ufp]. Body [dispatch-end:r16-ufp]\n"
                "  ⏵⏵ accept edits on (shift+tab to cycle)\n",
                "r16-ufp",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                "Select a model:\n› GPT-5.1\n  GPT-5.2\n[dispatch:r16-exk] task [dispatch-end:r16-exk]\n› \n",
                "r16-exk",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› Select a model:\nSure, done.\n› [dispatch:r17-f1b] task [dispatch-end:r17-f1b]\n",
                "r17-f1b",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                "› Select a model:\nSure, done.\n› [dispatch:r17-f1c]\n",
                "r17-f1c",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "Select a model:\n› earlier\n  detail 1\nSure.\n› [dispatch:r17-f1d1]\n",
                "r17-f1d1",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "Select a model:\n› GPT-5.1\n  GPT-5.2\nEnter to confirm · Esc to exit\n› \n",
                "r17-f2a",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "Select a model:\n[dispatch:r17-f3b]\n"
                + ("  payload\n" * 80)
                + "[dispatch-end:r17-f3b]\n  Option 1\n› Option 2\n› \n",
                "r17-f3b",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "Select a model:\n› GPT-5.1\n  GPT-5.2\n↑/↓ to navigate\n› \n",
                "r18-f3-nav",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "Select a model:\n› GPT-5.1\n  GPT-5.2\n↑/↓ to navigate · Enter to confirm\n› \n",
                "r18-f3-both",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "Select a model:\n› GPT-5.1\n  GPT-5.2\nPress Esc to cancel\n› \n",
                "r18-f3-esc",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "Select a model:\n› GPT-5.1\n  GPT-5.2\n↑/↓ to navigate\n"
                "› [dispatch:r18-f3-un] task [dispatch-end:r18-f3-un]\n",
                "r18-f3-un",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [dispatch:r21-col] Go [dispatch-end:r21-col]\n[dispatch-accepted:r21-col]\n"
                '• Working\n• Note: "Select a model:", then pick.\n› \n',
                "r21-col",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [dispatch:r21-edit] Go [dispatch-end:r21-edit]\n[dispatch-accepted:r21-edit]\n"
                "Do you want to make this edit to src/app.py?\n  1. Yes\n  2. No\n",
                "r21-edit",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [dispatch:r21-stk] Go [dispatch-end:r21-stk]\n[dispatch-accepted:r21-stk]\n"
                'err: missing "a Do you want to proceed? ~~"s"~~\n› \n',
                "r21-stk",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [dispatch:r21-ph] Go [dispatch-end:r21-ph]\n[dispatch-accepted:r21-ph]\n"
                "Select a model:\n› 1. GPT-5\n  2. Claude\n› Ask Codex to do anything\n",
                "r21-ph",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [dispatch:r22-list] Go [dispatch-end:r22-list]\n[dispatch-accepted:r22-list]\n"
                "• Working\n• Findings:\n  1. Continue using the existing guard.\n"
                "  2. Allow retry when transient.\n› Ask Codex to do anything\n",
                "r22-list",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [dispatch:r22-run] Go [dispatch-end:r22-run]\n[dispatch-accepted:r22-run]\n"
                "Do you want to run pytest -q?\n1. Yes\n2. No\n",
                "r22-run",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [dispatch:r22-phyn] Go [dispatch-end:r22-phyn]\n[dispatch-accepted:r22-phyn]\n"
                "Run this command? [y/n]\n› Ask Codex to do anything\n",
                "r22-phyn",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› ↑ 12 more lines\n  Do you want to proceed?\n  [dispatch-end:r23-chip1]\n",
                "r23-chip1",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                "› [Pasted text #1 +15 lines]\n  Allow execution of /tmp/x?\n  [dispatch-end:r23-chip2]\n",
                "r23-chip2",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                "› ↑ 12 more lines\n  Do you want to proceed?\n"
                "  [dispatch-end:r23-chip3]\n[dispatch-accepted:r23-chip3]\n• Working\n› \n",
                "r23-chip3",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [dispatch:r23-allow] Go [dispatch-end:r23-allow]\n[dispatch-accepted:r23-allow]\n"
                "Do you want to overwrite app.py?\n1. Allow once\n2. Always allow\n› \n",
                "r23-allow",
                DeliveryState.BLOCKED_MODAL,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars] [dispatch-end:r24-csub]\n\n"
                "› [Pasted Content 500 chars] [dispatch-end:r24-csub]\n"
                "[dispatch-accepted:r24-csub]\n• Working\n› \n",
                "r24-csub",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars] [dispatch-end:r24-cact]\n\n"
                "› [Pasted Content 500 chars] [dispatch-end:r24-cact]\n"
                "[dispatch-accepted:r24-cact]\n• Working\n",
                "r24-cact",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars] [dispatch-end:r24-cuns]\n\n"
                "› [Pasted Content 500 chars] [dispatch-end:r24-cuns]\n",
                "r24-cuns",
                DeliveryState.UNSUBMITTED,
            ),
            (
                "› \n",
                "› ↑ 12 more lines\n  Do you want to proceed?\n  [dispatch-end:r24-cm1]\n\n"
                "› ↑ 12 more lines\n  Do you want to proceed?\n  [dispatch-end:r24-cm1]\n"
                "[dispatch-accepted:r24-cm1]\n• Working\n› \n",
                "r24-cm1",
                DeliveryState.SUBMITTED,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars] [dispatch-end:r24-dup1]\n"
                "[dispatch-accepted:r24-dup1]\n• Working\n"
                "› [Pasted Content 500 chars] [dispatch-end:r24-dup1]\n",
                "r24-dup1",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars] [dispatch-end:r24-cprom]\n\n"
                "› [Pasted Content 500 chars] [dispatch-end:r24-cprom]\n"
                "[dispatch-accepted:r24-cprom]\n",
                "r24-cprom",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars] [dispatch-end:r24-dup2]\n"
                "[dispatch-accepted:r24-dup2]\n• Working\n"
                "› [Pasted Content 500 chars] [dispatch-end:r24-dup2]\n"
                "[dispatch-accepted:r24-dup2]\n",
                "r24-dup2",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars] [dispatch-end:r24-dup3]\n"
                "[dispatch-accepted:r24-dup3]\n• Working\n"
                "› [Pasted Content 500 chars] [dispatch-end:r24-dup3]\n"
                "[dispatch-accepted:r24-dup3]\n• Working\n",
                "r24-dup3",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [dispatch:r25-ech1] Ack [dispatch-accepted:r25-ech1]. Go [dispatch-end:r25-ech1]\n"
                '[dispatch-accepted:r25-ech1]\n• Checked "[dispatch:r25-ech1]"\n',
                "r25-ech1",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [dispatch:r25-ech2] Ack [dispatch-accepted:r25-ech2]. Go [dispatch-end:r25-ech2]\n"
                '[dispatch-accepted:r25-ech2]\n• Ran echo "[dispatch:r25-ech2]"\n',
                "r25-ech2",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [dispatch:r25-ech3] first [dispatch-end:r25-ech3]"
                "[dispatch-accepted:r25-ech3][dispatch:r25-ech3] unclosed\n",
                "r25-ech3",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [dispatch:r25-adj][dispatch-end:r25-adj][dispatch-accepted:r25-adj]\n",
                "r25-adj",
                DeliveryState.ACCEPTED_ACTIVE,
            ),
            (
                "› \n",
                "› [dispatch:r25-dups] first [dispatch:r25-dups] second "
                "[dispatch-end:r25-dups][dispatch-accepted:r25-dups]\n",
                "r25-dups",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars]\n  [dispatch-end:r25-sh1]\n  [dispatch-end:r25-sh1]\n\n"
                "› [Pasted Content 500 chars] [dispatch-end:r25-sh1]\n",
                "r25-sh1",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars] [dispatch-end:r25-sh2] [dispatch-end:r25-sh2]\n\n"
                "› [Pasted Content 500 chars] [dispatch-end:r25-sh2]\n",
                "r25-sh2",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [dispatch-end:r25-rev][dispatch-accepted:r25-rev][dispatch:r25-rev]\n",
                "r25-rev",
                DeliveryState.PARTIAL_PASTE,
            ),
            (
                "› \n",
                "› [Pasted Content 500 chars]\n  [dispatch-end:r25-sh3]\n  [dispatch-end:r25-sh3]\n"
                "[dispatch-accepted:r25-sh3]\n• Working\n› \n",
                "r25-sh3",
                DeliveryState.AMBIGUOUS,
            ),
        ],
    )
    def test_classify_capture_respects_delimiter_active_chip_and_list_boundaries(
        self,
        pre: str,
        post: str,
        dispatch_id: str,
        expected: DeliveryState,
    ) -> None:
        # Arrange
        pre_capture = pre
        post_capture = post

        # Act
        verdict = classify_capture(pre_capture, post_capture, dispatch_id)

        # Assert
        assert verdict.state is expected

    def test_classify_paste_chip_with_active_esc_footer_marks_composer_unclean(self) -> None:
        # Arrange
        post = "› [Pasted text #1 +15 lines] [dispatch-end:r9-chipc]\n  esc to interrupt\n"

        # Act
        verdict = classify_capture("› \n", post, "r9-chipc")

        # Assert
        assert verdict.composer_clean is False

    @pytest.mark.parametrize(
        "pre",
        [
            "users' " * 4000 + "\n› \n",
            "'a " + "users' " * 4000 + "Allow execution of " * 2000 + "foo?\n› \n",
            "```foo\n" * 4000 + "› \n",
            "~~~foo\n" * 4000 + "› \n",
            "\n".join("`" * (1503 - i) for i in range(1500)) + "\n› \n",
            "a" + " " * 20000 + "X\n› \n",
            "─" * 20000 + "X\n› \n",
            "› prompt\n" + "  • Checked\n" * 4000 + "  esc to interrupt\n",
            "› prompt\n" + "  • Edited file.py\n" * 4000 + "  › dirty\n",
            "› prompt\n" + "  • Working\n" * 4000 + "  > bare\n",
            (r"\\`a" * 5000) + "\n› \n",
            '"a" ' * 15000 + "\n› \n",
            "'a' " * 15000 + "\n› \n",
            "``a`` " * 15000 + "\n› \n",
            ('"""Do you want to proceed?""" ' * 10000) + "\n› \n",
            " ".join("`" * (i + 2) for i in range(2500)) + "\n› \n",
            'a "b"\n' + " " * 40000 + "x\n› \n",
            'a "b"\nfoo [y/n]' + " " * 40000 + "x\n› \n",
            '"""\n"""' + " " * 40000 + "x\n› \n",
            '"""\n"""' + " " * 20000 + ")" + " " * 20000 + "x\n› \n",
            '"""' + ('\\""" ' * 8000) + '"""\n› \n',
            ("'a' " * 5000) + ('"""ok""" ' * 5000) + "\n› \n",
            "'''" + '"' + ('\\"' * 15000) + "X\n› \n",
            "'''" + (" 'x \"x' ''' y\" " * 4000) + "\n› \n",
            '"""' + (' \'x `x\' """ y` ' * 4000) + "\n› \n",
            '"""' + (' `x \'x` """ y\' ' * 4000) + "\n› \n",
            '"""' + (' "x \'x" """ y\' ' * 4000) + "\n› \n",
            (" " * 100000) + "X\n› \n",
            "Do you want to run" + " " * 50000 + "!\n› \n",
            "Do you want to run x" + " " * 50000 + "!\n› \n",
            "Do you want to make this edit to" + " " * 50000 + "!\n› \n",
            ("Do you want to run " + " " * 200) * 200 + "!\n› \n",
            "1. Yes," + " " * 25000 + "(" + "a" * 25000 + "!\n› \n",
            "1. Yes, " + "x" * 72 + " " * 25000 + "(" + "a" * 25000 + "!\n› \n",
            "› Yes" + " " * 50000 + "!\n› \n",
        ],
    )
    def test_pre_check_processes_adversarial_inputs_in_linear_time(self, pre: str) -> None:
        # Arrange
        pane_text = pre

        # Act
        t0 = time.perf_counter()
        _ = check_pre_capture(pane_text, "r12-perf")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Assert
        assert elapsed_ms < 4000.0

    def test_classify_capture_processes_excluded_overlap_spans_in_linear_time(self) -> None:
        # Arrange
        s_m, _, e_m = markers_for_id("r14-ex")
        post = "\n".join([s_m] * 15000 + [e_m] * 15000) + "\n› \n"

        # Act
        t0 = time.perf_counter()
        _ = classify_capture("› \n", post, "r14-ex")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Assert
        assert elapsed_ms < 4000.0

    @pytest.mark.parametrize("mode", ["balanced", "ordered", "chip_acc"])
    def test_classify_capture_processes_marker_triplets_in_linear_time(self, mode: str) -> None:
        # Arrange
        s_m, a_m, e_m = markers_for_id("r15-trip")
        if mode == "balanced":
            post = (f"{s_m} ok {a_m} {e_m}\n" * 8000) + f"› {s_m} unclosed\n"
        elif mode == "ordered":
            post = (a_m * 8000) + ((s_m + e_m) * 8000) + s_m + "\n› \n"
        else:
            post = (f"{a_m}\n" * 8000) + (f"› [Pasted Content] {e_m}\n" * 8000) + f"{a_m}\n• Working\n› \n"

        # Act
        t0 = time.perf_counter()
        _ = classify_capture("› \n", post, "r15-trip")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Assert
        assert elapsed_ms < 4000.0

    def test_classify_capture_processes_long_whitespace_line_in_linear_time(self) -> None:
        # Arrange
        post = "Status" + " " * 20000 + "OK\n"

        # Act
        t0 = time.perf_counter()
        _ = classify_capture("› \n", post, "r12-space-perf")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Assert
        assert elapsed_ms < 1500.0

    def test_classify_capture_processes_docstring_close_redos_in_linear_time(self) -> None:
        # Arrange
        post = '"""\n"""' + " " * 30000 + "x\n› \n"

        # Act
        t0 = time.perf_counter()
        _ = classify_capture("› \n", post, "r16-cls-redos")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Assert
        assert elapsed_ms < 4000.0

    @pytest.mark.parametrize(
        "pattern",
        [
            "^›" + " " * 20000 + "$",
            "^›" + "[ \t]" * 5000 + "X",
            "^›" + r"\s" * 10000 + "X",
            "^prompt>$",
        ],
    )
    def test_classify_capture_handles_long_composer_regex_whitespace_in_linear_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pattern: str,
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", pattern)

        # Act
        t0 = time.perf_counter()
        _ = classify_capture("› \n", "› \n", "r12-tws-perf")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Assert
        assert elapsed_ms < 4000.0

    def test_classify_accepts_collapsed_chip_under_custom_prompt_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*λ>[[:space:]]*$")
        post = "λ> [Pasted text #1 +15 lines] [dispatch-end:r11-custom]\n[dispatch-accepted:r11-custom]\n"

        # Act
        verdict = classify_capture("λ> \n", post, "r11-custom")

        # Assert
        assert verdict.state is DeliveryState.ACCEPTED_ACTIVE

    def test_classify_rejects_col0_custom_prompt_on_end_line_below_chip(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*λ>[[:space:]]*$")
        post = "λ> [Pasted text #1 +3 lines]\nλ> manual [dispatch-end:r12-stop4]\n[dispatch-accepted:r12-stop4]\nλ> \n"

        # Act
        verdict = classify_capture("λ> \n", post, "r12-stop4")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_pre_check_rejects_draft_below_more_indented_active_bullet(self) -> None:
        # Arrange
        pre = "  • Working\n› Draft:\n • Ran pytest\n"

        # Act
        ok, _ = check_pre_capture(pre, "r13-m154")

        # Assert
        assert ok is False

    def test_classify_preserves_nested_bullet_end_marker_below_more_indented_stop(self) -> None:
        # Arrange
        post = "› draft header\n  • Working\n • Ran pytest [dispatch-end:m154]\n > trailing line\n"

        # Act
        verdict = classify_capture("› \n", post, "m154")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_classify_stops_partial_chip_at_indented_custom_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*λ>[[:space:]]*$")
        post = (
            "λ> [Pasted text #1 +3 lines] old\n  λ> manual [dispatch-end:r14-cust]\n[dispatch-accepted:r14-cust]\nλ> \n"
        )

        # Act
        verdict = classify_capture("λ> \n", post, "r14-cust")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_classify_detects_partial_chip_below_checked_at_indented_custom_prompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*λ>[[:space:]]*$")
        post = "λ> [Pasted text #1 +3 lines] old\n  • Checked\n  λ> partial [dispatch-end:r15-cust-p]\n"

        # Act
        verdict = classify_capture("λ> \n", post, "r15-cust-p")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_classify_stops_custom_indented_prompt_above_generic_chip_prefix(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*λ>[[:space:]]*$")
        post = "↑ 12 more lines\n  λ> manual [dispatch-end:r15-cust2]\n[dispatch-accepted:r15-cust2]\nλ> \n"

        # Act
        verdict = classify_capture("λ> \n", post, "r15-cust2")

        # Assert
        assert verdict.state is DeliveryState.AMBIGUOUS

    def test_pre_check_returns_modal_reason_for_dirty_19_option_picker(self) -> None:
        # Arrange
        pre = (
            "Select a model:\n"
            + "\n".join(f"  {i}. Model {i}" if i != 19 else "› 19. Model 19" for i in range(1, 20))
            + "\n"
        )

        # Act
        _, reason = check_pre_capture(pre, "r16-m19-reason")

        # Assert
        assert reason == "blocked by active modal/approval UI"

    def test_pre_check_stops_modal_scan_on_custom_prompt_prefix(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*Codex>[[:space:]]*$")
        pre = "Codex> Select a model:\nSure, done.\nCodex> \n"

        # Act
        ok, _ = check_pre_capture(pre, "r16-cust-stop")

        # Assert
        assert ok is True

    def test_classify_stops_dirty_menu_expansion_at_prior_custom_prompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*Codex>[[:space:]]*$")
        post = (
            "Select a model:\n"
            "Codex> earlier request\n"
            + "\n".join(f"  detail {i}" for i in range(1, 21))
            + "\nCodex> [dispatch:r16-cdm]\n"
        )

        # Act
        verdict = classify_capture("Codex> \n", post, "r16-cdm")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    def test_pre_check_returns_unrelated_input_reason_for_dirty_composer_below_prior_prompt(self) -> None:
        # Arrange
        pre = "› Select a model:\nSure, done.\n› my draft\n"

        # Act
        _, reason = check_pre_capture(pre, "r17-f1a")

        # Assert
        assert reason == "composer already holds unrelated unsubmitted input"

    def test_pre_check_stops_dirty_composer_modal_scan_at_custom_prompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*Codex>[[:space:]]*$")
        pre = "Codex> Select a model:\nSure, done.\nCodex> my draft\n"

        # Act
        _, reason = check_pre_capture(pre, "r17-f1e")

        # Assert
        assert reason == "composer already holds unrelated unsubmitted input"

    def test_classify_stops_dirty_composer_modal_scan_at_custom_prompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        monkeypatch.setenv("ORCHESTRATION_COMPOSER_REGEX", r"^[[:space:]]*Codex>[[:space:]]*$")
        post = "Select a model:\nCodex> earlier request\n  detail 1\nCodex> [dispatch:r17-f1f]\n"

        # Act
        verdict = classify_capture("Codex> \n", post, "r17-f1f")

        # Assert
        assert verdict.state is DeliveryState.PARTIAL_PASTE

    @pytest.mark.parametrize(
        ("pre", "expected_ok"),
        [
            ("Do you want to proceed?\n› Yes\n  No\n› \n", False),
            ("Allow execution of /tmp/x?\n› Allow for this session\n  Deny\n› \n", False),
            ("> Yes, that works.\n› \n", True),
            (
                "Select a model:\n  1. A\n  2. B\n  3. C\n  4. D\n  5. E\n"
                "  6. F\n  7. G\n  8. H\n  9. I\n› 10. J\n  11. K\n› \n",
                False,
            ),
            (
                "Select a model:\n  GPT-5.1\n  GPT-5.2\n  Claude Sonnet\n"
                "  Claude Opus\n  Gemini Pro\n  Gemini Flash\n  Gemini Ultra\n› \n",
                False,
            ),
            (
                "Select a model:\n› 1. Model A\n    fast\n    cheap\n    good\n"
                "  2. Model B\n    slow\n    deep\n    smart\n› \n",
                False,
            ),
            ("Header info\n  opt A\n  opt B\nDo you want to proceed?\n› \n", False),
            ('err: "Do you want to\nproceed?" now\n\n(y/n)\n› \n', False),
            ('err: "Do you want to\nproceed?" now\nDetails line\n(y/n)\n› \n', False),
            ("• See 'Do you want to proceed?'\n\n(y/n)\n› \n", False),
            ("'Do you want to proceed?'(y/n)\n› \n", False),
            ('• Logged "Do you want to proceed?"\n• Tested "(y/n)"\n› \n', True),
            ('def f():\n    """Summary.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('Do you want to proceed? """\nnote\n"""\n› \n', False),
            ('• See `"""` in docs\nDo you want to proceed?\nNote `"""` again\n› \n', False),
            ('• """ok"""`Do you want to proceed?`\n› \n', True),
            ("• foo`s Do you want to proceed? `ok`\n› \n", False),
            ("› prior prompt\n• Ran pytest\n  • Working\n", True),
            ('x = """first\ndoc""" + """second\nDo you want to proceed?\n"""\n› \n', True),
            ('    """\n    1. Yes\n    """\n    Do you want to allow access to /x?\n    """\n› \n', False),
            ('"""Summary.\nDo you want to proceed?\n"""\n› \n', True),
            (
                'See `"""` note\n'
                + "filler\n" * 5
                + "Do you want to proceed?\n"
                + "  opt\n" * 5
                + 'End `"""` note\n› \n',
                False,
            ),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\n  Claude Opus\n› \n", False),
            ("Select a model:\n› GPT-5.1\n\n  GPT-5.2\n› \n", False),
            ("Select a model:\n  GPT-5.1\n  GPT-5.2\n› Claude Opus\n› \n", False),
            ("Select a model:\n› Explain this setting\nIt sets the model.\n› \n", True),
            ("Do you want to proceed?\n› Yes, proceed (y)\n  No (esc)\n› \n", False),
            ("Allow execution of /tmp/x?\n› Allow for all commands in this directory\n  Deny\n› \n", False),
            (
                "Select a model:\n"
                + "\n".join(f"  {i}. Model {i}" if i != 20 else "› 20. Model 20" for i in range(1, 21))
                + "\n› \n",
                False,
            ),
            ('Do you want to """proceed?"""\nAllow execution of /tmp/x?\nAllow running """/tmp/x?"""\n› \n', False),
            (
                'Do you want to """proceed?"""\nSelect a model:\n  GPT-5.1\n  GPT-5.2\n  """\n› \n',
                False,
            ),
            ('SyntaxError: unterminated """\nAllow execution of /tmp/x?\nNote: check """ handling\n› \n', False),
            ('def f():\n    """Summary with `"""` inside.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('"""\n' + "line\n" * 25 + 'Do you want to proceed?\n"""\n› \n', False),
            ('"""\n' + "line\n" * 22 + 'Do you want to proceed?\n"""\n› \n', True),
            ('Allow running "/tmp/long\nscript"\n?\n› \n', False),
            ('• Logged "Do you want to proceed?" and tested "(y/n)"\n› \n', True),
            ("• Logged 'Do you want to proceed?' and tested '[y/n]'\n› \n", True),
            ("• Logged `Do you want to proceed?` and tested `(y/n)`\n› \n", True),
            ("'Do you want to proceed?' (y/n)\"x\"\n› \n", False),
            ('"""Do you want to proceed?"""(y/n)\n› \n', False),
            ("Select a model:\n> prior answer\nOk.\n› \n", True),
            ('x = """path\\\\"""\nAllow execution of /tmp/x?\n"""\n› \n', False),
            ('"""\n\\"""\nDo you want to proceed?\n"""\n› \n', True),
            ('"""\nAllow execution of /tmp/x?\nx = """\n› \n', False),
            ('"""\nAllow execution of /tmp/x?\nreturn """\n› \n', False),
            ('def f():\n    """Explain `x = """` syntax.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('def f():\n    """Summary with \\""" inside.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('"""\nSelect a model:\n  info\n"""\n› \n', False),
            ('Do you want to "make\nthis edit"\n?\n› \n', False),
            (
                "Select a model:\n"
                + "\n".join(f"  {i}. Model {i}" if i != 19 else "› 19. Model 19" for i in range(1, 20))
                + "\n",
                False,
            ),
            (
                "Select a model:\n"
                + "\n".join(f"  {i}. Model {i}\n    desc A\n    desc B" for i in range(1, 7))
                + "\n› 7. Model 7\n",
                False,
            ),
            (
                "Select a model:\n" + "\n".join(f"  Model {i}" for i in range(1, 20)) + "\n› Model 20\n› \n",
                False,
            ),
            ("› Fix Select a model: bug\n  in settings.py\nFixed.\n› \n", True),
            ("› Implement Select a model: picker\n  Added model list:\n  - GPT-5.1\n  - GPT-5.2\n› \n", True),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\n↑/↓ Scroll\n› \n", False),
            ("• Done\n› \n  ⏵⏵ accept edits on (shift+tab to cycle)\n", True),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\n────────\n› \n", False),
            ("Select a model:\n• Ran pytest\n› Explain Select a model: bug\n  in settings.py\n› \n", True),
            ("Select a model:\n> old prompt\n› Explain Select a model: bug\n  in settings.py\n› \n", True),
            ("Select a model:\n› Explain this setting\n  in config.py\nIt sets the model.\n› \n", True),
            (
                'def f():\n    """Note `Do you want to proceed? """`?\n\n    Do you want to proceed?\n    """\n› \n',
                True,
            ),
            ('def f():\n    """Summary with \\\\\\""" inside.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('• Note: """Do you want to proceed? ``b``"""\n› \n', True),
            ("Select a model:\n  • bullet item\n› Explain Select a model: bug\n› \n", True),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\nEnter to confirm · Esc to exit\n› \n", False),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\nesc to cancel\n› \n", False),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\nType to filter models\n› \n", False),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\nPress Enter to select\n› \n", False),
            ("› Fix Select a model: bug\n  in ui.py\nType to filter models\n› \n", True),
            (
                "Select a model:\n"
                + "\n".join(f"  Option {i}\n    desc A\n    desc B\n" for i in range(1, 20))
                + "› Option 20\n  desc A\n  desc B\n› \n",
                False,
            ),
            ("Select a model:\n" + "  line\n" * 72 + "› Explain Select a model: bug\n  in a.py\n› \n", True),
            ('def f():\n    """Use `"""`-style docstrings.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('def f():\n    """Explain `"""`s here.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('def f():\n    """Note: see \'x = """\' below.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ("def f():\n    '''Note: see \"x = '''\" below.\n\n    Do you want to proceed?\n    '''\n› \n", True),
            (
                'def f():\n    """Note `Do you want to proceed?` and \'Allow running """\' ?\n\n'
                '    Do you want to proceed?\n    """\n› \n',
                True,
            ),
            ('print("""\n    Do you want to proceed?\n""")\n› \n', True),
            ('x = ("""\n    Do you want to proceed?\n""")\n› \n', True),
            ('Allow execution of ("""\n    harmless\n""")?\n› \n', False),
            ('"""\nAllow execution of \\""" # foo?\nDo you want to proceed?\n"""\n› \n', True),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\n↑/↓ to navigate\n› \n", False),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\n↑/↓ to navigate · Enter to confirm\n› \n", False),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\nUse ↑/↓ to navigate\n› \n", False),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\nPress ↑/↓ to navigate\n› \n", False),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\n↑ ↓ to move\n› \n", False),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\nPress Esc to cancel\n› \n", False),
            ("› Fix Select a model: bug\n  in ui.py\n↑/↓ to navigate\n› \n", True),
            ('items = ["""\n    Do you want to proceed?\n"""]\n› \n', True),
            ('print(repr("""\n    Do you want to proceed?\n"""))\n› \n', True),
            ('foo("""\n    Do you want to proceed?\n""",)\n› \n', True),
            ('def f():\n    """Use \'"""\'-style docstrings.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('def f():\n    """Explain \'"""\'s here.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ("def f():\n    '''Use \"'''\"-style docstrings.\n\n    Do you want to proceed?\n    '''\n› \n", True),
            ("def f():\n    '''Explain \"'''\"s here.\n\n    Do you want to proceed?\n    '''\n› \n", True),
            ('def f():\n    """Use **`"""`** for docstrings.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('def f():\n    """Use _`"""`_ for docstrings.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ("• Note: ('-Do you want to proceed?')\n› \n", True),
            ("• Note: 'a' ('-Do you want to proceed?')\n› \n", True),
            (
                'def f():\n    """Explain \'Check users\' notes """\'-style here.\n\n'
                '    Do you want to proceed?\n    """\n› \n',
                True,
            ),
            ("• Note: 'User's choice: Do you want to proceed?'\n› \n", True),
            ('foo"Do you want to proceed?"\n› \n', False),
            ("def f():\n    '''x = \"   \"+\"-'''\"\n\n    Do you want to proceed?\n    '''\n› \n", True),
            ('• Note: foo"" "-Do you want to proceed?"\n› \n', True),
            ('• Note: "foo"" "-Do you want to proceed?"\n› \n', True),
            ('• Note: x"" "" "(y/n)"\n', True),
            ("def f():\n    '''Note: a\"\" \"'''\"\n\n    Do you want to proceed?\n    '''\n› \n", True),
            ("• Note: foo'' '-Do you want to proceed?'\n› \n", True),
            ('def f():\n    """Use **\'"""\'** for docstrings.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('def f():\n    """Use _\'"""\'_ for docstrings.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ("def f():\n    '''Use **\"'''\"** for docstrings.\n\n    Do you want to proceed?\n    '''\n› \n", True),
            ("def f():\n    '''Use _\"'''\"_ for docstrings.\n\n    Do you want to proceed?\n    '''\n› \n", True),
            ('• Use **"Do you want to proceed?"** in UI\n› \n', True),
            ("• Use **'Do you want to proceed?'** in UI\n› \n", True),
            ('• Use _"Do you want to proceed?"_ in UI\n› \n', True),
            ("• Use _'Do you want to proceed?'_ in UI\n› \n", True),
            ('    d = {\n        "prompt": """\n        Do you want to proceed?\n        """,\n    }\n› \n', True),
            ('    d = {\n        prompt: """\n        Do you want to proceed?\n        """,\n    }\n› \n', True),
            ('    d = {"prompt": """\n        Do you want to proceed?\n    """}\n› \n', True),
            ('    foo({"a": """\n        Do you want to proceed?\n    """})\n› \n', True),
            ("• See 'users' notes ('-Do you want to proceed?')\n› \n", True),
            ("• See 'users' notes ('/tmp/Allow access?')\n› \n", True),
            ('def f():\n    """Explain \'\'\'"""\'\'\' here.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ("def f():\n    '''Explain \"\"\"'''\"\"\" here.\n\n    Do you want to proceed?\n    '''\n› \n", True),
            ('def f():\n    """Explain \'\'\' """ \'\'\' here.\n\n    Do you want to proceed?\n    """\n› \n', True),
            (
                "def f():\n    \"\"\"Explain '''''' '''\"\"\"''' here.\n\n"
                '    Do you want to proceed?\n    """\n› \n',
                True,
            ),
            ("err: missing `a Do you want to proceed? _`b`\n› \n", False),
            ("err: missing `a Do you want to proceed? \\\\`b`\n› \n", False),
            ("• Note: `Do you want to proceed? \\`b` here\n› \n", True),
            ("• Note: foo`a Do you want to proceed?`\n› \n", False),
            ('foo"bar "Do you want to proceed?"\n› \n', True),
            ('• Note: """"Do you want to proceed?"""\n› \n', True),
            ("Select a model:\n› GPT-5.1\n  GPT-5.2\nUse arrow keys to navigate\n› \n", False),
            ('foo("""\n    Do you want to proceed?\n""");\n› \n', True),
            ("err: missing `a Do you want to proceed? **`-v`**\n› \n", False),
            ("err: missing `a Do you want to proceed? (`-v`)\n› \n", False),
            ("err: missing `a Do you want to proceed? _`-b`\n› \n", False),
            ("err: missing `a Do you want to proceed? _`/b`\n› \n", False),
            ('err: unterminated "a Do you want to proceed? ("-v")\n› \n', False),
            ('err: unterminated "a Do you want to proceed? **"-v"**\n› \n', False),
            ('err: unterminated "a Do you want to proceed? _"/tmp/x"_\n› \n', False),
            ("err: unterminated 'a Do you want to proceed? ('-v')\n› \n", False),
            ("err: unterminated 'a Do you want to proceed? **'-v'**\n› \n", False),
            ("err: missing `a Do you want to proceed? _`s`\n› \n", False),
            ("err: missing `a Do you want to proceed? _`s\n› \n", False),
            ("err: missing `a Do you want to proceed? \\\\`s`\n› \n", False),
            ("err: missing `a Do you want to proceed? \\\\`s\n› \n", False),
            ("err: missing `a Do you want to proceed? (`s`)\n› \n", False),
            ('err: missing "a Do you want to proceed? _"s"\n› \n', False),
            ('err: missing "a Do you want to proceed? _"s\n› \n', False),
            ("err: missing 'a Do you want to proceed? ('s')\n› \n", False),
            ("err: missing 'a Do you want to proceed? ('s\n› \n", False),
            ('err: missing "a Do you want to proceed? ("./foo.sh")\n› \n', False),
            ('err: missing "a Do you want to proceed? (")")\n› \n', False),
            ("err: missing 'a Do you want to proceed? (',')\n› \n", False),
            ("• Note: `(`-style Do you want to proceed?`\n› \n", False),
            ('• Use **`"""`** and Do you want to proceed?`\n› \n', False),
            ('err: "unclosed foo"bar Do you want to proceed?"\n› \n', False),
            ("• Count `(`s and `)`s in `Do you want to proceed?`\n› \n", True),
            ('• Check arg.startswith("--foo=") and "-Do you want to proceed?"\n› \n', True),
            ("• Check arg.startswith('--foo=') and '-Do you want to proceed?'\n› \n", True),
            ("• See 'all users' notes ('-Do you want to proceed?')\n› \n", True),
            ("• See 'all users' notes ('/tmp/Allow access?')\n› \n", True),
            ("• Check 'my tests' results ('-Do you want to proceed?')\n› \n", True),
            ("• See 'all users' notes **'-Do you want to proceed?'**\n› \n", True),
            ("• See 'all users' notes _'-Do you want to proceed?'_\n› \n", True),
            ("• See 'all users' notes _'/tmp/Allow access?'_\n› \n", True),
            ("Check 'my tests' notes Allow execution of foo?' here\n› \n", False),
            ("Check 'my tests' results Do you want to proceed?' here\n› \n", False),
            ("• Note: 'Allow execution of users' script?' here.\n› \n", True),
            ('    {"prompt": """Select a model:\n    1. A\n    """}\n› \n', False),
            ('    """Select a model:\n    1. GPT-5\n    """\n› \n', False),
            ('    """Do you want to proceed? (y/n)\n    details\n    """\n› \n', False),
            ('• Note: "Do you want to proceed? `"foo"` here"\n› \n', True),
            ("• Note: 'Do you want to proceed? `'foo'` here'\n› \n", True),
            ('• Note: "Do you want to proceed? "" here"\n› \n', True),
            ("• Note: 'Do you want to proceed? '' here'\n› \n", True),
            ('def f():\n    """hi \\``x """``\n    Do you want to proceed?\n    """\n› \n', False),
            ('def f():\n    """hi ``x """\\``\n    Do you want to proceed?\n    """\n› \n', False),
            ('def f():\n    """hi \\`x """`\n    Do you want to proceed?\n    """\n› \n', False),
            ("• Note: ''Do you want to proceed?'\n› \n", False),
            ('• Note: ""Do you want to proceed?"\n› \n', False),
            ("• Note: ' ''Do you want to proceed?'\n› \n", False),
            ('• Note: " ""Do you want to proceed?"\n› \n', False),
            ('err: "foo_"" Do you want to proceed?"\n› \n', False),
            ("err: 'foo_'' Do you want to proceed?'\n› \n", False),
            ('def f():\n    """Explain \'\'\'\' """ \'\'\' here.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('"""\nAllow execution of /tmp/x?\nd = {"k": """\n› \n', False),
            ('• Note: "Do you want to proceed? foo" bar"\n› \n', True),
            ("• Note: `Do you want to proceed? foo` bar`\n› \n", True),
            ("def f():\n    '''Note: \"foo \"\" bar\" here.\n\n    Do you want to proceed?\n    '''\n› \n", True),
            ('• Note: "Select a model:", then pick.\n› \n', True),
            ("• Note: 'Select a model:', then pick.\n› \n", True),
            ("• Note: `Select a model:`, then pick.\n› \n", True),
            ('• Note: **"Select a model:"**\n› \n', True),
            ('• Note: _"Select a model:"_\n› \n', True),
            ('• Note: ~~"Select a model:"~~\n› \n', True),
            ('• Note: "Select a model:".\n› \n', True),
            ('• Note: "Do you want to proceed? *", next\n› \n', True),
            ('• Note: **"Do you want to proceed? *"**\n› \n', True),
            ('• Note: "Do you want to proceed? \\\\", next\n› \n', True),
            ('• Note: "Do you want to proceed? =", next\n› \n', True),
            ('• Note: "Do you want to proceed?"("-v")\n› \n', True),
            ('• Note: "Do you want to proceed? \'x\'"("-v")\n› \n', True),
            ("• Note: \"Do you want to proceed? ''\"-style\n› \n", True),
            ('• Note: ("-Do you want to proceed?")\n› \n', True),
            ('err: missing `a Do you want to proceed? "`-v`"\n› \n', False),
            ('err: missing "a Do you want to proceed? \'"-v"\'"\n› \n', False),
            ("err: missing 'a Do you want to proceed? \"'-v'\"'\n› \n", False),
            ('err: missing "a Do you want to proceed? |"-v"|"\n› \n', False),
            ('err: missing "a Do you want to proceed? ;"-v"\n› \n', False),
            ('err: missing "a Do you want to proceed? >"-v"\n› \n', False),
            ('err: missing "a Do you want to proceed? /"-v"\n› \n', False),
            ('err: missing "a Do you want to proceed? foo_"s"\n› \n', False),
            ("err: missing `a Do you want to proceed? foo_`s`\n› \n", False),
            ('err: missing "a Do you want to proceed? (", ")\n› \n', False),
            ('err: missing "a Do you want to proceed? !"("-v")\n› \n', False),
            ('err: missing "a Do you want to proceed? x""-v\n› \n', False),
            ('err: missing "a Do you want to proceed? -\'"-v"\n› \n', False),
            ('• Note: "a""-b Do you want to proceed?"\n› \n', True),
            ("err: missing `a Do you want to proceed? ~~`s`~~\n› \n", False),
            ("err: missing `a Do you want to proceed? ~~`-v`~~\n› \n", False),
            ('err: missing "a Do you want to proceed? ~~"s"~~\n› \n', False),
            ("err: missing 'a Do you want to proceed? ~~'s'~~\n› \n", False),
            ("err: missing `a Do you want to proceed? <`s`>\n› \n", False),
            ('err: missing "a Do you want to proceed? <"/tmp/x">"\n› \n', False),
            ('err: missing "a Do you want to proceed? <", ">"\n› \n', False),
            ("err: missing `a Do you want to proceed? <`, `>`\n› \n", False),
            ("err: missing `a Do you want to proceed? -`s`\n› \n", False),
            ("err: missing `a Do you want to proceed? /`s`\n› \n", False),
            ("Do you want to make this edit to src/app.py?\n1. Yes\n2. No\n", False),
            ("Do you want to run `pytest -q`?\n1. Yes\n2. No\n", False),
            ("Check 'my tests' notes: Allow execution of foo?' here\n› \n", False),
            ("Check 'my tests' notes - Do you want to proceed?' here\n› \n", False),
            ("Check 'my tests' results, Do you want to proceed?' here\n› \n", False),
            ("Check 'my tests' notes; Allow execution of foo?' here\n› \n", False),
            ("Check 'My tests' notes - Do you want to proceed?' here\n› \n", False),
            ("• See 'all users' notes \"'-Do you want to proceed?'\"\n› \n", True),
            ("• See 'all users' notes/'-Do you want to proceed?'\n› \n", True),
            ("• See 'all users' notes—'-Do you want to proceed?'\n› \n", True),
            ("• Ran python3 -c 'print(\nALL USERS' notes\nDo you want to\nproceed?'\n› \n", True),
            ("• Note: 'Allow execution of USERS' script?' here.\n› \n", True),
            ('• Note: "Do you want to proceed? ""-v"" here"\n› \n', True),
            ('• Note: "Do you want to proceed? ""foo"" here"\n› \n', True),
            ('• Note: "Do you want to proceed? foo"" here"\n› \n', True),
            ('• Note: "a " "" Do you want to proceed?"\n› \n', True),
            ("• Note: 'a ' '' Do you want to proceed?'\n› \n", True),
            ('• Note: ""foo Do you want to\nproceed?" here\n› \n', False),
            ('• Note: """Do you want to\nproceed?" here\n› \n', False),
            ('"Do you want to proceed?"""\n› \n', False),
            ('"a """ Do you want to proceed?"""\n› \n', True),
            ('err: "a Do you want to proceed? key:"-v"\n› \n', False),
            ("err: `a Do you want to proceed? key:`-v`\n› \n", False),
            ("err: 'a Do you want to proceed? key:'-v'\n› \n", False),
            ('err: "a Do you want to proceed? key="-v"\n› \n', False),
            ('err: "a Do you want to proceed? a,"-v"\n› \n', False),
            ('err: "a Do you want to proceed? ["-v"]\n› \n', False),
            ('err: "a Do you want to proceed? {"-v"}\n› \n', False),
            ('def f():\n    """Summary.\n    Do you want to proceed? (y/n) [default=n]\n    """\n› \n', False),
            ("Select a model:\n› 1. GPT-5\n  2. Claude\n› Ask Codex to do anything\n", False),
            ("Allow execution of /tmp/evil.sh?\n› Allow once\n  Deny\n› Type your message...\n", False),
            ("Do you want to proceed?\n1. Yes\n2. No\n› Ask Codex to do anything\n", False),
            ("Allow execution of /tmp/evil.sh?\n  Allow once\n  Enter to confirm\n› Type your message...\n", False),
            ("Models & Quota\n  gpt-5  80% left\n  esc Close\n› Ask Codex to do anything\n", False),
            ("Previous check completed.\n› Ask Codex to do anything\n", True),
            ("• Previous prompt showed Select a model:\n› Ask Codex to do anything\n", True),
            ('• Ran echo "unclosed\nDo you want to\nproceed\n?"\n› \n', False),
            ('def f():\n    """Explain \'"""\'S here.\n\n    Do you want to proceed?\n    """\n› \n', True),
            ('def g():\n    yield """\n    Do you want to proceed?\n    """\n› \n', True),
            ('def h():\n    return """\n    yield """\n    Do you want to proceed?\n    """\n› \n', True),
            ('• Note: "Do you want to proceed?"S here\n› \n', True),
            ("• Note: 'Do you want to proceed?'S here\n› \n", True),
            ("• Note: `Do you want to proceed?`S here\n› \n", True),
            ('• Note: "Allow execution of foo"-style in docs\n› \n', True),
            ("• Note: `Allow execution of foo`-style in docs\n› \n", True),
            ('("-v Do you want to proceed?")\n› \n', True),
            ("('-v Do you want to proceed?')\n› \n", True),
            ("Check 'See users' prompt: Do you want to proceed?'\n› \n", False),
            ("Check 'See users' prompt: Do you want to proceed? (y/n)' here.\n› \n", False),
            ("• Ran echo \"harmless 'inner\ntext\" Do you want to\nproceed?'\n› \n", False),
            ("› Option 1\n  Option 2\nDo you want to proceed?\n› \n", False),
            (
                "• Summary:\n  1. Yes, all tests pass.\n  2. No, no regressions found.\n› Ask Codex to do anything\n",
                True,
            ),
            (
                "• Findings:\n  1. Continue using the existing guard.\n  2. Allow retry when transient.\n"
                "› Type your message...\n",
                True,
            ),
            ("Do you want to make this edit to src/app.py?\n(y/n)\n› Type your message...\n", False),
            ("Run this command? [y/n]\n› Ask Codex to do anything\n", False),
            ("Continue? [y/n]\n› Ask Codex to do anything\n", False),
            ("Confirm deployment? (y/n)\n› Type your message...\n", False),
            ("Documentation steps:\n1. Select a model\n› Ask Codex to do anything\n", True),
            ('• Ran echo "arg1\nDo you want to proceed?"\n› \n', False),
            ("Do you want to run pytest -q?\n1. Yes\n2. No\n", False),
            ("Do you want to run git status?\n› \n", False),
            ("Do you want to run `cat a` and `cat b`?\n1. Yes\n2. No\n", False),
            ("Do you want to run this?\n› \n", False),
            ("Do you want to proceed with this command?\n1. Yes\n2. No\n", False),
            ("Do you want to overwrite src/app.py?\n1. Yes\n2. No\n", False),
            ("Do you want to overwrite src/app.py?\n1. Yes (y)\n2. No (n)\n", False),
            ("• Ran echo 'harmless \"inner\ntext' Do you want to\nproceed?\"\n› \n", False),
            ('• Ran echo `harmless "inner\ntext` Do you want to\nproceed?"\n› \n', False),
            ("• Ran echo `harmless 'inner\ntext` Do you want to\nproceed?'\n› \n", False),
            ("The dialog asks 'Do you want to\nproceed? yes' here.\n❯ \n", True),
            ("• Note: 'Allow access to\nfiles' here.\n› \n", True),
            ("• Note: 'Allow execution of\ntests' here.\n› \n", True),
            ("err: 'unclosed Allow access to\nfiles' here\n› \n", False),
            ('err: "unclosed\nDo you want to proceed?" here\n› \n', False),
            ('• Note: "Select a model:", 5" wide\n› \n', True),
            ('• Note: "Do you want to proceed? *", 5" wide\n› \n', True),
            ('• Note: **"Select a model:"**, 5" wide\n› \n', True),
            ('• Note: "Select a model:", foo""\n› \n', True),
            ("The guide says 'See users' prompt: Allow execution of users' script?' here.\n❯ \n", True),
            ("Check 'my tests' notes: Allow execution of users' script?' here\n› \n", False),
            ("• Note: 'Allow execution of users' notes 'hello' here.\n› \n", True),
            ('err: """\nDo you want to proceed?\n"""\n› \n', False),
            ('SyntaxError: invalid: """\nDo you want to proceed?\n"""\n› \n', False),
            ('err: missing "a Do you want to proceed? [", "]"\n› \n', False),
            ('err: missing "a Do you want to proceed? {", "}"\n› \n', False),
            ('• Note: "(", Do you want to proceed?"\n› \n', False),
            ("• Note: `[`-style Do you want to proceed?`\n› \n", False),
            ("• Note: `{`-style Do you want to proceed?`\n› \n", False),
            ('• Note: "Do you want to proceed? (ok)"("-v")\n› \n', True),
            ('• Note: "Do you want to proceed? [ok]"("-v")\n› \n', True),
            ('• Note: "Do you want to proceed? {ok}"("-v")\n› \n', True),
            ('• Note: "Do you want to proceed?!"("-v")\n› \n', True),
            ('• Note: "Do you want to proceed?."-style\n› \n', True),
            ('?"-v Do you want to proceed?"\n› \n', True),
            (')"-v Do you want to proceed?"\n› \n', True),
            ('• Note: |"Select a model:"|\n› \n', True),
            ('• Note: <"Select a model:">\n› \n', True),
            ('• Note: "Do you want to proceed? `"x`"\n› \n', True),
            ("• Note: 'Do you want to proceed? `'x`'\n› \n", True),
            ('• Note: "Do you want to proceed?"/help\n› \n', True),
            ("• Note: 'Do you want to proceed?'/help\n› \n", True),
            ("• Note: `Do you want to proceed?`/help\n› \n", True),
            ('• Note: "Do you want to proceed?"*2\n› \n', True),
            ("• Note: 'Do you want to proceed?'*2\n› \n", True),
            ("• Note: `Do you want to proceed?`*2\n› \n", True),
            ('• Note: _"Do you want to proceed?"_foo\n› \n', True),
            ("• Note: _'Do you want to proceed?'_foo\n› \n", True),
            ("• Note: _`Do you want to proceed?`_foo\n› \n", True),
            ('• Note: "Do you want to proceed?"\\\\n\n› \n', True),
            ("• Note: 'Do you want to proceed?'\\\\n\n› \n", True),
            ("• Note: `Do you want to proceed?`\\\\n\n› \n", True),
            ("• Note: 'Do you want to proceed?'('-v')\n› \n", True),
            ("• Note: `Do you want to proceed?`(`-v`)\n› \n", True),
            ('def f():\n    """Summary.\n    Do you want to proceed? (y/n) (default: n)\n    """\n› \n', False),
            ('def f():\n    """Summary.\n    Do you want to proceed? (y/n):\n    """\n› \n', False),
            ('• Note: "line 1\nDo you want to\nproceed?" more\n› \n', False),
            ("• Note: 'Allow execution of users' and ADMINS'S script?' here.\n› \n", True),
            ("err: missing 'a Do you want to proceed? (')')\n› \n", False),
            ("err: missing 'a Do you want to proceed? [']']\n› \n", False),
            ("print('Do you want to proceed? (')\n› \n", True),
            ("g('Do you want to proceed? (') + 'yes'\n› \n", True),
            (
                'def a():\n    """\n'
                + "    x\n" * 25
                + '    """\nDo you want to proceed?\ndef b():\n    """\n    y\n    """\n› \n',
                False,
            ),
            ('"""\n' + "x\n" * 25 + '"""\nDo you want to proceed?\n"""\ny\n"""\n› \n', False),
            ("Do you want to overwrite src/app.py?\n  1. Allow once\n  2. Always allow\n› \n", False),
            (
                "Do you want to overwrite src/app.py?\n"
                "  1. Allow once\n  2. Always allow\n› Ask Codex to do anything\n",
                False,
            ),
            ("Do you want to overwrite src/app.py?\n  1. Yes, overwrite\n  2. No, keep original\n› \n", False),
            ("Do you want to run ?\n› \n", False),
            ("Run this command ?\n› \n", False),
            ("• Note: 'Allow access to\nfiles' there.\n› \n", True),
            ("• Note: 'Allow access to\nfiles' now.\n› \n", True),
            ("The guide says 'See users' prompt: Do you want to proceed?' there.\n❯ \n", True),
            ("The guide says 'See users' prompt: Do you want to proceed?' now.\n❯ \n", True),
            ("• Note: 'Allow execution of \"inner\nb\" c' here.\n› \n", True),
            ('• Note: `Allow execution of "inner\nb" c` here.\n› \n', True),
            ("The guide says 'See users' prompt: check admins' note for Allow execution of foo?' here.\n❯ \n", True),
            ('• Note: "Do you want to proceed?"["-v"]\n› \n', True),
            ('• Note: "Do you want to proceed?"{"-v"}\n› \n', True),
            ('• Note: "a"[", Do you want to proceed?"]\n› \n', True),
            ('• Note: "a"(", Do you want to proceed?")\n› \n', True),
            ('"(", Do you want to proceed?"\n› \n', False),
            ('x="(", Do you want to proceed?"\n› \n', False),
            ('"("-style and "Do you want to proceed?"\n› \n', True),
            ("• Notes:\n  1. Yes\n• Logged: Do you want to overwrite src/app.py?\n› \n", True),
            (
                '"""\n'
                + "x\n" * 8
                + 'example = """Do you want to proceed?"""\n(y/n)\n'
                + "x\n" * 16
                + '"""\nDo you want to proceed?\n"""\ny\n"""\n› \n',
                False,
            ),
            (
                '"""\n'
                + "x\n" * 8
                + 'example = """Do you want to proceed?"""\n1. Yes\n'
                + "x\n" * 16
                + '"""\nDo you want to proceed?\n"""\ny\n"""\n› \n',
                False,
            ),
            (
                'def a():\n    """\n    example = """Do you want to proceed?"""\n    (y/n)\n'
                + "    x\n" * 30
                + '    """\nDo you want to proceed?\ndef b():\n    """\n    y\n    """\n› \n',
                False,
            ),
            ("Do you want to overwrite app.py?\n1. Yes, " + "x" * 60 + "\n› \n", False),
            ("Do you want to overwrite app.py?\n1. Yes, " + "x" * 75 + "\n› \n", True),
            ("Select a model:\n› Yes " + "x" * 60 + "\n› \n", False),
            ("Select a model:\n› Yes " + "x" * 75 + "\n› \n", True),
        ],
    )
    def test_pre_check_round14_modals_docstrings_and_indent_boundaries(
        self,
        pre: str,
        expected_ok: bool,
    ) -> None:
        # Arrange
        pane_text = pre

        # Act
        ok, _ = check_pre_capture(pane_text, "r14-pre")

        # Assert
        assert ok is expected_ok


_PBT_SEED = 20261115
_PBT_CASES = 3000


def _pbt_unsubmitted(rng: random.Random, directive: str, width: int, left_bar: str, right_pad: str, footer: str) -> str:
    s_head, _sep, e_tail = directive.rpartition(" [dispatch-end:")
    e_marker = f"[dispatch-end:{e_tail}"
    lines = rng.choice(
        [
            [f"› {directive}"],
            [f"› {s_head}", "", "> nested markdown note", e_marker],
            [f"› {s_head}", "• bullet in draft", e_marker],
        ]
    )
    body = _wrap_logical_lines(["• Done", "", *lines], width, left_bar, right_pad)
    return "\n".join([*body, footer])


def _pbt_submitted(
    rng: random.Random, directive: str, a_m: str, width: int, left_bar: str, right_pad: str, chrome: str, footer: str
) -> str:
    gap = rng.choice([[], [""]])
    verb = rng.choice(["• Working fresh", "• Ran pytest -q", "• Edited app.py"])
    body = _wrap_logical_lines(["• Stale", directive, *gap, a_m, verb], width, left_bar, right_pad)
    return "\n".join([*body, chrome, footer])


def _pbt_accepted_active(
    rng: random.Random, directive: str, a_m: str, width: int, left_bar: str, right_pad: str, footer_esc: str
) -> str:
    tail = rng.choice(["▸ Thinking", "• Working"])
    body = _wrap_logical_lines(["• Stale", directive, a_m, tail], width, left_bar, right_pad)
    return "\n".join([*body, footer_esc])


def _pbt_queued(
    rng: random.Random, directive: str, width: int, left_bar: str, right_pad: str, chrome: str, footer_esc: str
) -> str:
    extra = rng.choice([[], ["  Press ↑ to edit"]])
    body = _wrap_logical_lines(["• Working", "  Queued messages:", f"  {directive}"], width, left_bar, right_pad)
    return "\n".join([*body, chrome, *extra, footer_esc])


def _pbt_partial(s_m: str, e_m: str, width: int, left_bar: str, right_pad: str) -> str:
    body = _wrap_logical_lines([f"• Working {s_m}", "────", f"› {e_m}"], width, left_bar, right_pad)
    return "\n".join(body)


def _pbt_modal(left_bar: str, right_pad: str) -> str:
    return "\n".join(
        [f"{left_bar}Models & Quota{right_pad}", f"{left_bar}  esc Close{right_pad}", f"{left_bar}› {right_pad}"]
    )


def _pbt_ambiguous(directive: str, width: int, left_bar: str, right_pad: str, chrome: str, footer_gpt: str) -> str:
    body = _wrap_logical_lines(["• Stale", directive, "• Working"], width, left_bar, right_pad)
    return "\n".join([*body, chrome, footer_gpt])


def _build_property_case(rng: random.Random, idx: int) -> tuple[str, str, DeliveryState]:
    did = f"pbt-{idx}-{rng.randint(100, 999)}"
    s_m, a_m, e_m = markers_for_id(did)
    width = rng.randint(18, 96)
    left_bar = rng.choice(["", "  ", "│ ", "┃ "])
    right_pad = rng.choice(["", "   ", "  │", "  ┃"])
    outcome = rng.choice(list(DeliveryState))
    composer = rng.choice(["› ", "❯ ", "> ", "› Ask Codex to do anything"])
    directive = f"{s_m} Acknowledge first by emitting {a_m}. Body {e_m}"
    chrome = f"{left_bar}{composer}{right_pad}"
    footer_gpt = f"{left_bar}GPT-6.1 · /workspace/app{right_pad}"
    footer_esc = f"{left_bar}esc to interrupt{right_pad}"

    builders = {
        DeliveryState.UNSUBMITTED: lambda: _pbt_unsubmitted(rng, directive, width, left_bar, right_pad, footer_gpt),
        DeliveryState.SUBMITTED: lambda: _pbt_submitted(
            rng, directive, a_m, width, left_bar, right_pad, chrome, footer_gpt
        ),
        DeliveryState.ACCEPTED_ACTIVE: lambda: _pbt_accepted_active(
            rng, directive, a_m, width, left_bar, right_pad, footer_esc
        ),
        DeliveryState.QUEUED: lambda: _pbt_queued(rng, directive, width, left_bar, right_pad, chrome, footer_esc),
        DeliveryState.PARTIAL_PASTE: lambda: _pbt_partial(s_m, e_m, width, left_bar, right_pad),
        DeliveryState.BLOCKED_MODAL: lambda: _pbt_modal(left_bar, right_pad),
        DeliveryState.AMBIGUOUS: lambda: _pbt_ambiguous(directive, width, left_bar, right_pad, chrome, footer_gpt),
    }
    return did, builders[outcome](), outcome


class TestWrappedLayoutPropertyInvariants:
    def test_wrapped_layout_oracle_matches_3000_seeded_cases(self) -> None:
        # Arrange
        rng = random.Random(_PBT_SEED)
        pre = "• Done\n› \n"
        mismatches: list[tuple[int, DeliveryState, DeliveryState]] = []

        # Act
        for idx in range(_PBT_CASES):
            did, post, want = _build_property_case(rng, idx)
            got = classify_capture(pre, post, did).state
            if got is not want:
                mismatches.append((idx, want, got))

        # Assert
        assert mismatches == [], mismatches[:5]

    def test_tab_to_queue_footer_invariant_across_500_seeded_cases(self) -> None:
        # Arrange
        rng = random.Random(_PBT_SEED + 1)
        pre = "• Working\n› \n"
        missing_flags: list[int] = []

        # Act
        for idx in range(500):
            did = f"pbt-tab-{idx}"
            s_m, _, e_m = markers_for_id(did)
            width = rng.randint(24, 96)
            body = _wrap_logical_lines(["• Working", f"› {s_m} Hold {e_m}"], width, "", "")
            post = "\n".join([*body, "  tab to queue · esc to interrupt"])
            verdict = classify_capture(pre, post, did)
            if not (verdict.state is DeliveryState.UNSUBMITTED and verdict.tab_to_queue):
                missing_flags.append(idx)

        # Assert
        assert missing_flags == []

    @pytest.mark.parametrize("width", [18, 24, 56, 80])
    def test_wrapped_end_marker_at_critical_widths_stays_unsubmitted(self, width: int) -> None:
        # Arrange
        did = f"width-{width}-test"
        s_m, a_m, e_m = markers_for_id(did)
        post = "\n".join(_wrap_logical_lines(["• Done", "", f"› {s_m} {a_m} Task {e_m}"], width, "  ", "   "))

        # Act
        got = classify_capture("› \n", post, did).state

        # Assert
        assert got is DeliveryState.UNSUBMITTED


@REQUIRE_TMUX
class TestNativeTmuxPtyIntegration:
    @pytest.fixture(scope="class")
    def submitted_2enter_run(self, tmp_path_factory: pytest.TempPathFactory) -> tuple[LiveScenarioOutcome, Path]:
        run_dir = tmp_path_factory.mktemp("pty-submitted-2")
        outcome = _run_live_pty_scenario(run_dir, "submitted", "First paragraph.\n\nSecond paragraph.")
        return outcome, run_dir

    @pytest.fixture(scope="class")
    def submitted_1enter_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(
            tmp_path_factory.mktemp("pty-submitted-1"), "submitted_one_enter", "One-enter body."
        )

    @pytest.fixture(scope="class")
    def submitted_0enter_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(tmp_path_factory.mktemp("pty-submitted-0"), "submitted_zero_enter", "Auto body.")

    @pytest.fixture(scope="class")
    def queued_denied_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(
            tmp_path_factory.mktemp("pty-queued-deny"), "queued", "Queued task.", allow_queued=False
        )

    @pytest.fixture(scope="class")
    def queued_allowed_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(
            tmp_path_factory.mktemp("pty-queued-allow"), "queued", "Queued opt-in.", allow_queued=True
        )

    @pytest.fixture(scope="class")
    def partial_paste_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(tmp_path_factory.mktemp("pty-partial"), "partial_paste", "Split paste task.")

    @pytest.fixture(scope="class")
    def ambiguous_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(tmp_path_factory.mktemp("pty-ambiguous"), "ambiguous", "Ambiguous task.")

    @pytest.fixture(scope="class")
    def accepted_active_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(tmp_path_factory.mktemp("pty-active"), "accepted_active", "Accepted active task.")

    @pytest.fixture(scope="class")
    def tab_to_queue_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(tmp_path_factory.mktemp("pty-tabq"), "tab_to_queue", "Tab-to-queue task.")

    @pytest.fixture(scope="class")
    def blocked_modal_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(tmp_path_factory.mktemp("pty-modal"), "blocked_modal", "Never pasted.")

    @pytest.fixture(scope="class")
    def second_enter_modal_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(
            tmp_path_factory.mktemp("pty-2nd-modal"), "second_enter_blocked_modal", "Triggers modal on 2nd enter."
        )

    @pytest.fixture(scope="class")
    def submitted_chip_redraw_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> LiveScenarioOutcome:
        return _run_live_pty_scenario(
            tmp_path_factory.mktemp("pty-chip-redraw"),
            "submitted_chip_redraw",
            "Collapsed chip redraw.\n\nDo you want to proceed?",
        )

    def test_submitted_2enter_exits_zero(self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run

        # Act
        code = outcome.returncode

        # Assert
        assert code == 0

    def test_submitted_2enter_reports_submitted_state(
        self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]
    ) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run

        # Act
        state = _kv_get(outcome.stdout, "delivery_state")

        # Assert
        assert state == "submitted"

    def test_submitted_2enter_reports_two_enter_presses(
        self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]
    ) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run

        # Act
        presses = _kv_get(outcome.stdout, "enter_presses")

        # Assert
        assert presses == "2"

    def test_submitted_2enter_records_two_pty_enters(
        self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]
    ) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run

        # Act
        enters = outcome.enters

        # Assert
        assert enters == 2

    def test_submitted_2enter_preserves_verify_first_capture(
        self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]
    ) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run
        evidence_dir = Path(_kv_get(outcome.stdout, "evidence_dir"))

        # Act
        exists = (evidence_dir / "verify-first" / "post-1.txt").is_file()

        # Assert
        assert exists is True

    def test_submitted_2enter_preserves_verify_second_capture(
        self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]
    ) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run
        evidence_dir = Path(_kv_get(outcome.stdout, "evidence_dir"))

        # Act
        exists = (evidence_dir / "verify-second" / "post-1.txt").is_file()

        # Assert
        assert exists is True

    def test_multiparagraph_paste_starts_with_csi_200_bracket(
        self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]
    ) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run

        # Act
        starts_bracket = outcome.raw_bytes.startswith(b"\x1b[200~")

        # Assert
        assert starts_bracket is True

    def test_multiparagraph_paste_ends_bracket_before_enter_cr(
        self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]
    ) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run

        # Act
        has_end = b"\x1b[201~\r" in outcome.raw_bytes

        # Assert
        assert has_end is True

    def test_multiparagraph_paste_converts_lf_to_cr_inside_bracket(
        self, submitted_2enter_run: tuple[LiveScenarioOutcome, Path]
    ) -> None:
        # Arrange
        outcome, _ = submitted_2enter_run

        # Act
        pasted = outcome.pastes[0]

        # Assert
        assert "First paragraph.\r\rSecond paragraph." in pasted

    def test_submitted_chip_redraw_exits_zero(self, submitted_chip_redraw_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = submitted_chip_redraw_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 0

    def test_submitted_chip_redraw_reports_submitted_state(
        self, submitted_chip_redraw_outcome: LiveScenarioOutcome
    ) -> None:
        # Arrange
        outcome = submitted_chip_redraw_outcome

        # Act
        state = _kv_get(outcome.stdout, "delivery_state")

        # Assert
        assert state == "submitted"

    def test_submitted_chip_redraw_sends_recovery_enter(
        self, submitted_chip_redraw_outcome: LiveScenarioOutcome
    ) -> None:
        # Arrange
        outcome = submitted_chip_redraw_outcome

        # Act
        presses = _kv_get(outcome.stdout, "enter_presses")

        # Assert
        assert presses == "2"

    def test_submitted_1enter_exits_zero(self, submitted_1enter_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = submitted_1enter_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 0

    def test_submitted_1enter_records_single_enter(self, submitted_1enter_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = submitted_1enter_outcome

        # Act
        presses = _kv_get(outcome.stdout, "enter_presses")

        # Assert
        assert presses == "1"

    def test_submitted_0enter_exits_zero(self, submitted_0enter_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = submitted_0enter_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 0

    def test_submitted_0enter_auto_submits_without_enter(self, submitted_0enter_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = submitted_0enter_outcome

        # Act
        presses = _kv_get(outcome.stdout, "enter_presses")

        # Assert
        assert presses == "0"

    def test_accepted_active_exits_nonzero_with_single_enter(
        self, accepted_active_outcome: LiveScenarioOutcome
    ) -> None:
        # Arrange
        outcome = accepted_active_outcome

        # Act
        enters = outcome.enters

        # Assert
        assert enters == 1

    def test_accepted_active_exits_one(self, accepted_active_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = accepted_active_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 1

    def test_tab_to_queue_blocks_second_enter(self, tab_to_queue_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = tab_to_queue_outcome

        # Act
        enters = outcome.enters

        # Assert
        assert enters == 1

    def test_tab_to_queue_exits_one(self, tab_to_queue_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = tab_to_queue_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 1

    def test_queued_default_exits_one(self, queued_denied_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = queued_denied_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 1

    def test_queued_default_presses_enter_once(self, queued_denied_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = queued_denied_outcome

        # Act
        enters = outcome.enters

        # Assert
        assert enters == 1

    def test_queued_opt_in_exits_zero(self, queued_allowed_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = queued_allowed_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 0

    def test_queued_opt_in_reports_queued_state(self, queued_allowed_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = queued_allowed_outcome

        # Act
        state = _kv_get(outcome.stdout, "delivery_state")

        # Assert
        assert state == "queued"

    def test_partial_paste_exits_one(self, partial_paste_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = partial_paste_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 1

    def test_partial_paste_does_not_press_second_enter(self, partial_paste_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = partial_paste_outcome

        # Act
        enters = outcome.enters

        # Assert
        assert enters == 1

    def test_ambiguous_exits_one(self, ambiguous_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = ambiguous_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 1

    def test_ambiguous_does_not_press_second_enter(self, ambiguous_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = ambiguous_outcome

        # Act
        enters = outcome.enters

        # Assert
        assert enters == 1

    def test_blocked_modal_exits_75(self, blocked_modal_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = blocked_modal_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 75

    def test_blocked_modal_pastes_zero_bytes(self, blocked_modal_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = blocked_modal_outcome

        # Act
        raw = outcome.raw_bytes

        # Assert
        assert raw == b""

    def test_second_enter_blocked_modal_exits_75(self, second_enter_modal_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = second_enter_modal_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 75

    def test_second_enter_blocked_modal_reports_delivery_state(
        self, second_enter_modal_outcome: LiveScenarioOutcome
    ) -> None:
        # Arrange
        outcome = second_enter_modal_outcome

        # Act
        stderr = outcome.stderr

        # Assert
        assert "delivery_state=blocked_modal" in stderr

    def test_second_enter_blocked_modal_sent_two_enters(self, second_enter_modal_outcome: LiveScenarioOutcome) -> None:
        # Arrange
        outcome = second_enter_modal_outcome

        # Act
        enters = outcome.enters

        # Assert
        assert enters == 2

    @pytest.mark.parametrize("tool", TOOLS)
    def test_all_four_skills_submit_end_to_end(self, tmp_path: Path, tool: str) -> None:
        # Arrange
        body = f"End-to-end check for {tool}."

        # Act
        outcome = _run_live_pty_scenario(tmp_path, "submitted_one_enter", body, tool=tool)

        # Assert
        assert _kv_get(outcome.stdout, "delivery_state") == "submitted"

    def test_standalone_copied_skill_dir_outside_repo_submits_cleanly(self, tmp_path: Path) -> None:
        # Arrange
        standalone_skill = tmp_path / "standalone" / "orchestrate-codex"
        shutil.copytree(REPO_ROOT / "skills/orchestrate-codex", standalone_skill)
        standalone_script = standalone_skill / "scripts/submit_followup.sh"

        # Act
        outcome = _run_live_pty_scenario(
            tmp_path,
            "submitted_one_enter",
            "Standalone skill copy delivery.",
            script_override=standalone_script,
        )

        # Assert
        assert _kv_get(outcome.stdout, "delivery_state") == "submitted"


def _run_invalid_dispatch_id_probe(tmp_path: Path, bad_id: str) -> subprocess.CompletedProcess[str]:
    with _isolated_tmux_dir(tmp_path, prefix="orch-bad-", extra_env={"ORCHESTRATION_DISPATCH_ID": bad_id}) as env:
        followup = tmp_path / "followup.txt"
        followup.write_text("Body.\n", encoding="utf-8")
        session = f"orch-bad-{uuid.uuid4().hex[:6]}"
        subprocess.run(
            [TMUX or "tmux", "-f", "/dev/null", "new-session", "-d", "-s", session, "sleep 5"], env=env, check=True
        )
        script = REPO_ROOT / "skills/orchestrate-codex/scripts/submit_followup.sh"
        return subprocess.run(
            [str(script), session, str(followup)], env=env, capture_output=True, text=True, check=False
        )


class TestHarnessAndWrapperContracts:
    def test_pty_harness_handles_split_bracketed_paste_without_false_enter(self, tmp_path: Path) -> None:
        # Arrange
        harness = tmp_path / "harness.py"
        log_path = tmp_path / "events.json"
        harness.write_text(PTY_HARNESS, encoding="utf-8")
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            ["python3", "-B", str(harness), "submitted_zero_enter", str(log_path)],
            stdin=slave_fd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(slave_fd)

        # Act
        try:
            _wait_for_events_json(log_path, min_rendered=0)
            os.write(master_fd, b"\x1b[200~First\r")
            time.sleep(0.05)
            os.write(master_fd, b"\rSecond [dispatch:chunk-1][dispatch-end:chunk-1]\x1b[20")
            time.sleep(0.05)
            os.write(master_fd, b"1~")
            payload = _wait_for_events_json(log_path, min_rendered=1)
        finally:
            os.close(master_fd)
            proc.wait(timeout=5)

        # Assert
        assert int(payload["enters"]) == 0  # type: ignore[call-overload]

    @REQUIRE_TMUX
    @pytest.mark.parametrize("bad_id", ["-bad", "../escape", "bad]id"])
    def test_submit_followup_rejects_invalid_dispatch_id(self, tmp_path: Path, bad_id: str) -> None:
        # Arrange
        candidate_id = bad_id

        # Act
        res = _run_invalid_dispatch_id_probe(tmp_path, candidate_id)

        # Assert
        assert res.returncode == 2

    @pytest.mark.parametrize("bad_id", ["-bad", "../escape", "bad]id"])
    def test_verify_dispatch_rejects_invalid_dispatch_id(self, tmp_path: Path, bad_id: str) -> None:
        # Arrange
        pre = tmp_path / "pre.txt"
        post = tmp_path / "post.txt"
        pre.write_text("› \n", encoding="utf-8")
        post.write_text("› \n", encoding="utf-8")
        script = REPO_ROOT / "skills/orchestrate-codex/scripts/verify_dispatch.sh"

        # Act
        res = subprocess.run(
            [str(script), "-", bad_id, str(pre), str(post)], capture_output=True, text=True, check=False
        )

        # Assert
        assert res.returncode == 2


@dataclass(frozen=True)
class SigtermOutcome:
    """Observed process termination, Enter presses, and leftover secrets on SIGTERM."""

    returncode: int
    enter_count: int
    leftover_directives: int


def _run_failed_paste_cleanup(tmp_path: Path) -> tuple[str, str, list[Path]]:
    real_tmux = TMUX or "tmux"
    shim_src = (
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "paste-buffer" ]]; then exit 1; fi\n'
        f'exec {shlex.quote(real_tmux)} -f /dev/null "$@"\n'
    )
    did = f"trap-{uuid.uuid4().hex[:6]}"
    with _isolated_tmux_dir(
        tmp_path,
        prefix="orch-trap-",
        extra_env={"ORCHESTRATION_DISPATCH_ID": did},
        shim_script=shim_src,
    ) as env:
        followup = tmp_path / "followup.txt"
        followup.write_text("Sensitive body.\n", encoding="utf-8")
        session = f"orch-trap-{uuid.uuid4().hex[:6]}"
        subprocess.run(
            [real_tmux, "-f", "/dev/null", "new-session", "-d", "-s", session, "printf '› \\n'; sleep 5"],
            env=env,
            check=True,
        )
        script = REPO_ROOT / "skills/orchestrate-codex/scripts/submit_followup.sh"
        subprocess.run([str(script), session, str(followup)], env=env, capture_output=True, text=True, check=False)
        buffers = subprocess.run(
            [real_tmux, "-f", "/dev/null", "list-buffers"], env=env, capture_output=True, text=True, check=False
        )
        leftovers = list(Path(env["TMPDIR"]).rglob("directive.txt"))
        return did, buffers.stdout, leftovers


def _run_sigterm_during_paste(tmp_path: Path) -> SigtermOutcome:
    pasting_flag = tmp_path / "pasting.flag"
    enters_file = tmp_path / "enters.txt"
    run_tmp = tmp_path / "run_tmp"
    run_tmp.mkdir(mode=0o700, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    shim = bin_dir / "tmux"
    shim.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import sys, time",
                "from pathlib import Path",
                "cmd = sys.argv[1] if len(sys.argv) > 1 else ''",
                "if cmd in ('has-session', 'load-buffer', 'delete-buffer'):",
                "    sys.exit(0)",
                "if cmd == 'list-panes':",
                "    print('%0')",
                "    sys.exit(0)",
                "if cmd == 'paste-buffer':",
                f"    Path({str(pasting_flag)!r}).touch()",
                "    time.sleep(1.0)",
                "    sys.exit(0)",
                "if cmd == 'send-keys':",
                f"    with open({str(enters_file)!r}, 'a', encoding='utf-8') as fh:",
                "        fh.write('enter\\n')",
                "    sys.exit(0)",
                "print('• Done\\n› ')",
                "sys.exit(0)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    followup = tmp_path / "followup.txt"
    followup.write_text("Abort on SIGTERM.\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "TMUX"} | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "TMPDIR": str(run_tmp),
        "ORCHESTRATION_DISPATCH_ID": "sigterm-001",
    }
    script = REPO_ROOT / "skills/orchestrate-codex/scripts/submit_followup.sh"
    proc = subprocess.Popen(
        [str(script), "fake-session", str(followup)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not (pasting_flag.exists() and list(run_tmp.rglob("directive.txt"))):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise TimeoutError("timed out waiting for paste-buffer start")
            time.sleep(0.01)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    leftover = len(list(run_tmp.rglob("directive.txt")))
    enters = len(enters_file.read_text(encoding="utf-8").splitlines()) if enters_file.exists() else 0
    return SigtermOutcome(returncode=rc, enter_count=enters, leftover_directives=leftover)


class TestTrapAndRecoveryContracts:
    @pytest.fixture(scope="class")
    def failed_paste_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str, list[Path]]:
        return _run_failed_paste_cleanup(tmp_path_factory.mktemp("failed-paste"))

    @pytest.fixture(scope="class")
    def sigterm_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> SigtermOutcome:
        return _run_sigterm_during_paste(tmp_path_factory.mktemp("sigterm"))

    @REQUIRE_TMUX
    def test_failed_paste_buffer_cleans_dispatch_buffer_from_tmux(
        self, failed_paste_outcome: tuple[str, str, list[Path]]
    ) -> None:
        # Arrange
        did, buffers_stdout, _leftovers = failed_paste_outcome

        # Act
        retained = did in buffers_stdout

        # Assert
        assert retained is False

    @REQUIRE_TMUX
    def test_failed_paste_buffer_cleans_plaintext_directive_file(
        self, failed_paste_outcome: tuple[str, str, list[Path]]
    ) -> None:
        # Arrange
        _did, _buffers_stdout, leftovers = failed_paste_outcome

        # Act
        remaining = len(leftovers)

        # Assert
        assert remaining == 0

    def test_sigterm_during_paste_exits_143(self, sigterm_outcome: SigtermOutcome) -> None:
        # Arrange
        outcome = sigterm_outcome

        # Act
        code = outcome.returncode

        # Assert
        assert code == 143

    def test_sigterm_during_paste_sends_zero_enters(self, sigterm_outcome: SigtermOutcome) -> None:
        # Arrange
        outcome = sigterm_outcome

        # Act
        enters = outcome.enter_count

        # Assert
        assert enters == 0

    def test_sigterm_during_paste_cleans_directive_file(self, sigterm_outcome: SigtermOutcome) -> None:
        # Arrange
        outcome = sigterm_outcome

        # Act
        leftovers = outcome.leftover_directives

        # Assert
        assert leftovers == 0

    def test_verify_dispatch_accepts_leading_hyphen_relative_capture_path(self, tmp_path: Path) -> None:
        # Arrange
        did = "hyphen-cap-001"
        s_m, a_m, e_m = markers_for_id(did)
        (tmp_path / "-pre.txt").write_text("› \n", encoding="utf-8")
        (tmp_path / "-post.txt").write_text(f"{s_m} ok {e_m}\n{a_m}\n• Working\n› \n", encoding="utf-8")
        script = REPO_ROOT / "skills/orchestrate-codex/scripts/verify_dispatch.sh"

        # Act
        res = subprocess.run(
            [str(script), "-", did, "-pre.txt", "-post.txt"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

        # Assert
        assert res.returncode == 0

    @pytest.fixture(scope="class")
    def verify_usage_exit2_outcome(self, tmp_path_factory: pytest.TempPathFactory) -> tuple[int, int]:
        tmp_path = tmp_path_factory.mktemp("vexit2")
        skill_dir = tmp_path / "skill" / "orchestrate-codex"
        shutil.copytree(REPO_ROOT / "skills/orchestrate-codex", skill_dir)
        (skill_dir / "scripts/verify_dispatch.sh").write_text("#!/usr/bin/env bash\necho bad >&2\nexit 2\n")
        (skill_dir / "scripts/verify_dispatch.sh").chmod(0o755)
        did = "vexit2-001"
        state_file = tmp_path / "phase.txt"
        state_file.write_text("pre", encoding="utf-8")
        enters_file = tmp_path / "enters.txt"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(mode=0o700)
        shim = bin_dir / "tmux"
        shim.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys",
                    "from pathlib import Path",
                    f"state_file = Path({str(state_file)!r})",
                    f"enters_file = Path({str(enters_file)!r})",
                    "cmd = sys.argv[1] if len(sys.argv) > 1 else ''",
                    "if cmd in ('has-session', 'load-buffer', 'paste-buffer', 'delete-buffer'):",
                    "    sys.exit(0)",
                    "if cmd == 'list-panes':",
                    "    print('%0')",
                    "    sys.exit(0)",
                    "if cmd == 'send-keys':",
                    "    cur_e = int(enters_file.read_text(encoding='utf-8')) if enters_file.exists() else 0",
                    "    enters_file.write_text(str(cur_e + 1), encoding='utf-8')",
                    "    sys.exit(0)",
                    "cur = state_file.read_text(encoding='utf-8')",
                    f"did = {did!r}",
                    "if cur == 'pre':",
                    "    state_file.write_text('pasted', encoding='utf-8')",
                    "    print('• Done\\n› ')",
                    "else:",
                    "    print(f'• Done\\n› [dispatch:{did}] Task [dispatch-end:{did}]')",
                    "sys.exit(0)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        followup = tmp_path / "followup.txt"
        followup.write_text("Verify usage error propagation.\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "TMUX"} | {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
            "ORCHESTRATION_DISPATCH_ID": did,
            "ORCHESTRATION_PREPARE_ATTEMPTS": "2",
            "ORCHESTRATION_PREPARE_DELAY_SECONDS": "0",
        }
        res = subprocess.run(
            [str(skill_dir / "scripts/submit_followup.sh"), "fake-session", str(followup)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        enters = int(enters_file.read_text(encoding="utf-8")) if enters_file.exists() else 0
        return res.returncode, enters

    def test_submit_followup_propagates_verify_usage_exit_2(self, verify_usage_exit2_outcome: tuple[int, int]) -> None:
        # Arrange
        code, _enters = verify_usage_exit2_outcome

        # Act
        status = code

        # Assert
        assert status == 2

    def test_submit_followup_verify_usage_exit_2_sends_single_enter(
        self, verify_usage_exit2_outcome: tuple[int, int]
    ) -> None:
        # Arrange
        _code, enters = verify_usage_exit2_outcome

        # Act
        press_count = enters

        # Assert
        assert press_count == 1

    def test_after_second_enter_late_submitted_exits_zero(self, tmp_path: Path) -> None:
        # Arrange
        standalone_skill = tmp_path / "skill" / "orchestrate-codex"
        shutil.copytree(REPO_ROOT / "skills/orchestrate-codex", standalone_skill)
        did = "late-sub-001"
        state_file = tmp_path / "phase.txt"
        state_file.write_text("pre", encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(mode=0o700)
        shim = bin_dir / "tmux"
        shim.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import os, sys",
                    "from pathlib import Path",
                    f"state_file = Path({str(state_file)!r})",
                    "cmd = sys.argv[1] if len(sys.argv) > 1 else ''",
                    "if cmd in ('has-session', 'load-buffer', 'paste-buffer', 'delete-buffer'):",
                    "    sys.exit(0)",
                    "if cmd == 'list-panes':",
                    "    print('%0')",
                    "    sys.exit(0)",
                    "if cmd == 'send-keys':",
                    "    cur = state_file.read_text(encoding='utf-8')",
                    "    state_file.write_text('enter1' if cur == 'pasted' else 'enter2', encoding='utf-8')",
                    "    sys.exit(0)",
                    "if cmd == 'capture-pane':",
                    "    cur = state_file.read_text(encoding='utf-8')",
                    "    vdir = os.environ.get('ORCHESTRATION_VERIFY_DIR', '')",
                    f"    did = {did!r}",
                    "    if cur == 'pre':",
                    "        state_file.write_text('pasted', encoding='utf-8')",
                    "        print('• Done\\n› ')",
                    "    elif cur == 'pasted' or cur == 'enter1':",
                    "        print(f'• Done\\n› [dispatch:{did}] Task [dispatch-end:{did}]')",
                    "    elif vdir.endswith('/verify-second'):",
                    "        acc = f'[dispatch-accepted:{did}]'",
                    "        print(f'[dispatch:{did}] Task [dispatch-end:{did}]\\n{acc}\\n▸ Thinking')",
                    "    else:",
                    "        acc = f'[dispatch-accepted:{did}]'",
                    "        print(f'[dispatch:{did}] Task [dispatch-end:{did}]\\n{acc}\\n• Working\\n› ')",
                    "    sys.exit(0)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        followup = tmp_path / "followup.txt"
        followup.write_text("Late verify body.\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "TMUX"} | {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
            "ORCHESTRATION_DISPATCH_ID": did,
            "ORCHESTRATION_PREPARE_ATTEMPTS": "2",
            "ORCHESTRATION_PREPARE_DELAY_SECONDS": "0",
            "ORCHESTRATION_VERIFY_ATTEMPTS": "1",
            "ORCHESTRATION_VERIFY_DELAY_SECONDS": "0",
        }

        # Act
        res = subprocess.run(
            [str(standalone_skill / "scripts/submit_followup.sh"), "fake-session", str(followup)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        # Assert
        assert res.returncode == 0

    def test_verify_dispatch_exits_2_when_tmux_session_missing(self, tmp_path: Path) -> None:
        # Arrange
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(mode=0o700)
        shim = bin_dir / "tmux"
        shim.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)
        pre = tmp_path / "pre.txt"
        pre.write_text("› \n", encoding="utf-8")
        script = REPO_ROOT / "skills/orchestrate-codex/scripts/verify_dispatch.sh"
        env = os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}

        # Act
        res = subprocess.run(
            [str(script), "missing-sess", "r13-miss-001", str(pre)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        # Assert
        assert res.returncode == 2

    def test_verify_dispatch_does_not_create_evidence_dir_when_session_missing(self, tmp_path: Path) -> None:
        # Arrange
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(mode=0o700)
        shim = bin_dir / "tmux"
        shim.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)
        pre = tmp_path / "pre.txt"
        pre.write_text("› \n", encoding="utf-8")
        ev_dir = tmp_path / "never-created-evidence"
        script = REPO_ROOT / "skills/orchestrate-codex/scripts/verify_dispatch.sh"
        env = os.environ | {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "ORCHESTRATION_VERIFY_DIR": str(ev_dir),
        }

        # Act
        _ = subprocess.run(
            [str(script), "missing-sess", "r14-miss-002", str(pre)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        # Assert
        assert not ev_dir.exists()
