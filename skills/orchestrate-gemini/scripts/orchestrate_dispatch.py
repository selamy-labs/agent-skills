#!/usr/bin/env python3
"""Wrap-aware, region-aware classification for interactive tmux dispatches."""

from __future__ import annotations

import argparse
import json
import os
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

DEFAULT_ACTIVITY_PATTERN = (
    r"^[ \t]*([*]|•|●|○|⏺|◉|▸|⣷|⣯|⣟|⡿|⢿|⣻|⣽|⣾)[ \t]+"
    r"(Working|Thinking|Thought|Generating|Checked|Running|Ran|Edited|Updated|Explored|Tool|Bash|Read|Edit|Write|Search|Fetch|Exec)"
    r"|^[ \t]*(Tool|Bash|Read|Edit|Write|Search|Fetch|Exec)[(: \t]"
)
DEFAULT_COMPOSER_PATTERN = r"^[ \t]*(?:│[ \t]*)?(›|❯|>)[ \t]*$"
DEFAULT_QUEUED_PATTERN = (
    r"(?:^[ \t]*(?:[↳•●⏳][ \t]*)?Queued(?:[ \t]+messages?)?(?::|\s*\(\d+\))?[ \t]*(?:\[dispatch|$)"
    r"|^[ \t]*(?:[↳•●⏳·][ \t]*)?Press[ \t]+(?:↑|up)[ \t]+to[ \t]+edit(?:[ \t]+queued(?:[ \t]+messages?)?)?[ \t]*$)"
)

_POSIX_CLASS_MAP = {
    "[:space:]": r" \t\r\n\f\v",
    "[:blank:]": r" \t",
    "[:digit:]": r"0-9",
    "[:alnum:]": r"A-Za-z0-9",
    "[:alpha:]": r"A-Za-z",
    "[:upper:]": r"A-Z",
    "[:lower:]": r"a-z",
    "[:punct:]": r"!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~",
    "[:xdigit:]": r"0-9A-Fa-f",
    "[:word:]": r"A-Za-z0-9_",
    "[:cntrl:]": r"\x00-\x1f\x7f",
    "[:graph:]": r"\x21-\x7e",
    "[:print:]": r"\x20-\x7e",
}

PLACEHOLDER_COMPOSER_RE = re.compile(
    r"^[ \t]*(?:[│┃][ \t]*)?(?:›|❯|>)[ \t]+"
    r"(?:Ask Codex to do anything|Type your message(?:\.\.\.|…)?|"
    r"Send a message(?:\.\.\.|…)?|Ask anything(?:\.\.\.|…)?)[ \t]*$"
)
TUI_PROMPT_RE = re.compile(r"^(?:›|❯|>)(?:[ \t]|$)")
UNICODE_PROMPT_RE = re.compile(r"^(?:›|❯)(?:[ \t]|$)")
DIVIDER_LINE_RE = re.compile(r"^[ \t]*(?:[│┃─━═╭╰┌└┐┘┼┴┬╮╯\-][ \t]*){3,}$")
BOX_BORDER_RE = re.compile(r"^[ \t]*[╭┌╰└┼├┤┬┴─━═]{3}[─━═╭┌╮┐╰└╯┘┼├┤┬┴ \t]*$")
COMPOSER_RULE_RE = re.compile(r"^[ \t]*[─━═╭┌├┬╮┐┤┼]{8}[─━═╭┌├┬╮┐┤┼]*[ \t]*$")
_TIER_BADGE_PAT = r"(?-i:[A-Z][A-Za-z0-9_.-]{2,31}[ \t]+\((?:Carbon|Neon|Argon|Hydrogen)\))$"
FOOTER_CHROME_RE = re.compile(
    r"(?i)^[ \t]*(?:\?[ \t]+for[ \t]+shortcuts|⚠[ \t]*\d+[ \t]+warning|"
    r"esc[ \t]+to[ \t]+(?:interrupt|cancel|stop)|tab[ \t]+to[ \t]+queue|"
    r"press[ \t]+(?:↑|up)[ \t]+to[ \t]+edit|"
    r"(?:[⏵⏸▸]+[ \t]*)?(?:accept[ \t-]+edits|bypass[ \t]+permissions|plan[ \t]+mode)|"
    r"shift\+tab|ctrl\+[a-z]|no[ \t]+sandbox|\d+%[ \t]+context[ \t]+left|"
    + _TIER_BADGE_PAT
    + r"|GPT-(?:\d|OSS\b)|Gemini[ \t]+\d|Claude[ \t])"
)
_STATUS_DOT_FOOTER_RE = re.compile(r"(?i)(?<![ \t])[ \t]+·[ \t]+(?:/|~/|\d+%|" + _TIER_BADGE_PAT + r")")
ACTIVE_STOP_RE = re.compile(
    r"^(?:Worked[ \t]+for[ \t]+\d+[smh]\b"
    r"|[•●○⏺◉▸*✻✽✢✳✶][ \t]+(?:(?:Working|Thinking|Generating|Brewing)(?:[ \t]*(?:$|[.(•·…]))|Running\b)"
    r"|[\u2800-\u28ff][ \t]+\S)"
)
TRANSCRIPT_STOP_RE = re.compile(
    r"^(?:Worked[ \t]+for[ \t]+\d+[smh]\b"
    r"|(?:[•●○⏺◉▸*✻✽✢✳✶][ \t]+(?:(?:Working|Thinking|Generating|Brewing)(?:[ \t]*(?:$|[.(•·…]))"
    r"|(?:Thought|Checked|Running|Ran|Edited|Updated|Explored)\b)"
    r"|(?:[•●○⏺◉▸*][ \t]+)?(?:Tool|Bash|Read|Edit|Write|Search|Fetch|Exec)\("
    r"|[\u2800-\u28ff][ \t]+\S))"
)
_CHIP_PREFIX_RE = re.compile(r"^(?:\[Pasted Content|\[Pasted text|↑[ \t]+\d+[ \t]+more lines)")
_BULLET_PREFIX_RE = re.compile(r"^(?:[•●○⏺◉▸✻✽✢✳✶]|[-*+][ \t])")
MENU_OPTION_CURSOR_RE = re.compile(r"^(?:›|❯|>)[ \t]*[1-9]\d*\.[ \t]+\S")
_MODAL_ARG_SPAN = r"(?>[ \t]+)[^\n? \t](?:[^\n?]{0,198}[^\n? \t])?"
_MODAL_JOINED_PATTERN = (
    r"Models\s*&\s*Quota|"
    r"Run this command(?>[ \t]*)(?:\?|\[y/n\]|\(y/n\))|"
    r"Allow (?:execution|running|access)\b[^\n?]{0,240}\?|"
    r"(?-i:\bAllow)[ \t]+(?:execution|running|access)[ \t]+(?:of|to)\b|"
    rf"Do you want to (?:proceed|make this edit(?:(?>[ \t]+)to{_MODAL_ARG_SPAN})?"
    rf"|run(?:{_MODAL_ARG_SPAN})?)(?>[ \t]*)(?:\?|\[y/n\]|\(y/n\))|"
    r"Select (?:a )?model:|↑/↓ Scroll|\besc Close\b"
)
_BUTTON_LABEL = (
    r"(?:Allow once|Always allow|Allow for this session|Trust this directory|Requesting permission(?:\.\.\.|…)?|"
    r"Select (?:a )?model|Enter to (?:select|confirm))"
    r"[ \t]*(?:\([A-Za-z0-9 _-]+\)[ \t]*)?$"
)
_CURSOR_CHOICE_LABEL = (
    r"(?:Yes|No|Allow|Always|Proceed|Confirm|Cancel|Continue|Reject|Deny|Trust this directory)\b[^\n]*$"
)
_UNNUMBERED_CURSOR_CHOICE_RE = re.compile(
    r"(?i)^(?:[›❯][ \t]*(?:Yes|No|Allow|Always|Proceed|Confirm|Cancel|Continue|Reject|Deny)"
    r"\b(?:(?!.{72,}[^\n \t])[^\n]*)?"
    r"|>[ \t]*(?:Yes|No|Allow|Always|Proceed|Confirm|Cancel|Continue|Reject|Deny)\b"
    r"(?:(?>[ \t]+)(?:once|always|all|for(?>[ \t]+)this(?>[ \t]+)session|this(?>[ \t]+)\w+))?"
    r"(?:(?>[ \t]*)\([A-Za-z0-9/_ -]{1,40}\))?)(?>[ \t]*)$"
)
_MODAL_CURSOR_BUTTON_RE = re.compile(
    rf"(?i)^(?:(?:[›❯>][ \t]*(?:[1-9]\d*\.[ \t]+)?)?{_BUTTON_LABEL}|[›❯>][ \t]*[1-9]\d*\.[ \t]+{_CURSOR_CHOICE_LABEL})"
)
_MODAL_NUMBERED_BUTTON_RE = re.compile(rf"(?i)^[1-9]\d*\.[ \t]+{_BUTTON_LABEL}")
_DO_YOU_WANT_Q_RE = re.compile(r"(?i)\bDo you want to [^\n?]{1,200}\?")
_NUMBERED_CONFIRM_CHOICE_RE = re.compile(
    r"(?i)^[1-9]\d*\.(?>[ \t]+)(?:"
    r"(?:Yes|No),(?>[ \t]+)(?!.{72,}[^\n \t])[^\n \t][^\n]*"
    r"|(?:(?:Yes|No)"
    r"|Allow(?:(?>[ \t]+)(?:once(?:(?>[ \t]+)for(?>[ \t]+)this(?>[ \t]+)session)?"
    r"|always|all|for(?>[ \t]+)this(?>[ \t]+)session))?"
    r"|Always(?:(?>[ \t]+)allow)?"
    r"|Don't(?>[ \t]+)allow|Do(?>[ \t]+)not(?>[ \t]+)allow"
    r"|Proceed|Confirm|Cancel|Continue|Reject|Deny|Trust(?>[ \t]+)this(?>[ \t]+)directory"
    r")(?>[ \t]*)(?:\([A-Za-z0-9/_ -]{1,40}\)(?>[ \t]*))?)$"
)
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?:(`{3,})[^`\n]*|(~{3,})([^\n]*))$")
_FENCE_CLOSE_SCAN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$")
_OPEN_PREFIX = r"(?:^|(?<![A-Za-z0-9]))"
_HARD_CLOSE_SUFFIX = r"(?=$|[\s)\]}])"
_AFTER_EMPH_SUFFIX = r"(?=$|[\s.,;:!?)\]}])"
_PUNCT_CLOSE_SUFFIX = rf"(?:[.,;:!?]+{_HARD_CLOSE_SUFFIX}|(?:[*_~]+|[|>]){_AFTER_EMPH_SUFFIX})"
_EMPTY_DQUOTE_AT_RE = re.compile(r'(?:^|(?<![A-Za-z0-9_"]))"[ \t]*"(?!\w|")')
_DQUOTE_OPEN_RE = re.compile(r'(?:^|(?<![A-Za-z0-9"]))"(?=[^\s"])')
_EMPTY_SQUOTE_AT_RE = re.compile(r"(?:^|(?<![A-Za-z0-9_']))'[ \t]*'(?!\w|')")
_BTICK_RUN_RE = re.compile(r"`+")
_BTICK_OPEN_RE = re.compile(rf"{_OPEN_PREFIX}`(?=\S)")
_INLINE_TOKEN_RE = re.compile(r'\\.|`+|"""|\'\'\'|"|\'')
_SQUOTE_TOKEN_RE = re.compile(r"\\.|'")
_SQUOTE_OPEN_RE = re.compile(r"(?:^|(?<![A-Za-z0-9']))'(?=[^\s'])")
_HARD_CLOSE_MAP = {
    '"': re.compile(rf'(?<=\S)"{_HARD_CLOSE_SUFFIX}'),
    "'": re.compile(rf"(?<=\S)'{_HARD_CLOSE_SUFFIX}"),
    "`": re.compile(rf"(?<=\S)`{_HARD_CLOSE_SUFFIX}"),
}
_PUNCT_CLOSE_MAP = {
    '"': re.compile(rf'(?<=\S)"(?={_PUNCT_CLOSE_SUFFIX})'),
    "'": re.compile(rf"(?<=\S)'(?={_PUNCT_CLOSE_SUFFIX})"),
    "`": re.compile(rf"(?<=\S)`(?={_PUNCT_CLOSE_SUFFIX}|[\"\']{_AFTER_EMPH_SUFFIX})"),
}
_CONT_CLOSE_MAP = {
    '"': re.compile(r'(?<=\S)"(?=[/*_\\-]|[([{][\"\'`]|[sS]\b)'),
    "'": re.compile(r"(?:(?<=\S)'(?=[/*_\\-]|[([{][\"\'`])|(?<=[^A-Za-z0-9_\s])'(?=[sS]\b))"),
    "`": re.compile(r"(?<=\S)`(?=[/*_\\-]|[([{][\"\'`]|[sS]\b)"),
}
_TRIPLE_POS_RE = {
    '"""': re.compile(r'(?=""")'),
    "'''": re.compile(r"(?=''')"),
}
_PLURAL_END_RE = re.compile(r"[A-Za-z][sS]")
_PLURAL_POSS_WORD_RE = re.compile(r"[A-Za-z][sS]'[ \t]+([A-Za-z]+)(?:[ \t]+([A-Za-z]+'[sS]?)[ \t])?")
_TRAILING_PROSE_RE = re.compile(r"(?<![ \t])[ \t]+[a-z]")
_YN_PROMPT_RE = re.compile(r"(?i)(?:\[y/n\]|\(y/n\))")
_TAIL_Q_OR_YN_RE = re.compile(r"(?i)(?:\?|\[y/n\]|\(y/n\))")
_STRAY_DELIM_RE = re.compile(r"['\"`]")
_DELIM_TRANS = str.maketrans("", "", "'\"`")
_QUOTED_QUESTION_END_RE = re.compile(r"\?(?:[ \t]+(?:yes|no))?[ \t]*$", re.IGNORECASE)
_NON_POSSESSED_WORDS = frozenset(
    {
        "to",
        "for",
        "with",
        "from",
        "by",
        "in",
        "on",
        "at",
        "into",
        "of",
        "before",
        "after",
        "if",
        "when",
        "while",
        "then",
        "than",
        "that",
        "is",
        "are",
        "was",
        "were",
        "be",
        "do",
        "does",
        "did",
        "can",
        "could",
        "will",
        "would",
        "should",
        "must",
        "may",
        "allow",
        "run",
        "select",
        "esc",
        "here",
        "there",
        "now",
    }
)
_EDGE_TOKEN_MAP = {
    '"': re.compile(r'\\.|"'),
    "'": _SQUOTE_TOKEN_RE,
    "`": re.compile(r"\\.|`+"),
}
_EDGE_OPEN_MAP = {
    '"': _DQUOTE_OPEN_RE,
    "'": _SQUOTE_OPEN_RE,
    "`": _BTICK_OPEN_RE,
}
_MODAL_FOLLOW_CHOICE_RE = re.compile(
    r"(?i)^[ \t]*(?:[›❯>][ \t]*[1-9]\d*\.[ \t]+\S"
    r"|[1-9]\d*\.[ \t]+(?:Yes|No|Allow|Always|Don't[ \t]+allow|Do[ \t]+not[ \t]+allow|Proceed|Confirm|"
    r"Cancel|Continue|Reject|Deny|Trust[ \t]+this[ \t]+directory)\b"
    r"|[›❯][ \t]*(?:Yes|No|Allow|Always|Proceed|Confirm|Cancel|Continue|Reject|Deny)\b[^\n]{0,72}"
    r"|>[ \t]*(?:Yes|No|Allow|Always|Proceed|Confirm|Cancel|Continue|Reject|Deny)\b"
    r"(?:[ \t]+(?:once|always|all|for[ \t]+this[ \t]+session|this[ \t]+\w+))?"
    r"(?:[ \t]*\([A-Za-z0-9/_ -]+\))?[ \t]*$)"
)
_YN_FOLLOWUP_LINE_RE = re.compile(
    r"(?i)^[^\"'`\n]*(?:\[y/n\]|\(y/n\))(?:[ \t]*(?:\[[^\]\n]{1,32}\]|\([^)\n]{1,32}\)))?[ \t]*(?::[ \t]*)?$"
)
_MODAL_GUARD_LINE_RE = re.compile(
    r"(?i)^[ \t]*(?:(?:[›❯>][ \t]*)?[1-9]\d*\.[ \t]+\S|└[ \t]*Models)"
    r"|(?:Models\s*&\s*Quota|Select\s+(?:a\s+)?model|Allow\s+(?:execution|running|access)\b"
    r"|Do\s+you\s+want\s+to\s+(?:proceed|make\s+this\s+edit|run)\b"
    r"|Run\s+this\s+command[ \t]*\?|\[y/n\]|\(y/n\)|↑/↓\s*Scroll|\besc\s+Close\b)"
)
_NUMBERED_ITEM_RE = re.compile(r"^[ \t]*[1-9]\d*\.[ \t]+\S")
_DOCSTRING_OPEN_RE = re.compile(
    r"^(?:(?>[ \t]+)[_A-Za-z]\w*:[ \t]*"
    r'|(?>[ \t]*)(?:[^\n"\'`#?]{0,80}[=(\[,+][ \t]*'
    r'|[^\n"\'`#?{]{0,80}\{[^\n"\'`#?]{0,80}:[ \t]*'
    r"|(?:return|yield)[ \t]+|:[ \t]*)?)"
    r'[rRuUbBfF]{0,2}("""|\'\'\')'
)
_DOCSTRING_CLOSE_RE = re.compile(r'("""|\'\'\')(?>[ \t]*)(?:[)\]},;]+(?>[ \t]*))?(?:#.*)?$')
_DOCSTRING_OPENER_BEFORE_RE = re.compile(r"(?:^[ \t]*(?:return|yield)[ \t]+|[=(\[,+:][ \t]*)[rRuUbBfF]{0,2}$")
_BS_RUN_RE = re.compile(r"\\+")
_DOCSTRING_MODAL_HEADER_RE = re.compile(r"(?i)(?:Select (?:a )?model:|Models\s*&\s*Quota|\besc Close\b|↑/↓ Scroll)")
_MODAL_HINT_LINE_RE = re.compile(
    r"(?i)^(?:(?:press[ \t]+)?\[?enter\]?[ \t]+to[ \t]+(?:confirm|select|continue|submit)"
    r"|(?:press[ \t]+)?\[?esc\]?[ \t]+to[ \t]+(?:cancel|exit|close|dismiss)"
    r"|type[ \t]+to[ \t]+(?:filter|search)"
    r"|(?:(?:use|press)[ \t]+)?(?:(?:↑/↓|↑[ \t]*↓)(?=$|[^A-Za-z0-9_])|arrow[ \t]+keys\b))"
)
_WS_ANCHOR_SCAN_RE = re.compile(
    r"(?P<ws>(?>(?:(?:\[(?:[ \t\r\n\f\v]|\\[trnfvs])+\]|\\s|[ \t])[*+?]?)+))(?P<dlr>\$(?=\||$))?"
    r"|\\.|\[(?:\\.|[^\]\\])*(?:\]|$)|(?P<bare>\$(?=\||$))"
)
MODAL_APPROVAL_RE = re.compile(rf"(?i)(?:{_MODAL_JOINED_PATTERN}|\[y/n\]|\(y/n\))")
MODAL_STRONG_RE = re.compile(rf"(?i)(?:{_MODAL_JOINED_PATTERN})")
PASTE_CHIP_RE = re.compile(r"(?:\[Pasted Content|\[Pasted text|↑[ \t]+\d+[ \t]+more lines)")
TURN_RUNNING_RE = re.compile(
    r"(?i)(?:[•●○⏺◉▸*✻✽✢✳✶\u2800-\u28ff][ \t]+(?:Working|Thinking|Thought|Generating|Brewing|Running)|"
    r"esc[ \t]+to[ \t]+(?:interrupt|cancel|stop)|tab[ \t]+to[ \t]+queue)"
)
TAB_QUEUE_FOOTER_RE = re.compile(r"(?i)tab[ \t]+to[ \t]+queue")
ESC_ACTIVE_FOOTER_RE = re.compile(r"(?i)esc[ \t]+to[ \t]+(?:interrupt|cancel|stop)")
_MAX_FOOTER_LINES = 5
_MODAL_WINDOW_LINES = 18
_QUEUE_BANNER_BEFORE = 8
_QUEUE_BANNER_AFTER = 8


class DeliveryState(Enum):
    """Exact state classification for an interactive tmux dispatch."""

    SUBMITTED = "submitted"
    ACCEPTED_ACTIVE = "accepted_active"
    QUEUED = "queued"
    UNSUBMITTED = "unsubmitted"
    PARTIAL_PASTE = "partial_paste"
    BLOCKED_MODAL = "blocked_modal"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class NormalizedText:
    """Wrap-collapsed text with 0-based original line map per character."""

    text: str
    line_map: tuple[int, ...]


@dataclass(frozen=True)
class MarkerSpan:
    """Byte span and 0-based original line bounds of a matched marker."""

    start: int
    end: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class CaptureRegions:
    """Segmented lines and boundary indices of a terminal pane capture."""

    lines: list[str]
    body_end: int
    composer_start: int
    composer_clean: bool


@dataclass(frozen=True)
class Classification:
    """Structured verdict produced for pre-check, prepare-check, and verify."""

    state: DeliveryState
    dispatch_id: str
    reason: str
    composer_clean: bool
    tab_to_queue: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "delivery_state": self.state.value,
            "dispatch_id": self.dispatch_id,
            "reason": self.reason,
            "composer_clean": self.composer_clean,
            "tab_to_queue": self.tab_to_queue,
        }


def _translate_posix_classes(raw: str) -> str:
    for posix_cls, py_chars in _POSIX_CLASS_MAP.items():
        if posix_cls in raw:
            raw = raw.replace(posix_cls, py_chars)
    return raw


def _compile_env_regex(env_key: str, default: str, flags: int = 0) -> re.Pattern[str]:
    override = os.environ.get(env_key)
    raw = _translate_posix_classes(override if override else default)
    return re.compile(raw, flags)


def _replace_ws_anchor_match(match: re.Match[str]) -> str:
    if match.group("dlr") is not None or match.group("bare") is not None:
        return r"(?:[ \t]|$)"
    return match.group(0)


def _rewrite_trailing_ws_anchors(pattern: str) -> str:
    if "$" not in pattern:
        return pattern
    return _WS_ANCHOR_SCAN_RE.sub(_replace_ws_anchor_match, pattern)


def _compile_custom_prompt_prefix() -> re.Pattern[str] | None:
    override = os.environ.get("ORCHESTRATION_COMPOSER_REGEX")
    if not override:
        return None
    translated = _translate_posix_classes(override)
    return re.compile(_rewrite_trailing_ws_anchors(translated))


def _strip_box_right(line: str) -> str:
    return line.rstrip(" \t\r").rstrip("│┃").rstrip(" \t")


def _strip_box_left(line: str) -> str:
    stripped = line.lstrip(" \t")
    if stripped.startswith(("│", "┃")):
        return stripped[1:].lstrip(" \t")
    return stripped


def _strip_box_borders(line: str) -> str:
    return _strip_box_left(_strip_box_right(line))


def _strip_box_edges_preserve_indent(line: str) -> str:
    rtrimmed = line.rstrip(" \t\r")
    if rtrimmed.endswith(("│", "┃")):
        line = rtrimmed[:-1].rstrip(" \t")
    ltrimmed = line.lstrip(" \t")
    if ltrimmed.startswith(("│", "┃")):
        after = ltrimmed[1:]
        return after[1:] if after.startswith((" ", "\t")) else after
    return line


def _is_barrier_line(line: str) -> bool:
    cleaned = _strip_box_borders(line)
    if not cleaned.strip():
        return True
    return bool(DIVIDER_LINE_RE.match(cleaned))


def normalize_lines(lines: list[str], start_idx: int = 0) -> NormalizedText:
    """Collapse single TUI soft-wraps while preserving blank/divider barriers."""
    chars: list[str] = []
    line_map: list[int] = []

    for offset, raw_line in enumerate(lines):
        orig_line = start_idx + offset
        if _is_barrier_line(raw_line):
            chars.append("\n")
            line_map.append(orig_line)
            continue
        piece = _strip_box_borders(raw_line)
        chars.extend(piece)
        line_map.extend([orig_line] * len(piece))

    return NormalizedText(text="".join(chars), line_map=tuple(line_map))


def find_marker_spans(norm: NormalizedText, marker: str) -> list[MarkerSpan]:
    """Return ordered MarkerSpan matches for `marker` inside `norm`."""
    spans: list[MarkerSpan] = []
    if not marker:
        return spans
    pos = 0
    while True:
        idx = norm.text.find(marker, pos)
        if idx < 0:
            break
        end_idx = idx + len(marker) - 1
        spans.append(
            MarkerSpan(
                start=idx,
                end=idx + len(marker),
                start_line=norm.line_map[idx],
                end_line=norm.line_map[end_idx],
            )
        )
        pos = idx + len(marker)
    return spans


def markers_for_id(dispatch_id: str) -> tuple[str, str, str]:
    """Return canonical start, accepted, and end markers for `dispatch_id`."""
    return (
        f"[dispatch:{dispatch_id}]",
        f"[dispatch-accepted:{dispatch_id}]",
        f"[dispatch-end:{dispatch_id}]",
    )


def _is_footer_line(line: str) -> bool:
    stripped = _strip_box_borders(line).strip()
    if "[dispatch" in stripped or "[Pasted" in stripped:
        return False
    if re.search(r"(?i)(?:\besc[ \t]+close\b|↑/↓[ \t]+scroll)", stripped):
        return False
    if TUI_PROMPT_RE.match(stripped):
        return False
    if DIVIDER_LINE_RE.match(stripped) or FOOTER_CHROME_RE.match(stripped):
        return True
    return bool(_STATUS_DOT_FOOTER_RE.search(stripped))


def _trim_blank_tail(lines: list[str], end: int) -> int:
    while end > 0 and not _strip_box_borders(lines[end - 1]).strip():
        end -= 1
    return end


def _find_body_end(lines: list[str]) -> int:
    end = _trim_blank_tail(lines, len(lines))
    for _ in range(_MAX_FOOTER_LINES):
        if end == 0 or not _is_footer_line(lines[end - 1]):
            break
        end = _trim_blank_tail(lines, end - 1)
    return end


def _is_clean_composer_line(line: str, composer_re: re.Pattern[str]) -> bool:
    cleaned = _strip_box_borders(line)
    return bool(composer_re.match(cleaned) or PLACEHOLDER_COMPOSER_RE.match(cleaned))


def _is_unambiguous_prompt(
    line: str,
    prev_line: str = "",
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    cleaned = _strip_box_borders(line)
    if UNICODE_PROMPT_RE.match(cleaned) or (custom_re is not None and custom_re.match(cleaned)):
        return True
    if not TUI_PROMPT_RE.match(cleaned):
        return False
    raw_strip = line.lstrip(" \t")
    return (
        raw_strip.startswith(("│", "┃"))
        or bool(PASTE_CHIP_RE.search(cleaned))
        or bool(COMPOSER_RULE_RE.match(prev_line))
    )


def _is_bare_ascii_prompt(
    line: str,
    prev_line: str = "",
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    cleaned = _strip_box_borders(line)
    return bool(TUI_PROMPT_RE.match(cleaned)) and not _is_unambiguous_prompt(line, prev_line, custom_re)


def _prompt_starts_dispatch(lines: list[str], idx: int, body_end: int, start_marker: str) -> bool:
    window = normalize_lines(lines[idx : min(body_end, idx + 6)], idx).text
    token = start_marker or "[dispatch:"
    return token in window


def _is_composer_prompt_line(
    lines: list[str],
    idx: int,
    body_end: int,
    start_marker: str,
    saw_blank_below: bool,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    line = lines[idx]
    prev_line = lines[idx - 1] if idx > 0 else ""
    if _is_unambiguous_prompt(line, prev_line, custom_re):
        return True
    if not _is_bare_ascii_prompt(line, prev_line, custom_re):
        return False
    return not saw_blank_below or _prompt_starts_dispatch(lines, idx, body_end, start_marker)


def _line_indent(line: str) -> int:
    stripped = line.lstrip(" \t")
    if stripped.startswith(("│", "┃")):
        inner = stripped[1:]
        return len(inner) - len(inner.lstrip(" \t"))
    return len(line) - len(stripped)


def _matches_any_prompt_prefix(cleaned: str, custom_re: re.Pattern[str] | None) -> bool:
    return bool(TUI_PROMPT_RE.match(cleaned) or (custom_re is not None and custom_re.match(cleaned)))


def _prev_line_at(lines: list[str], idx: int) -> str:
    return lines[idx - 1] if idx > 0 else ""


def _push_indent_event(stack_indents: list[int], stack_vals: list[bool], eff_indent: int, val: bool) -> None:
    while stack_indents and eff_indent in range(stack_indents[-1] + 1):
        stack_indents.pop()
        stack_vals.pop()
    stack_indents.append(eff_indent)
    stack_vals.append(val)


def _record_indent_line(
    lines: list[str],
    idx: int,
    cur_clean: str,
    cur_indent: int,
    custom_re: re.Pattern[str] | None,
    stack_indents: list[int],
    stack_vals: list[bool],
) -> None:
    if TRANSCRIPT_STOP_RE.match(cur_clean):
        _push_indent_event(stack_indents, stack_vals, cur_indent, False)
    if _is_unambiguous_prompt(lines[idx], _prev_line_at(lines, idx), custom_re):
        _push_indent_event(stack_indents, stack_vals, cur_indent + 1, True)


def _precompute_less_indented_prompts(
    lines: list[str],
    body_end: int,
    running_footer: bool,
    custom_re: re.Pattern[str] | None,
) -> list[bool]:
    result = [False] * body_end
    if running_footer:
        return result
    stack_indents: list[int] = []
    stack_vals: list[bool] = []
    for idx in range(body_end):
        cur_clean = _strip_box_borders(lines[idx])
        if not cur_clean.strip():
            continue
        cur_indent = _line_indent(lines[idx])
        pos = bisect_right(stack_indents, cur_indent) - 1
        if pos >= 0:
            result[idx] = stack_vals[pos]
        _record_indent_line(lines, idx, cur_clean, cur_indent, custom_re, stack_indents, stack_vals)
    return result


def _last_prompt_dispatch_spans(
    lines: list[str],
    body_end: int,
    start_marker: str,
    custom_re: re.Pattern[str] | None,
) -> tuple[MarkerSpan, MarkerSpan, list[MarkerSpan]] | None:
    if not start_marker:
        return None
    dispatch_id = start_marker.removeprefix("[dispatch:").removesuffix("]")
    _, acc_m, end_m = markers_for_id(dispatch_id)
    norm = normalize_lines(lines[:body_end])
    starts = find_marker_spans(norm, start_marker)
    if not starts:
        return None
    s_span = starts[-1]
    if not _matches_any_prompt_prefix(_strip_box_borders(lines[s_span.start_line]), custom_re):
        return None
    ends = find_marker_spans(norm, end_m)
    if not ends or ends[-1].start < s_span.end:
        return None
    return s_span, ends[-1], find_marker_spans(norm, acc_m)


def _has_active_turn_without_queue(lines: list[str], body_end: int) -> bool:
    has_esc = any(ESC_ACTIVE_FOOTER_RE.search(line) for line in lines[body_end:])
    return has_esc and not _has_tab_to_queue_footer(lines, body_end)


def _prompt_dispatch_bounds(
    lines: list[str],
    body_end: int,
    start_marker: str,
    custom_re: re.Pattern[str] | None,
) -> tuple[int | None, int]:
    found = _last_prompt_dispatch_spans(lines, body_end, start_marker, custom_re)
    if found is None:
        return None, 0
    s_span, e_span, accs = found
    if e_span.end_line != body_end - 1 or _has_active_turn_without_queue(lines, body_end):
        return None, e_span.end_line + 1
    if any(acc.start < s_span.end or acc.start >= e_span.end for acc in accs):
        return None, 0
    return s_span.start_line, 0


def _has_running_footer(lines: list[str], body_end: int) -> bool:
    return any(TURN_RUNNING_RE.search(line) for line in lines[body_end:])


def _is_dirty_composer_stop(
    line: str,
    has_candidate: bool,
    unambiguous_candidate: bool,
    running_footer: bool,
    less_prompt_above: bool,
) -> bool:
    cleaned = _strip_box_borders(line)
    stop_re = TRANSCRIPT_STOP_RE if (has_candidate or running_footer) else ACTIVE_STOP_RE
    is_box = has_candidate and _line_indent(line) == 0 and bool(BOX_BORDER_RE.match(line))
    if not (stop_re.match(cleaned) or is_box):
        return False
    if running_footer or unambiguous_candidate:
        return True
    return not less_prompt_above


def _should_break_at_prior_prompt(
    lines: list[str],
    idx: int,
    body_end: int,
    candidate: int,
    unambig_cand: bool,
    start_marker: str,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    if not unambig_cand or _prompt_starts_dispatch(lines, idx, body_end, start_marker):
        return False
    cand_clean = _strip_box_borders(lines[candidate])
    if UNICODE_PROMPT_RE.match(cand_clean) or (custom_re is not None and custom_re.match(cand_clean)):
        return True
    return _line_indent(lines[idx]) >= _line_indent(lines[candidate])


def _classify_dirty_line(
    lines: list[str],
    idx: int,
    body_end: int,
    candidate: int,
    unambig_cand: bool,
    start_marker: str,
    running_footer: bool,
    less_prompt: bool,
    saw_blank_below: bool,
    custom_re: re.Pattern[str] | None,
) -> str:
    line = lines[idx]
    if not _strip_box_borders(line).strip():
        return "blank"
    if _is_dirty_composer_stop(line, candidate < body_end, unambig_cand, running_footer, less_prompt):
        return "break"
    if not _is_composer_prompt_line(lines, idx, body_end, start_marker, saw_blank_below, custom_re):
        return "skip"
    if _should_break_at_prior_prompt(lines, idx, body_end, candidate, unambig_cand, start_marker, custom_re):
        return "break"
    return "dispatch" if _prompt_starts_dispatch(lines, idx, body_end, start_marker) else "candidate"


def _scan_dirty_composer_upward(
    lines: list[str],
    body_end: int,
    floor_idx: int,
    start_marker: str,
    running_footer: bool,
    less_prompts: list[bool],
    custom_re: re.Pattern[str] | None,
) -> int:
    candidate = body_end
    unambig_cand = False
    saw_blank_below = False
    for idx in range(body_end - 1, floor_idx - 1, -1):
        action = _classify_dirty_line(
            lines,
            idx,
            body_end,
            candidate,
            unambig_cand,
            start_marker,
            running_footer,
            less_prompts[idx],
            saw_blank_below,
            custom_re,
        )
        if action == "blank":
            saw_blank_below = True
        elif action == "break":
            return candidate
        elif action == "dispatch":
            return idx
        elif action == "candidate":
            candidate = idx
            unambig_cand = unambig_cand or _is_unambiguous_prompt(lines[idx], _prev_line_at(lines, idx), custom_re)
    return candidate


def _find_dirty_composer_start(lines: list[str], body_end: int, start_marker: str = "") -> int:
    """Locate start of populated bottom composer without mistaking markdown quotes."""
    custom_re = _compile_custom_prompt_prefix()
    direct_start, floor_idx = _prompt_dispatch_bounds(lines, body_end, start_marker, custom_re)
    if direct_start is not None:
        return direct_start
    running_footer = _has_running_footer(lines, body_end)
    less_prompts = _precompute_less_indented_prompts(lines, body_end, running_footer, custom_re)
    return _scan_dirty_composer_upward(
        lines, body_end, floor_idx, start_marker, running_footer, less_prompts, custom_re
    )


def segment_capture(
    text: str,
    composer_re: re.Pattern[str],
    start_marker: str = "",
) -> CaptureRegions:
    """Split terminal pane text into transcript/queued region and bottom composer."""
    lines = text.splitlines()
    body_end = _find_body_end(lines)
    if body_end == 0:
        return CaptureRegions(lines=lines, body_end=0, composer_start=0, composer_clean=False)

    if _is_clean_composer_line(lines[body_end - 1], composer_re):
        return CaptureRegions(
            lines=lines,
            body_end=body_end,
            composer_start=body_end - 1,
            composer_clean=True,
        )

    comp_start = _find_dirty_composer_start(lines, body_end, start_marker)
    return CaptureRegions(
        lines=lines,
        body_end=body_end,
        composer_start=comp_start,
        composer_clean=False,
    )


def _exclude_unmatched_end_chip(
    lines: list[str],
    end_span: MarkerSpan,
    high_water: int,
    excluded: set[int],
    custom_re: re.Pattern[str] | None,
) -> int:
    chip_line = _find_contiguous_chip_line(lines, high_water + 1, end_span.start_line, custom_re)
    if chip_line is not None:
        excluded.update(range(chip_line, end_span.end_line + 1))
    return max(high_water, end_span.end_line)


def _dispatch_excluded_lines(
    lines: list[str],
    dispatch_id: str,
    custom_re: re.Pattern[str] | None = None,
) -> set[int]:
    if not dispatch_id:
        return set()
    norm = normalize_lines(lines)
    start_m, _, end_m = markers_for_id(dispatch_id)
    starts = find_marker_spans(norm, start_m)
    ends = find_marker_spans(norm, end_m)
    excluded: set[int] = set()
    for span in [*starts, *ends]:
        excluded.update(range(span.start_line, span.end_line + 1))
    end_idx = 0
    high_water = -1
    for s_span in starts:
        while end_idx < len(ends) and ends[end_idx].start < s_span.end:
            high_water = _exclude_unmatched_end_chip(lines, ends[end_idx], high_water, excluded, custom_re)
            end_idx += 1
        if end_idx < len(ends):
            lo = max(s_span.start_line, high_water + 1)
            hi = ends[end_idx].end_line
            excluded.update(range(lo, hi + 1))
            high_water = max(high_water, hi)
            end_idx += 1
    for rem_end in ends[end_idx:]:
        high_water = _exclude_unmatched_end_chip(lines, rem_end, high_water, excluded, custom_re)
    return excluded


def _is_cursor_modal_item(cleaned: str) -> bool:
    return bool(
        MENU_OPTION_CURSOR_RE.match(cleaned)
        or _MODAL_CURSOR_BUTTON_RE.match(cleaned)
        or _UNNUMBERED_CURSOR_CHOICE_RE.match(cleaned)
    )


def _is_indented_option_line(raw_line: str) -> bool:
    preserved = _strip_box_edges_preserve_indent(raw_line).rstrip(" \t\r")
    cleaned = preserved.lstrip(" \t")
    return bool(cleaned and preserved[:1].isspace() and not _BULLET_PREFIX_RE.match(cleaned))


def _neighbor_nonempty_line(
    lines: list[str],
    start: int,
    stop: int,
    step: int,
    excluded: set[int],
) -> str:
    for k in range(start, stop, step):
        if k not in excluded and _strip_box_borders(lines[k]).strip():
            return lines[k]
    return ""


def _classify_cursor_follow_line(raw_line: str) -> str:
    preserved = _strip_box_edges_preserve_indent(raw_line).rstrip(" \t\r")
    cleaned = preserved.lstrip(" \t")
    if _is_barrier_line(cleaned) or _MODAL_HINT_LINE_RE.match(cleaned):
        return ""
    if MODAL_APPROVAL_RE.search(cleaned):
        return "modal"
    return "prose" if not preserved[:1].isspace() else "indented"


def _scan_after_cursor_item(lines: list[str], idx: int, end: int, excluded: set[int]) -> str:
    for k in range(idx + 1, end):
        kind = "" if k in excluded else _classify_cursor_follow_line(lines[k])
        if kind in ("modal", "prose"):
            return kind
    return "indented"


def _cursor_header_signal(raw_line: str, custom_re: re.Pattern[str] | None) -> str:
    cleaned = _strip_box_borders(raw_line).strip()
    if not cleaned:
        return ""
    if TRANSCRIPT_STOP_RE.match(cleaned) or _matches_any_prompt_prefix(cleaned, custom_re):
        return "stop"
    if MODAL_APPROVAL_RE.search(cleaned):
        return "modal"
    return "text"


def _iter_header_signals_above(
    lines: list[str],
    idx: int,
    excluded: set[int],
    custom_re: re.Pattern[str] | None,
) -> list[str]:
    signals: list[str] = []
    limit = _MODAL_WINDOW_LINES * 4
    for k in range(idx - 1, -1, -1):
        sig = "" if k in excluded else _cursor_header_signal(lines[k], custom_re)
        if sig:
            signals.append(sig)
            if len(signals) == limit:
                break
    return signals


def _has_modal_header_above(
    lines: list[str],
    idx: int,
    excluded: set[int],
    custom_re: re.Pattern[str] | None,
) -> bool:
    for signal in _iter_header_signals_above(lines, idx, excluded, custom_re):
        if signal in ("stop", "modal"):
            return signal == "modal"
    return False


def _is_contextual_cursor_item(
    lines: list[str],
    idx: int,
    end: int,
    excluded: set[int],
    cleaned: str,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    if _is_cursor_modal_item(cleaned):
        return True
    if not UNICODE_PROMPT_RE.match(cleaned):
        return False
    prev_raw = _neighbor_nonempty_line(lines, idx - 1, -1, -1, excluded)
    next_raw = _neighbor_nonempty_line(lines, idx + 1, end, 1, excluded)
    if not (_is_indented_option_line(prev_raw) or _is_indented_option_line(next_raw)):
        return False
    after = _scan_after_cursor_item(lines, idx, end, excluded)
    if after == "prose":
        return False
    return after == "modal" or _has_modal_header_above(lines, idx, excluded, custom_re)


def _should_stop_modal_scan(
    lines: list[str],
    idx: int,
    end: int,
    excluded: set[int],
    cleaned: str,
    prompt_stop_end: int,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    if TRANSCRIPT_STOP_RE.match(cleaned):
        return True
    if not (idx < prompt_stop_end and _matches_any_prompt_prefix(cleaned, custom_re)):
        return False
    return not _is_contextual_cursor_item(lines, idx, end, excluded, cleaned, custom_re)


def _collect_trailing_modal_lines(
    lines: list[str],
    end: int,
    excluded: set[int],
    limit: int,
    *,
    prompt_stop_end: int,
    custom_re: re.Pattern[str] | None = None,
) -> list[str]:
    collected: list[str] = []
    for idx in range(end - 1, -1, -1):
        if idx in excluded:
            continue
        preserved = _strip_box_edges_preserve_indent(lines[idx]).rstrip(" \t\r")
        cleaned = preserved.lstrip(" \t")
        if not cleaned:
            continue
        if _should_stop_modal_scan(lines, idx, end, excluded, cleaned, prompt_stop_end, custom_re):
            break
        collected.append(preserved)
        if len(collected) >= limit:
            break
    return list(reversed(collected))


def _parse_fence_line(lines: list[str], idx: int) -> tuple[str, int] | None:
    match = _FENCE_OPEN_RE.match(lines[idx])
    if match is None:
        return None
    bt_marker, tilde_marker, tilde_rest = match.group(1), match.group(2), match.group(3)
    if bt_marker is not None:
        return "`", len(bt_marker)
    prev_text = "".join(lines[max(0, idx - 1) : idx]).strip()
    if not tilde_rest.strip(" \t") and prev_text:
        return None
    return "~", len(tilde_marker)


class _FenceCloserIndex:
    """Segment-tree index answering first closing fence > line_idx with length >= f_len in O(log L)."""

    def __init__(self, entries: list[tuple[int, int]]) -> None:
        self._lines = [item[0] for item in entries]
        if not entries:
            self._size = 0
            self._tree: list[int] = []
            return
        size = 1 << (len(entries) - 1).bit_length()
        self._size = size
        tree = [0] * (2 * size)
        for i, (_, t_len) in enumerate(entries):
            tree[size + i] = t_len
        for node in range(size - 1, 0, -1):
            tree[node] = max(tree[2 * node], tree[2 * node + 1])
        self._tree = tree

    def _query_first(self, node: int, nl: int, nr: int, min_pos: int, f_len: int) -> int | None:
        if nr <= min_pos or self._tree[node] < f_len:
            return None
        if nr - nl == 1:
            return self._lines[nl]
        mid = (nl + nr) // 2
        left_hit = self._query_first(2 * node, nl, mid, min_pos, f_len)
        return left_hit if left_hit is not None else self._query_first(2 * node + 1, mid, nr, min_pos, f_len)

    def find_after(self, line_idx: int, f_len: int) -> int | None:
        if not self._lines:
            return None
        start = bisect_right(self._lines, line_idx)
        return self._query_first(1, 0, self._size, start, f_len)


def _build_fence_closers(lines: list[str]) -> dict[str, _FenceCloserIndex]:
    buckets: dict[str, list[tuple[int, int]]] = {"`": [], "~": []}
    for idx, line in enumerate(lines):
        match = _FENCE_CLOSE_SCAN_RE.match(line)
        if match is not None:
            run = match.group(1)
            buckets[run[0]].append((idx, len(run)))
    return {ch: _FenceCloserIndex(entries) for ch, entries in buckets.items()}


def _is_docstring_button_or_choice(line: str) -> bool:
    stripped = line.strip()
    return bool(
        _has_modal_followup_line(line) or _is_cursor_modal_item(stripped) or _DOCSTRING_MODAL_HEADER_RE.search(line)
    )


def _line_valid_triples(line: str, delim: str) -> tuple[int, ...]:
    odd_bs = _odd_backslash_ends(line)
    bdata = _prepare_btick_line_data(line, odd_bs)
    sdata = _prepare_squote_line_data(line, _EMPTY_MODAL_INDEX, bdata)
    dquote_ends = _prepare_dquote_ends(line, bdata)
    q_spans = _opposite_quote_spans(line, delim, sdata, dquote_ends, bdata, odd_bs)
    return _valid_triple_positions(line, delim, bdata, odd_bs, q_spans)


def _find_docstring_open(line: str) -> tuple[int, str] | None:
    match = _DOCSTRING_OPEN_RE.match(line)
    if match is None:
        return None
    delim = match.group(1)
    valid_pos = _line_valid_triples(line, delim)
    if _match_triple_pos(match.start(1), valid_pos) is not None:
        return None
    return match.start(1), delim


def _is_valid_docstring_closer(line: str, delim: str, close_pos: int) -> bool:
    valid_pos = _line_valid_triples(line, delim)
    if close_pos not in valid_pos:
        return False
    prior = 0
    next_allowed = 0
    for pos in valid_pos:
        if pos in range(next_allowed, close_pos - 2):
            prior += 1
            next_allowed = pos + 3
    return prior % 2 == 0


def _maybe_erase_docstring(
    lines: list[str],
    open_idx: int,
    open_pos: int,
    close_idx: int,
    close_end: int,
) -> None:
    if close_idx - open_idx > 24:
        return
    end = min(len(lines), close_idx + 5)
    probes = [lines[open_idx][open_pos + 3 :], *lines[open_idx + 1 : end]]
    if any(_is_docstring_button_or_choice(seg) for seg in probes):
        return
    lines[open_idx] = lines[open_idx][:open_pos]
    for k in range(open_idx + 1, close_idx):
        lines[k] = ""
    lines[close_idx] = lines[close_idx][close_end:]


def _close_docstring_state(
    lines: list[str],
    idx: int,
    open_state: tuple[int, int, str],
) -> tuple[int, int, str] | None:
    open_idx, open_pos, delim = open_state
    match = _DOCSTRING_CLOSE_RE.search(lines[idx])
    if match is None or match.group(1) != delim or not _is_valid_docstring_closer(lines[idx], delim, match.start(1)):
        return open_state
    if _DOCSTRING_OPENER_BEFORE_RE.search(lines[idx][: match.start(1)]) is not None:
        return open_state
    _maybe_erase_docstring(lines, open_idx, open_pos, idx, match.end(1))
    return None


def _strip_docstring_blocks(lines: list[str]) -> None:
    open_state: tuple[int, int, str] | None = None
    for idx in range(len(lines)):
        if open_state is not None:
            open_state = _close_docstring_state(lines, idx, open_state)
            continue
        found = _find_docstring_open(lines[idx])
        if found is not None:
            open_state = (idx, found[0], found[1])


def _strip_unclosed_fence_prefix(line: str) -> str:
    return line.lstrip(" \t`~")


def _erase_or_unfence(lines: list[str], idx: int, closers: dict[str, _FenceCloserIndex]) -> int:
    parsed = _parse_fence_line(lines, idx)
    if parsed is None:
        return idx + 1
    close_idx = closers[parsed[0]].find_after(idx, parsed[1])
    if close_idx is None:
        lines[idx] = _strip_unclosed_fence_prefix(lines[idx])
        return idx + 1
    for k in range(idx, close_idx + 1):
        lines[k] = ""
    return close_idx + 1


def _strip_fenced_blocks(lines: list[str]) -> None:
    closers = _build_fence_closers(lines)
    next_idx = 0
    for idx in range(len(lines)):
        if idx >= next_idx:
            next_idx = _erase_or_unfence(lines, idx, closers)


@dataclass(frozen=True)
class _ModalIndex:
    """Sorted non-overlapping modal intervals for O(log M) overlap queries."""

    starts: tuple[int, ...]
    ranges: tuple[tuple[int, int], ...]


_EMPTY_MODAL_INDEX = _ModalIndex(starts=(), ranges=())


@dataclass(frozen=True)
class _SquoteLineData:
    """Precomputed single-quote markers and modal ranges for one line."""

    marks: list[tuple[int, str]]
    open_slots: dict[int, int]
    modal_index: _ModalIndex
    close_targets: tuple[int | None, ...] = ()


_EMPTY_SQUOTE_DATA = _SquoteLineData(marks=[], open_slots={}, modal_index=_EMPTY_MODAL_INDEX)


@dataclass(frozen=True)
class _BtickLineData:
    """Precomputed single-backtick closes and multi-backtick run positions for O(N) lookup."""

    single_ends: dict[int, int | None]
    multi_runs: dict[int, list[int]]
    span_starts: tuple[int, ...] = ()
    span_ends: tuple[int, ...] = ()


_EMPTY_BTICK_DATA = _BtickLineData(single_ends={}, multi_runs={})


def _is_1char_bracket_quote(line: str, pos: int, q_char: str) -> bool:
    if line[max(0, pos - 2) : max(0, pos - 1)] != q_char:
        return False
    prev3 = line[max(0, pos - 3) : max(0, pos - 2)]
    return not prev3 or prev3.isspace() or prev3 in "=:;,([{<+~|"


def _is_open_bracket_wrap(line: str, pos: int, q_char: str) -> bool:
    return line[max(0, pos - 1) : pos] in ("(", "[", "{", "<") and not _is_1char_bracket_quote(line, pos, q_char)


def _can_close_inner_quote(prev: str, prev2: str, q_char: str) -> bool:
    return prev in ('"', "'", "`") and prev != q_char and (prev2.isalnum() or prev2 in ('"', "'", "`"))


def _can_close_before_cont(line: str, pos: int, q_char: str) -> bool:
    prev = line[max(0, pos - 1) : pos]
    if prev.isalnum():
        return True
    prev2 = line[max(0, pos - 2) : max(0, pos - 1)]
    if not prev2:
        return False
    if prev in "([{" and _is_1char_bracket_quote(line, pos, q_char):
        return True
    if prev in ")]}?!." and not prev2.isspace():
        return True
    return _can_close_inner_quote(prev, prev2, q_char)


def _is_close_delim_at(line: str, pos: int, q_char: str) -> bool:
    if _HARD_CLOSE_MAP[q_char].match(line, pos) is not None:
        return True
    if _PUNCT_CLOSE_MAP[q_char].match(line, pos) is not None:
        return not _is_open_bracket_wrap(line, pos, q_char)
    return _CONT_CLOSE_MAP[q_char].match(line, pos) is not None and _can_close_before_cont(line, pos, q_char)


def _classify_squote_mark(line: str, pos: int) -> str | None:
    if _is_close_delim_at(line, pos, "'"):
        return "C"
    if _is_true_open_at(line, pos, "'"):
        return "O"
    return None


def _is_skipped_quote_token(
    match: re.Match[str],
    q_char: str,
    skip_until: int,
    bdata: _BtickLineData,
) -> bool:
    pos = match.start()
    return (
        match.group(0) != q_char
        or pos in range(skip_until)
        or _is_inside_spans(pos, bdata.span_starts, bdata.span_ends)
    )


def _single_quote_marks(line: str, bdata: _BtickLineData = _EMPTY_BTICK_DATA) -> list[tuple[int, str]]:
    marks: list[tuple[int, str]] = []
    skip_until = 0
    for match in _SQUOTE_TOKEN_RE.finditer(line):
        if _is_skipped_quote_token(match, "'", skip_until, bdata):
            continue
        pos = match.start()
        empty = _EMPTY_SQUOTE_AT_RE.match(line, pos)
        if empty is not None:
            skip_until = empty.end()
            continue
        kind = _classify_squote_mark(line, pos)
        if kind is not None:
            marks.append((pos, kind))
    return marks


def _compute_modal_index(text: str, offset: int = 0) -> _ModalIndex:
    if not MODAL_APPROVAL_RE.search(text.translate(_DELIM_TRANS)):
        return _EMPTY_MODAL_INDEX
    chars: list[str] = []
    pos_map: list[int] = []
    for idx, ch in enumerate(text):
        if ch not in "'\"`":
            chars.append(ch)
            pos_map.append(idx)
    cleaned = "".join(chars)
    ranges = tuple(
        (pos_map[m.start()] - offset, pos_map[m.end() - 1] + 1 - offset) for m in MODAL_APPROVAL_RE.finditer(cleaned)
    )
    return _ModalIndex(starts=tuple(ms for ms, _ in ranges), ranges=ranges)


def _overlaps_modal(lo: int, hi: int, m_idx: _ModalIndex) -> bool:
    idx = bisect_right(m_idx.starts, hi - 1) - 1
    if idx < 0:
        return False
    ms, me = m_idx.ranges[idx]
    return ms in range(lo, hi) or lo in range(ms, me)


def _precompute_squote_close_targets(line: str, marks: list[tuple[int, str]]) -> tuple[int | None, ...]:
    nxt: int | None = None
    targets: list[int | None] = [None] * len(marks)
    for idx in range(len(marks) - 1, -1, -1):
        targets[idx] = nxt
        pos, kind = marks[idx]
        if kind == "O" or (nxt is not None and _is_true_open_at(line, pos, "'")):
            marks[idx] = (pos, "O")
            nxt = None
        elif _possessive_match(line, pos) is None:
            nxt = idx
    return tuple(targets)


def _prepare_squote_line_data(
    line: str,
    modal_index: _ModalIndex,
    bdata: _BtickLineData = _EMPTY_BTICK_DATA,
) -> _SquoteLineData:
    marks = _single_quote_marks(line, bdata)
    targets = _precompute_squote_close_targets(line, marks)
    open_slots = {m_pos: idx for idx, (m_pos, kind) in enumerate(marks) if kind == "O"}
    return _SquoteLineData(marks=marks, open_slots=open_slots, modal_index=modal_index, close_targets=targets)


def _possessive_match(line: str, pos: int) -> re.Match[str] | None:
    match = _PLURAL_POSS_WORD_RE.match(line, max(0, pos - 2))
    if match is None:
        return None
    next_word = match.group(1).lower()
    if next_word in ("and", "or"):
        return match if match.group(2) is not None else None
    return None if next_word in _NON_POSSESSED_WORDS else match


def _borrows_outer_modal(start_pos: int, close_pos: int, m_idx: _ModalIndex) -> bool:
    return _overlaps_modal(start_pos, start_pos + 1, m_idx) or _overlaps_modal(close_pos, close_pos + 1, m_idx)


def _is_quoted_question_modal(inner: str) -> bool:
    if _YN_PROMPT_RE.search(inner):
        return False
    return _QUOTED_QUESTION_END_RE.search(inner) is not None


def _has_unquoted_modal_after(
    line: str,
    open_pos: int,
    pos: int,
    close_pos: int,
    m_idx: _ModalIndex,
) -> bool:
    idx = bisect_right(m_idx.starts, pos)
    if idx not in range(len(m_idx.starts)) or m_idx.starts[idx] not in range(close_pos):
        return False
    if not line[open_pos + 1 : open_pos + 2].isupper():
        return True
    return line.find(":", open_pos + 1, m_idx.starts[idx]) == -1


def _allows_possessive_modal_tail(
    line: str,
    open_pos: int,
    pos: int,
    close_pos: int,
    sdata: _SquoteLineData,
) -> bool:
    if not sdata.modal_index.ranges or not _overlaps_modal(pos, close_pos, sdata.modal_index):
        return True
    if _has_unquoted_modal_after(line, open_pos, pos, close_pos, sdata.modal_index):
        return False
    if _TRAILING_PROSE_RE.match(line, close_pos + 1) is None:
        return False
    if not _is_quoted_question_modal(line[pos + 1 : close_pos]):
        return False
    return _TAIL_Q_OR_YN_RE.search(line, close_pos + 1) is None


def _can_skip_possessive_mark(
    line: str,
    open_pos: int,
    sdata: _SquoteLineData,
    slot: int,
) -> bool:
    pos = sdata.marks[slot][0]
    if line.find(" ", open_pos + 1, pos) == -1:
        return False
    if _possessive_match(line, pos) is None:
        return False
    close_slot = sdata.close_targets[slot]
    if close_slot is None:
        return False
    return _allows_possessive_modal_tail(line, open_pos, pos, sdata.marks[close_slot][0], sdata)


def _match_inline_squote_close(
    line: str,
    open_pos: int,
    sdata: _SquoteLineData,
    start_slot: int,
) -> int | None:
    marks = sdata.marks
    for slot in range(start_slot, len(marks)):
        _, kind = marks[slot]
        if kind == "O":
            return None
        if _can_skip_possessive_mark(line, open_pos, sdata, slot):
            continue
        return slot
    return None


def _erase_spans(line: str, spans: list[tuple[int, int]]) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(line[cursor:start])
        cursor = end
    parts.append(line[cursor:])
    return "".join(parts)


@dataclass(frozen=True)
class _InlineLineCtx:
    """Per-line precomputed indices for O(N) inline span filtering."""

    m_idx: _ModalIndex
    sdata: _SquoteLineData
    bdata: _BtickLineData
    has_choice_below: bool
    yn_limit: range


def _is_true_open_at(line: str, pos: int, q_char: str) -> bool:
    if _EDGE_OPEN_MAP[q_char].match(line, pos) is None:
        return False
    return not _is_close_delim_at(line, pos, q_char) or _is_open_bracket_wrap(line, pos, q_char)


def _resets_rtl_neutral_quote(line: str, pos: int, q_char: str) -> bool:
    prev = line[max(0, pos - 1) : pos]
    nxt = line[pos + 1 : pos + 2]
    if prev == q_char and nxt == q_char:
        return True
    return prev.isalnum() and nxt.isalnum()


def _next_rtl_quote_end(line: str, pos: int, q_char: str, next_end: int | None) -> int | None:
    if next_end is not None and _is_true_open_at(line, pos, q_char):
        return None
    if _is_close_delim_at(line, pos, q_char):
        return pos + 1
    return None if _resets_rtl_neutral_quote(line, pos, q_char) else next_end


def _collect_btick_spans(
    line: str,
    single_ends: dict[int, int | None],
    multi_runs: dict[int, list[int]],
    odd_bs: set[int] | frozenset[int],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    probe = _BtickLineData(single_ends=single_ends, multi_runs=multi_runs)
    starts: list[int] = []
    ends: list[int] = []
    skip_until = 0
    for match in _BTICK_RUN_RE.finditer(line):
        pos = match.start()
        if pos in range(skip_until) or pos in odd_bs:
            continue
        end = _match_btick_span_at(pos, match.end() - pos, probe)
        if end is not None:
            starts.append(pos)
            ends.append(end)
            skip_until = end
    return tuple(starts), tuple(ends)


def _prepare_btick_line_data(line: str, odd_bs: set[int] | frozenset[int] = frozenset()) -> _BtickLineData:
    singles: list[int] = []
    multi_runs: dict[int, list[int]] = {}
    for match in _BTICK_RUN_RE.finditer(line):
        if match.start() in odd_bs:
            continue
        run_len = match.end() - match.start()
        if run_len == 1:
            singles.append(match.start())
        else:
            multi_runs.setdefault(run_len, []).append(match.start())
    single_ends: dict[int, int | None] = {}
    next_end: int | None = None
    for m_pos in reversed(singles):
        if _is_true_open_at(line, m_pos, "`"):
            single_ends[m_pos] = next_end
        next_end = _next_rtl_quote_end(line, m_pos, "`", next_end)
    span_starts, span_ends = _collect_btick_spans(line, single_ends, multi_runs, odd_bs)
    return _BtickLineData(
        single_ends=single_ends,
        multi_runs=multi_runs,
        span_starts=span_starts,
        span_ends=span_ends,
    )


def _match_btick_span_at(pos: int, bt_len: int, bdata: _BtickLineData) -> int | None:
    if bt_len == 1:
        return bdata.single_ends.get(pos)
    starts = bdata.multi_runs.get(bt_len, [])
    idx = bisect_right(starts, pos)
    return starts[idx] + bt_len if idx < len(starts) else None


def _match_squote_span_at(line: str, pos: int, sdata: _SquoteLineData) -> int | None:
    empty = _EMPTY_SQUOTE_AT_RE.match(line, pos)
    if empty is not None:
        return empty.end()
    slot = sdata.open_slots.get(pos)
    if slot is None:
        return None
    close_slot = _match_inline_squote_close(line, pos, sdata, slot + 1)
    return sdata.marks[close_slot][0] + 1 if close_slot is not None else None


def _odd_backslash_ends(line: str) -> set[int]:
    return {m.end() for m in _BS_RUN_RE.finditer(line) if len(m.group(0)) % 2 == 1}


def _is_inside_spans(pos: int, starts: tuple[int, ...], ends: tuple[int, ...]) -> bool:
    idx = bisect_right(starts, pos) - 1
    return idx >= 0 and pos < ends[idx]


def _collect_dquote_tokens(line: str, bdata: _BtickLineData) -> tuple[dict[int, int], list[int]]:
    ends: dict[int, int] = {}
    quotes: list[int] = []
    skip_until = 0
    for match in _EDGE_TOKEN_MAP['"'].finditer(line):
        if _is_skipped_quote_token(match, '"', skip_until, bdata):
            continue
        pos = match.start()
        empty = _EMPTY_DQUOTE_AT_RE.match(line, pos)
        if empty is not None:
            ends[pos] = empty.end()
            skip_until = empty.end()
            continue
        quotes.append(pos)
    return ends, quotes


def _prepare_dquote_ends(line: str, bdata: _BtickLineData = _EMPTY_BTICK_DATA) -> dict[int, int]:
    ends, quotes = _collect_dquote_tokens(line, bdata)
    next_end: int | None = None
    for pos in reversed(quotes):
        if next_end is not None and _is_true_open_at(line, pos, '"'):
            ends[pos] = next_end
        next_end = _next_rtl_quote_end(line, pos, '"', next_end)
    return {pos: ends[pos] for pos in sorted(ends)}


def _collect_dquote_spans(dquote_ends: dict[int, int]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    skip_until = 0
    for pos, end in dquote_ends.items():
        if pos in range(skip_until):
            continue
        spans.append((pos, end))
        skip_until = end
    return spans


def _collect_squote_spans(line: str, sdata: _SquoteLineData) -> list[tuple[int, int]]:
    return [(pos, end) for pos in sdata.open_slots if (end := _match_squote_span_at(line, pos, sdata)) is not None]


def _spans_to_bounds(spans: list[tuple[int, int]]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(s for s, _ in spans), tuple(e for _, e in spans)


def _collect_opp_triple_spans(
    line: str,
    opp_delim: str,
    bdata: _BtickLineData,
    odd_bs: set[int] | frozenset[int],
    outer_bounds: tuple[tuple[int, ...], tuple[int, ...]],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    positions = _valid_triple_positions(line, opp_delim, bdata, odd_bs, (outer_bounds, ((), ())))
    spans: list[tuple[int, int]] = []
    open_pos: int | None = None
    for pos in positions:
        if open_pos is None:
            open_pos = pos
        elif pos >= open_pos + 3:
            spans.append((open_pos, pos + 3))
            open_pos = None
    return _spans_to_bounds(spans)


def _opposite_quote_spans(
    line: str,
    delim: str,
    sdata: _SquoteLineData,
    dquote_ends: dict[int, int],
    bdata: _BtickLineData,
    odd_bs: set[int] | frozenset[int],
) -> tuple[tuple[tuple[int, ...], tuple[int, ...]], tuple[tuple[int, ...], tuple[int, ...]]]:
    sq_bounds = _spans_to_bounds(_collect_squote_spans(line, sdata))
    dq_bounds = _spans_to_bounds(_collect_dquote_spans(dquote_ends))
    if delim == '"""':
        return sq_bounds, _collect_opp_triple_spans(line, "'''", bdata, odd_bs, dq_bounds)
    return dq_bounds, _collect_opp_triple_spans(line, '"""', bdata, odd_bs, sq_bounds)


def _valid_triple_positions(
    line: str,
    delim: str,
    bdata: _BtickLineData,
    odd_bs: set[int] | frozenset[int],
    q_spans: tuple[tuple[tuple[int, ...], tuple[int, ...]], tuple[tuple[int, ...], tuple[int, ...]]],
) -> tuple[int, ...]:
    char_bounds, opp_bounds = q_spans
    return tuple(
        m.start()
        for m in _TRIPLE_POS_RE[delim].finditer(line)
        if m.start() not in odd_bs
        and not _is_inside_spans(m.start(), bdata.span_starts, bdata.span_ends)
        and not _is_inside_spans(m.start(), char_bounds[0], char_bounds[1])
        and not _is_inside_spans(m.start(), opp_bounds[0], opp_bounds[1])
    )


def _get_triple_positions(
    tpos_cache: dict[str, tuple[int, ...]],
    line: str,
    delim: str,
    sdata: _SquoteLineData,
    bdata: _BtickLineData,
    odd_bs: set[int] | frozenset[int],
    dquote_ends: dict[int, int],
) -> tuple[int, ...]:
    cached = tpos_cache.get(delim)
    if cached is not None:
        return cached
    q_spans = _opposite_quote_spans(line, delim, sdata, dquote_ends, bdata, odd_bs)
    positions = _valid_triple_positions(line, delim, bdata, odd_bs, q_spans)
    tpos_cache[delim] = positions
    return positions


def _match_triple_pos(pos: int, valid_positions: tuple[int, ...]) -> int | None:
    idx = bisect_right(valid_positions, pos + 2)
    return valid_positions[idx] + 3 if idx < len(valid_positions) else None


def _yn_limit_outside_spans(line: str, raw_spans: list[tuple[int, int]]) -> range:
    last = -1
    s_idx = 0
    for match in _YN_PROMPT_RE.finditer(line):
        while s_idx < len(raw_spans) and raw_spans[s_idx][1] <= match.start():
            s_idx += 1
        if s_idx == len(raw_spans) or match.end() <= raw_spans[s_idx][0]:
            last = match.start()
    return range(last + 1)


def _raw_inline_span_end(
    line: str,
    match: re.Match[str],
    sdata: _SquoteLineData,
    bdata: _BtickLineData,
    odd_bs: set[int] | frozenset[int],
    dquote_ends: dict[int, int],
    tpos_cache: dict[str, tuple[int, ...]],
) -> int | None:
    tok = match.group(0)
    pos = match.start()
    if tok.startswith("\\"):
        return None
    if tok in ('"""', "'''"):
        tpos = _get_triple_positions(tpos_cache, line, tok, sdata, bdata, odd_bs, dquote_ends)
        return _match_triple_pos(pos, tpos)
    if tok[0] == "`":
        return _match_btick_span_at(pos, len(tok), bdata)
    if tok == '"':
        return dquote_ends.get(pos)
    return _match_squote_span_at(line, pos, sdata)


def _collect_raw_inline_spans(
    line: str,
    sdata: _SquoteLineData,
    bdata: _BtickLineData,
    odd_bs: set[int] | frozenset[int],
) -> list[tuple[int, int]]:
    dquote_ends = _prepare_dquote_ends(line, bdata)
    tpos_cache: dict[str, tuple[int, ...]] = {}
    raw_spans: list[tuple[int, int]] = []
    skip_until = 0
    for match in _INLINE_TOKEN_RE.finditer(line):
        pos = match.start()
        if pos < skip_until:
            continue
        end_pos = _raw_inline_span_end(line, match, sdata, bdata, odd_bs, dquote_ends, tpos_cache)
        if end_pos is not None:
            skip_until = end_pos
            raw_spans.append((pos, end_pos))
    return raw_spans


def _is_blocked_inline_span(pos: int, end_pos: int, ctx: _InlineLineCtx) -> bool:
    if _borrows_outer_modal(pos, end_pos - 1, ctx.m_idx):
        return True
    return (ctx.has_choice_below or end_pos in ctx.yn_limit) and _overlaps_modal(pos, end_pos, ctx.m_idx)


def _strip_inline_quotes_on_line(
    line: str,
    has_choice_below: bool,
    follow_probe: str = "",
    prev_probe: str = "",
) -> str:
    prefix = f"{prev_probe} " if prev_probe else ""
    combined_src = f"{prefix}{line} {follow_probe}" if follow_probe else f"{prefix}{line}"
    m_idx = _compute_modal_index(combined_src, len(prefix))
    odd_bs = _odd_backslash_ends(line)
    bdata = _prepare_btick_line_data(line, odd_bs) if "`" in line else _EMPTY_BTICK_DATA
    sdata = _prepare_squote_line_data(line, m_idx, bdata) if "'" in line else _EMPTY_SQUOTE_DATA
    raw_spans = _collect_raw_inline_spans(line, sdata, bdata, odd_bs)
    yn_limit = _yn_limit_outside_spans(line, raw_spans) if m_idx.ranges else range(0)
    ctx = _InlineLineCtx(
        m_idx=m_idx,
        sdata=sdata,
        bdata=bdata,
        has_choice_below=has_choice_below,
        yn_limit=yn_limit,
    )
    spans = [(start, end) for start, end in raw_spans if not _is_blocked_inline_span(start, end, ctx)]
    return _erase_spans(line, spans)


def _is_edge_close_match(line: str, pos: int, q_char: str) -> bool:
    if not _is_close_delim_at(line, pos, q_char):
        return False
    if q_char != "'" or pos < 2 or _PLURAL_END_RE.fullmatch(line, pos - 2, pos) is None:
        return True
    return _TRAILING_PROSE_RE.match(line, pos + 1) is not None and _possessive_match(line, pos) is None


def _advance_edge_quote_state(
    line: str,
    pos: int,
    q_char: str,
    open_pos: int | None,
    close_pos: int | None,
) -> tuple[int | None, int | None]:
    is_close = _is_edge_close_match(line, pos, q_char)
    if open_pos is not None and is_close:
        return None, close_pos
    if close_pos is None and open_pos is None and is_close:
        close_pos = pos
    if _EDGE_OPEN_MAP[q_char].match(line, pos) is not None:
        open_pos = pos
    return open_pos, close_pos


def _line_edge_quote(line: str, q_char: str) -> tuple[int | None, int | None]:
    open_pos: int | None = None
    close_pos: int | None = None
    for match in _EDGE_TOKEN_MAP[q_char].finditer(line):
        if match.group(0) == q_char:
            open_pos, close_pos = _advance_edge_quote_state(line, match.start(), q_char, open_pos, close_pos)
    return open_pos, close_pos


def _span_straddles_multiline_outer(
    lines: list[str],
    open_idx: int,
    open_pos: int,
    close_idx: int,
    close_pos: int,
    span_clean: str,
) -> bool:
    prefix_clean = lines[open_idx][max(0, open_pos - 256) : open_pos].strip().translate(_DELIM_TRANS)
    close_after = lines[close_idx][close_pos + 1 : close_pos + 1024]
    follow_after = " ".join(p.strip() for p in lines[close_idx + 1 : close_idx + 3] if p.strip())
    outer_clean = f"{close_after} {follow_after}".translate(_DELIM_TRANS)
    lead = f"{prefix_clean} " if prefix_clean else ""
    combined = f"{lead}{span_clean}{outer_clean}"
    inner_start = len(lead)
    inner_end = inner_start + len(span_clean)
    return any(
        (m.start() < inner_start < m.end()) or (m.start() < inner_end < m.end())
        for m in MODAL_APPROVAL_RE.finditer(combined)
    )


def _midline_span_contains_modal(
    lines: list[str],
    open_idx: int,
    open_pos: int,
    close_idx: int,
    close_pos: int,
) -> bool:
    open_tail = lines[open_idx][open_pos + 1 :].strip()
    segs = [*(lines[k].strip() for k in range(open_idx + 1, close_idx)), lines[close_idx][:close_pos].strip()]
    span_clean = " ".join(s for s in [open_tail, *segs] if s).translate(_DELIM_TRANS)
    if _span_straddles_multiline_outer(lines, open_idx, open_pos, close_idx, close_pos, span_clean):
        return True
    if MODAL_APPROVAL_RE.search(open_tail) and not MODAL_APPROVAL_RE.match(open_tail):
        return True
    if not MODAL_APPROVAL_RE.search(span_clean):
        return False
    if TRANSCRIPT_STOP_RE.match(lines[open_idx].lstrip()):
        return lines[close_idx][:close_pos].strip().startswith("?")
    if close_idx - open_idx > 1:
        return True
    return (
        _TRAILING_PROSE_RE.match(lines[close_idx], close_pos + 1) is None
        or _TAIL_Q_OR_YN_RE.search(lines[close_idx], close_pos + 1) is not None
    )


def _has_modal_followup_line(line: str) -> bool:
    return bool(_MODAL_FOLLOW_CHOICE_RE.match(line) or _YN_FOLLOWUP_LINE_RE.match(line))


def _has_choice_or_guard_line(line: str) -> bool:
    stripped = line.strip()
    return bool(
        _MODAL_GUARD_LINE_RE.search(line) or _has_modal_followup_line(line) or _MODAL_CURSOR_BUTTON_RE.match(stripped)
    )


def _can_bridge_multiline(
    lines: list[str],
    open_idx: int,
    open_pos: int,
    close_idx: int,
    close_pos: int,
) -> bool:
    if close_idx - open_idx > 24:
        return False
    if any(_has_modal_followup_line(line) for line in lines[close_idx + 1 : close_idx + 9]):
        return False
    span_lines = [*lines[open_idx + 1 : close_idx], lines[close_idx][:close_pos]]
    if any(_has_choice_or_guard_line(seg) for seg in span_lines):
        return False
    return not _midline_span_contains_modal(lines, open_idx, open_pos, close_idx, close_pos)


def _apply_multiline_bridge(
    lines: list[str],
    o_idx: int,
    o_pos: int,
    close_idx: int,
    close_pos: int,
    q_char: str,
) -> tuple[int, int] | None:
    lines[o_idx] = lines[o_idx][:o_pos]
    for mid in range(o_idx + 1, close_idx):
        lines[mid] = ""
    lines[close_idx] = lines[close_idx][close_pos + 1 :]
    rem_open = _line_edge_quote(lines[close_idx], q_char)[0]
    return (close_idx, rem_open) if rem_open is not None else None


def _clear_enclosed_actives(
    active: dict[str, tuple[int, int] | None],
    start: tuple[int, int],
    end: tuple[int, int],
) -> None:
    for other, opos in list(active.items()):
        if opos is not None and start < opos < end:
            active[other] = None


def _try_bridge_line_quotes(
    lines: list[str],
    idx: int,
    active: dict[str, tuple[int, int] | None],
) -> str | None:
    live = sorted(
        ((pos, ch) for ch, pos in active.items() if pos is not None),
        key=lambda item: item[0],
    )
    for (o_idx, o_pos), ch in live:
        close_pos = _line_edge_quote(lines[idx], ch)[1]
        if close_pos is not None and _can_bridge_multiline(lines, o_idx, o_pos, idx, close_pos):
            _clear_enclosed_actives(active, (o_idx, o_pos), (idx, close_pos))
            active[ch] = _apply_multiline_bridge(lines, o_idx, o_pos, idx, close_pos, ch)
            return ch
    return None


def _strip_multiline_quotes(lines: list[str]) -> None:
    active: dict[str, tuple[int, int] | None] = {'"': None, "'": None, "`": None}
    for idx in range(len(lines)):
        bridged = _try_bridge_line_quotes(lines, idx, active)
        for ch in ('"', "'", "`"):
            if ch != bridged:
                open_pos = _line_edge_quote(lines[idx], ch)[0]
                if open_pos is not None:
                    active[ch] = (idx, open_pos)


def _has_inline_modal_followup(cleaned: list[str], idx: int) -> bool:
    return any(_has_modal_followup_line(cleaned[k]) for k in range(idx + 1, min(len(cleaned), idx + 5)))


def _unquote_cleaned_line(cleaned: list[str], idx: int) -> str:
    line = cleaned[idx]
    if _STRAY_DELIM_RE.search(line) is None:
        return line
    has_choice = _has_inline_modal_followup(cleaned, idx)
    non_empty_prev = [part.strip() for part in cleaned[max(0, idx - 2) : idx] if part.strip()][-2:]
    non_empty_follow = [part.strip() for part in cleaned[idx + 1 : idx + 5] if part.strip()][:2]
    prev_probe = " ".join(non_empty_prev)[-256:]
    follow_probe = " ".join(non_empty_follow)[:256]
    return _strip_inline_quotes_on_line(line, has_choice, follow_probe, prev_probe)


def _pre_unquote_lines(lines: list[str], excluded: set[int]) -> list[str]:
    if not lines:
        return []
    cleaned = ["" if idx in excluded else _strip_box_edges_preserve_indent(line) for idx, line in enumerate(lines)]
    _strip_fenced_blocks(cleaned)
    unquoted = [_unquote_cleaned_line(cleaned, idx) for idx in range(len(cleaned))]
    _strip_docstring_blocks(unquoted)
    _strip_multiline_quotes(unquoted)
    return unquoted


def _is_modal_button_line(line: str, allow_numbered: bool) -> bool:
    if _MODAL_CURSOR_BUTTON_RE.match(line):
        return True
    return bool(allow_numbered and _MODAL_NUMBERED_BUTTON_RE.match(line))


def _has_numbered_confirm_modal(non_empty: list[str], stripped_lines: list[str]) -> bool:
    choice_idxs = [idx for idx, line in enumerate(non_empty) if _NUMBERED_CONFIRM_CHOICE_RE.match(line)]
    if not choice_idxs:
        return False
    before_choices = " ".join(s for s in stripped_lines[: choice_idxs[-1]] if s)
    return _DO_YOU_WANT_Q_RE.search(before_choices) is not None


def _matches_modal_pattern(
    lines: list[str],
    pattern: re.Pattern[str],
    *,
    allow_numbered_buttons: bool,
) -> bool:
    non_empty = [line.strip() for line in lines if line.strip()]
    if not non_empty:
        return False
    if any(_is_modal_button_line(line, allow_numbered_buttons) for line in non_empty):
        return True
    stripped_lines = [line.translate(_DELIM_TRANS).strip() for line in non_empty]
    joined = " ".join(s for s in stripped_lines if s)
    if pattern.search(joined):
        return True
    return _has_numbered_confirm_modal(non_empty, stripped_lines)


def _is_modal_break_line(raw_line: str) -> bool:
    if _is_barrier_line(raw_line):
        return True
    return raw_line.lstrip(" \t").startswith(("└", "╭", "┌"))


def _truncated_composer_excluded_lines(regions: CaptureRegions) -> set[int]:
    excluded = {regions.composer_start}
    for idx in range(regions.composer_start + 1, regions.body_end):
        if _is_modal_break_line(regions.lines[idx]):
            break
        excluded.add(idx)
    return excluded


def _is_menu_tail_line(line: str) -> bool:
    stripped = line.lstrip(" \t")
    if line[:1].isspace():
        return not _BULLET_PREFIX_RE.match(stripped)
    return bool(_NUMBERED_ITEM_RE.match(line) or _is_cursor_modal_item(stripped))


def _tail_needs_menu_expansion(tail: list[str]) -> bool:
    if any(_MODAL_FOLLOW_CHOICE_RE.match(line) for line in tail):
        return True
    return sum(1 for line in tail if _is_menu_tail_line(line)) >= 2


def _collect_modal_tail(
    sanitized: list[str],
    end: int,
    excluded: set[int],
    initial_limit: int,
    prompt_stop_end: int,
    custom_re: re.Pattern[str] | None = None,
) -> list[str]:
    tail = _collect_trailing_modal_lines(
        sanitized,
        end,
        excluded,
        initial_limit,
        prompt_stop_end=prompt_stop_end,
        custom_re=custom_re,
    )
    if _tail_needs_menu_expansion(tail):
        return _collect_trailing_modal_lines(
            sanitized,
            end,
            excluded,
            _MODAL_WINDOW_LINES * 4,
            prompt_stop_end=prompt_stop_end,
            custom_re=custom_re,
        )
    return tail


def _clean_composer_modal_match(prompt_line: str, tail: list[str]) -> bool:
    if PLACEHOLDER_COMPOSER_RE.match(prompt_line):
        non_bullet = [line for line in tail if not _BULLET_PREFIX_RE.match(line.strip())]
        return _matches_modal_pattern(non_bullet, MODAL_APPROVAL_RE, allow_numbered_buttons=False)
    return _matches_modal_pattern(tail, MODAL_STRONG_RE, allow_numbered_buttons=False)


def _detect_modal_overlay(
    regions: CaptureRegions,
    dispatch_id: str = "",
    *,
    exclude_truncated_composer: bool = False,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    excluded = _dispatch_excluded_lines(regions.lines, dispatch_id, custom_re)
    if exclude_truncated_composer and not regions.composer_clean:
        excluded.update(_truncated_composer_excluded_lines(regions))
    sanitized = _pre_unquote_lines(regions.lines[: regions.body_end], excluded)
    if regions.composer_clean:
        prompt_line = _strip_box_borders(regions.lines[regions.composer_start]).strip()
        tail = _collect_modal_tail(
            sanitized,
            regions.composer_start,
            excluded,
            6,
            regions.composer_start,
            custom_re,
        )
        return _clean_composer_modal_match(prompt_line, tail)
    running = _has_running_footer(regions.lines, regions.body_end)
    modal_re = MODAL_STRONG_RE if running else MODAL_APPROVAL_RE
    ordered = _collect_modal_tail(
        sanitized,
        regions.body_end,
        excluded,
        _MODAL_WINDOW_LINES,
        regions.composer_start,
        custom_re,
    )
    return _matches_modal_pattern(ordered, modal_re, allow_numbered_buttons=not running)


def _has_pre_existing_marker(pre_text: str, dispatch_id: str) -> bool:
    norm = normalize_lines(pre_text.splitlines())
    return any(marker in norm.text for marker in markers_for_id(dispatch_id))


def _has_dirty_composer_text(regions: CaptureRegions, composer_re: re.Pattern[str]) -> bool:
    if regions.composer_clean:
        return False
    composer_body = "\n".join(regions.lines[regions.composer_start : regions.body_end]).strip()
    return bool(composer_body) and not _is_clean_composer_line(composer_body, composer_re)


def check_pre_capture(pre_text: str, dispatch_id: str) -> tuple[bool, str]:
    """Validate pre-submission pane for marker collision or blocking modal UI."""
    if _has_pre_existing_marker(pre_text, dispatch_id):
        return False, "dispatch marker already existed before submission"
    composer_re = _compile_env_regex("ORCHESTRATION_COMPOSER_REGEX", DEFAULT_COMPOSER_PATTERN)
    custom_re = _compile_custom_prompt_prefix()
    regions = segment_capture(pre_text, composer_re)
    if _detect_modal_overlay(regions, custom_re=custom_re):
        return False, "blocked by active modal/approval UI"
    if _has_dirty_composer_text(regions, composer_re):
        return False, "composer already holds unrelated unsubmitted input"
    return True, "ok"


def _extract_trimmed_body(regions: CaptureRegions) -> list[str]:
    return [
        _strip_box_borders(line).strip()
        for line in regions.lines[: regions.composer_start]
        if _strip_box_borders(line).strip()
    ]


def _has_transcript_change(
    pre_text: str,
    post_regions: CaptureRegions,
    composer_re: re.Pattern[str],
) -> bool:
    pre_regions = segment_capture(pre_text, composer_re)
    pre_body = _extract_trimmed_body(pre_regions)
    post_slice = post_regions.lines[: post_regions.composer_start]
    cur_body = _extract_trimmed_body(post_regions)
    post_footer = _has_running_footer(post_regions.lines, post_regions.body_end)
    is_running = post_footer or any(TURN_RUNNING_RE.search(_strip_box_borders(line)) for line in post_slice)
    changed = pre_body != cur_body or (post_footer and pre_regions.composer_clean and not post_regions.composer_clean)
    return changed and is_running


def _all_accs_inside_pairs(
    t_start: list[MarkerSpan],
    t_end: list[MarkerSpan],
    t_acc: list[MarkerSpan],
) -> bool:
    pair_idx = 0
    n_pairs = len(t_start)
    for acc in t_acc:
        while pair_idx < n_pairs and t_end[pair_idx].start < acc.end:
            pair_idx += 1
        if pair_idx == n_pairs or acc.start < t_start[pair_idx].end:
            return False
    return True


def _has_balanced_unaccepted_pairs(
    t_start: list[MarkerSpan],
    t_end: list[MarkerSpan],
    t_acc: list[MarkerSpan],
) -> bool:
    if not t_start or len(t_start) != len(t_end):
        return False
    for idx, (start, end) in enumerate(zip(t_start, t_end)):
        if start.end > end.start:
            return False
        if idx + 1 < len(t_start) and end.end > t_start[idx + 1].start:
            return False
    return _all_accs_inside_pairs(t_start, t_end, t_acc)


def _all_ends_have_disjoint_chips(
    lines: list[str],
    floor_idx: int,
    ends: list[MarkerSpan],
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    cur_floor = floor_idx
    for span in ends:
        if not _has_contiguous_chip_above(lines, cur_floor, span.start_line, custom_re):
            return False
        cur_floor = span.end_line + 1
    return True


def _is_intact_chip_redraw(
    c_start: list[MarkerSpan],
    t_start: list[MarkerSpan],
    t_end: list[MarkerSpan],
    t_acc: list[MarkerSpan],
    regions: CaptureRegions,
    custom_re: re.Pattern[str] | None,
) -> bool:
    if c_start or t_start or t_acc:
        return False
    return _all_ends_have_disjoint_chips(regions.lines, 0, t_end, custom_re)


def _is_split_across_regions(
    c_start: list[MarkerSpan],
    c_end: list[MarkerSpan],
    t_start: list[MarkerSpan],
    t_end: list[MarkerSpan],
    t_acc: list[MarkerSpan],
    regions: CaptureRegions,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    if not ((t_start or t_end) and (c_start or c_end)):
        return False
    intact_composer = len(c_start) == 1 and len(c_end) == 1 and c_start[0].end <= c_end[0].start
    if intact_composer and _has_balanced_unaccepted_pairs(t_start, t_end, t_acc):
        return False
    return not _is_intact_chip_redraw(c_start, t_start, t_end, t_acc, regions, custom_re)


def _composer_markers_intact(
    c_start: list[MarkerSpan],
    c_end: list[MarkerSpan],
    contiguous_chip: bool,
) -> bool:
    if len(c_start) == 1 and c_end and c_start[0].end <= c_end[0].start:
        if len(c_end) == 1:
            return True
        if contiguous_chip and len(c_end) == 2:
            return True
    return not c_start and len(c_end) == 1 and contiguous_chip


def _is_partial_composer(
    *,
    c_start: list[MarkerSpan],
    c_end: list[MarkerSpan],
    t_start: list[MarkerSpan],
    t_end: list[MarkerSpan],
    t_acc: list[MarkerSpan],
    composer_text: str,
    transcript_changed: bool,
    regions: CaptureRegions,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    if _is_split_across_regions(c_start, c_end, t_start, t_end, t_acc, regions, custom_re):
        return True
    if c_start or c_end:
        contiguous_chip = bool(
            c_end
            and _has_contiguous_chip_above(
                regions.lines,
                regions.composer_start,
                c_end[-1].start_line,
                custom_re,
            )
        )
        return not _composer_markers_intact(c_start, c_end, contiguous_chip)
    has_chip = bool(PASTE_CHIP_RE.search(composer_text))
    return has_chip and transcript_changed


def _has_queue_banner(lines: list[str], s_line: int, e_line: int, queued_re: re.Pattern[str]) -> bool:
    window_start = max(0, s_line - _QUEUE_BANNER_BEFORE)
    window_end = min(len(lines), e_line + _QUEUE_BANNER_AFTER)
    for idx in range(window_start, window_end):
        if s_line < idx <= e_line:
            continue
        cleaned = _strip_box_borders(lines[idx])
        if idx == s_line:
            cleaned = cleaned.partition("[dispatch:")[0]
        if cleaned and queued_re.search(cleaned):
            return True
    return False


def _has_post_acceptance_activity(lines: list[str], after_line: int, limit: int, activity_re: re.Pattern[str]) -> bool:
    return any(activity_re.search(_strip_box_borders(lines[idx])) for idx in range(after_line + 1, limit))


def _first_end_after(t_end: list[MarkerSpan], end_starts: list[int], start_pos: int) -> MarkerSpan | None:
    idx = bisect_left(end_starts, start_pos)
    return t_end[idx] if idx < len(t_end) else None


def _has_acc_between(acc_spans: list[MarkerSpan], acc_starts: list[int], left: int, right: int) -> bool:
    idx = bisect_left(acc_starts, left)
    return idx < len(acc_spans) and acc_spans[idx].end <= right


def _pick_accepted_prior_pair(
    t_start: list[MarkerSpan],
    t_end: list[MarkerSpan],
    end_starts: list[int],
    acc_spans: list[MarkerSpan],
    acc_starts: list[int],
) -> tuple[MarkerSpan, MarkerSpan] | None:
    limit = t_start[-1].start
    for start_span in reversed(t_start[:-1]):
        end_span = _first_end_after(t_end, end_starts, start_span.end)
        if end_span is not None and _has_acc_between(acc_spans, acc_starts, end_span.end, limit):
            return (start_span, end_span)
    return None


def _ordered_dispatch_spans(
    t_start: list[MarkerSpan],
    t_end: list[MarkerSpan],
    t_acc: list[MarkerSpan] | None = None,
) -> tuple[MarkerSpan, MarkerSpan] | None:
    if not t_start or not t_end:
        return None
    acc_spans = t_acc or []
    end_starts = [sp.start for sp in t_end]
    last_start = t_start[-1]
    last_end = _first_end_after(t_end, end_starts, last_start.end)
    if last_end is not None and acc_spans and acc_spans[-1].start >= last_end.end:
        return (last_start, last_end)
    acc_starts = [sp.start for sp in acc_spans]
    prior = _pick_accepted_prior_pair(t_start, t_end, end_starts, acc_spans, acc_starts)
    if prior is not None:
        return prior
    return (last_start, last_end) if last_end is not None else None


def _verdict(
    state: DeliveryState,
    dispatch_id: str,
    reason: str,
    composer_clean: bool,
    tab_to_queue: bool = False,
) -> Classification:
    return Classification(
        state=state,
        dispatch_id=dispatch_id,
        reason=reason,
        composer_clean=composer_clean,
        tab_to_queue=tab_to_queue,
    )


def _classify_accepted_turn(
    *,
    dispatch_id: str,
    regions: CaptureRegions,
    acc_span: MarkerSpan,
    activity_re: re.Pattern[str],
    tab_to_queue: bool,
) -> Classification:
    has_act = _has_post_acceptance_activity(regions.lines, acc_span.end_line, regions.composer_start, activity_re)
    if has_act and regions.composer_clean:
        return _verdict(
            DeliveryState.SUBMITTED,
            dispatch_id,
            "ordered start/end/acceptance/activity/clean-composer verified",
            True,
            tab_to_queue,
        )
    return _verdict(
        DeliveryState.ACCEPTED_ACTIVE,
        dispatch_id,
        "dispatch was accepted but the clean composer has not returned",
        regions.composer_clean,
        tab_to_queue,
    )


def _is_chip_prompt_line(cleaned: str, custom_re: re.Pattern[str] | None) -> bool:
    if TUI_PROMPT_RE.match(cleaned) or _CHIP_PREFIX_RE.match(cleaned):
        return True
    return custom_re is not None and custom_re.match(cleaned) is not None


def _should_stop_chip_walk(
    inner: str,
    stripped: str,
    is_before_end: bool,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    if _is_chip_prompt_line(stripped, custom_re):
        return True
    if inner[:1].isspace():
        return False
    return is_before_end or not stripped.startswith("[dispatch")


def _find_contiguous_chip_line(
    lines: list[str],
    floor_idx: int,
    end_line: int,
    custom_re: re.Pattern[str] | None = None,
) -> int | None:
    for idx in range(end_line, floor_idx - 1, -1):
        inner = _strip_box_edges_preserve_indent(lines[idx])
        stripped = inner.strip()
        if not stripped:
            continue
        if PASTE_CHIP_RE.search(stripped) and _is_chip_prompt_line(stripped, custom_re):
            return idx
        if _should_stop_chip_walk(inner, stripped, idx < end_line, custom_re):
            return None
    return None


def _has_contiguous_chip_above(
    lines: list[str],
    floor_idx: int,
    end_line: int,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    return _find_contiguous_chip_line(lines, floor_idx, end_line, custom_re) is not None


def _end_acceptance_at(
    ends: list[MarkerSpan],
    idx: int,
    accs: list[MarkerSpan],
    acc_starts: list[int],
) -> MarkerSpan | None:
    cur_end = ends[idx]
    pos = bisect_left(acc_starts, cur_end.end)
    if pos >= len(accs):
        return None
    acc = accs[pos]
    upper = ends[idx + 1].start if idx + 1 < len(ends) else None
    if upper is not None and acc.end > upper:
        return None
    return acc


def _post_end_acceptance(ends: list[MarkerSpan], accs: list[MarkerSpan]) -> MarkerSpan | None:
    if not ends:
        return None
    return _end_acceptance_at(ends, 0, accs, [sp.start for sp in accs])


def _find_chip_end_acceptance(
    lines: list[str],
    floor_idx: int,
    ends: list[MarkerSpan],
    accs: list[MarkerSpan],
    custom_re: re.Pattern[str] | None,
) -> MarkerSpan | None:
    acc_starts = [sp.start for sp in accs]
    cur_floor = floor_idx
    for idx, span in enumerate(ends):
        if not _has_contiguous_chip_above(lines, cur_floor, span.start_line, custom_re):
            return None
        cur_floor = span.end_line + 1
        acc = _end_acceptance_at(ends, idx, accs, acc_starts)
        if acc is not None:
            return acc
    return None


def _accepted_chip_span(
    lines: list[str],
    floor_idx: int,
    starts: list[MarkerSpan],
    ends: list[MarkerSpan],
    accs: list[MarkerSpan],
    custom_re: re.Pattern[str] | None = None,
) -> MarkerSpan | None:
    acc = _find_chip_end_acceptance(lines, floor_idx, ends, accs, custom_re)
    if acc is None:
        return None
    return None if any(s.start < acc.end for s in starts) else acc


def _classify_transcript_state(
    *,
    dispatch_id: str,
    regions: CaptureRegions,
    t_start: list[MarkerSpan],
    t_end: list[MarkerSpan],
    t_acc: list[MarkerSpan],
    activity_re: re.Pattern[str],
    queued_re: re.Pattern[str],
    tab_to_queue: bool,
    custom_re: re.Pattern[str] | None = None,
) -> Classification:
    chip_acc = _accepted_chip_span(regions.lines, 0, t_start, t_end, t_acc, custom_re)
    if chip_acc is not None:
        return _classify_accepted_turn(
            dispatch_id=dispatch_id,
            regions=regions,
            acc_span=chip_acc,
            activity_re=activity_re,
            tab_to_queue=tab_to_queue,
        )
    ordered = _ordered_dispatch_spans(t_start, t_end, t_acc)
    if ordered is None:
        reason = (
            "dispatch markers appeared out of order"
            if (t_start and t_end)
            else "dispatch lacks ordered start/end/acceptance/activity/clean-composer evidence"
        )
        return _verdict(DeliveryState.AMBIGUOUS, dispatch_id, reason, regions.composer_clean, tab_to_queue)

    start_span, end_span = ordered
    post_acc = [span for span in t_acc if span.start >= end_span.end]
    if post_acc:
        return _classify_accepted_turn(
            dispatch_id=dispatch_id,
            regions=regions,
            acc_span=post_acc[0],
            activity_re=activity_re,
            tab_to_queue=tab_to_queue,
        )

    is_queued = regions.composer_clean and _has_queue_banner(
        regions.lines, start_span.start_line, end_span.end_line, queued_re
    )
    if is_queued:
        return _verdict(
            DeliveryState.QUEUED,
            dispatch_id,
            "dispatch is queued by client without acceptance yet",
            True,
            tab_to_queue,
        )

    return _verdict(
        DeliveryState.AMBIGUOUS,
        dispatch_id,
        "dispatch lacks ordered start/end/acceptance/activity/clean-composer evidence",
        regions.composer_clean,
        tab_to_queue,
    )


def _pre_transcript_is_balanced(
    regions: CaptureRegions,
    start_m: str,
    end_m: str,
    accepted_m: str,
    custom_re: re.Pattern[str] | None = None,
) -> bool:
    pre_norm = normalize_lines(regions.lines[: regions.composer_start], 0)
    pre_s = find_marker_spans(pre_norm, start_m)
    pre_e = find_marker_spans(pre_norm, end_m)
    if not (pre_s or pre_e):
        return True
    pre_acc = find_marker_spans(pre_norm, accepted_m)
    if not (pre_s or pre_acc):
        return _all_ends_have_disjoint_chips(regions.lines, 0, pre_e, custom_re)
    return _has_balanced_unaccepted_pairs(pre_s, pre_e, pre_acc)


def _is_full_composer_accepted(
    c_start: list[MarkerSpan],
    c_end: list[MarkerSpan],
    full_acc: MarkerSpan | None,
) -> bool:
    if full_acc is None or not c_start:
        return False
    if c_start[0].end > c_end[0].start:
        return False
    return not any(s.start < full_acc.end for s in c_start[1:])


def _promote_accepted_composer(
    regions: CaptureRegions,
    start_m: str,
    end_m: str,
    accepted_m: str,
    custom_re: re.Pattern[str] | None = None,
) -> CaptureRegions:
    if not _pre_transcript_is_balanced(regions, start_m, end_m, accepted_m, custom_re):
        return regions
    comp_lines = regions.lines[regions.composer_start : regions.body_end]
    comp_norm = normalize_lines(comp_lines, regions.composer_start)
    c_start = find_marker_spans(comp_norm, start_m)
    c_end = find_marker_spans(comp_norm, end_m)
    c_acc = find_marker_spans(comp_norm, accepted_m)
    chip_acc = _accepted_chip_span(regions.lines, regions.composer_start, c_start, c_end, c_acc, custom_re)
    full_acc = _post_end_acceptance(c_end, c_acc)
    full_ok = _is_full_composer_accepted(c_start, c_end, full_acc)
    if chip_acc is not None or full_ok:
        return CaptureRegions(
            lines=regions.lines,
            body_end=regions.body_end,
            composer_start=regions.body_end,
            composer_clean=False,
        )
    return regions


def _has_tab_to_queue_footer(lines: list[str], body_end: int) -> bool:
    return any(TAB_QUEUE_FOOTER_RE.search(line) for line in lines[body_end:])


def classify_capture(pre_text: str, post_text: str, dispatch_id: str) -> Classification:
    """Classify post-action pane capture cleanly across all delivery outcomes."""
    if _has_pre_existing_marker(pre_text, dispatch_id):
        return _verdict(
            DeliveryState.AMBIGUOUS,
            dispatch_id,
            "dispatch marker already existed before submission",
            False,
        )

    activity_re = _compile_env_regex("ORCHESTRATION_ACTIVITY_REGEX", DEFAULT_ACTIVITY_PATTERN)
    composer_re = _compile_env_regex("ORCHESTRATION_COMPOSER_REGEX", DEFAULT_COMPOSER_PATTERN)
    queued_re = _compile_env_regex("ORCHESTRATION_QUEUED_REGEX", DEFAULT_QUEUED_PATTERN, re.IGNORECASE)
    custom_re = _compile_custom_prompt_prefix()

    start_m, accepted_m, end_m = markers_for_id(dispatch_id)
    regions = segment_capture(post_text, composer_re, start_m)
    tab_to_queue = _has_tab_to_queue_footer(regions.lines, regions.body_end)
    regions = _promote_accepted_composer(regions, start_m, end_m, accepted_m, custom_re)

    comp_lines = regions.lines[regions.composer_start : regions.body_end]
    comp_norm = normalize_lines(comp_lines, regions.composer_start)
    trans_norm = normalize_lines(regions.lines[: regions.composer_start], 0)

    c_start, c_end = find_marker_spans(comp_norm, start_m), find_marker_spans(comp_norm, end_m)
    t_start, t_end = find_marker_spans(trans_norm, start_m), find_marker_spans(trans_norm, end_m)
    t_acc = find_marker_spans(trans_norm, accepted_m)
    comp_raw = "\n".join(comp_lines)

    if _detect_modal_overlay(
        regions,
        dispatch_id,
        exclude_truncated_composer=bool(c_start and not c_end),
        custom_re=custom_re,
    ):
        return _verdict(
            DeliveryState.BLOCKED_MODAL,
            dispatch_id,
            "blocked by active modal/approval UI",
            regions.composer_clean,
            tab_to_queue,
        )

    trans_changed = _has_transcript_change(pre_text, regions, composer_re)

    if _is_partial_composer(
        c_start=c_start,
        c_end=c_end,
        t_start=t_start,
        t_end=t_end,
        t_acc=t_acc,
        composer_text=comp_raw,
        transcript_changed=trans_changed,
        regions=regions,
        custom_re=custom_re,
    ):
        return _verdict(
            DeliveryState.PARTIAL_PASTE,
            dispatch_id,
            "partial paste split or truncated in composer",
            False,
            tab_to_queue,
        )

    if c_end:
        if _has_active_turn_without_queue(regions.lines, regions.body_end):
            return _verdict(
                DeliveryState.AMBIGUOUS,
                dispatch_id,
                "active turn footer present without tab-to-queue support",
                False,
                tab_to_queue,
            )
        return _verdict(
            DeliveryState.UNSUBMITTED,
            dispatch_id,
            "complete directive remains held in active composer",
            False,
            tab_to_queue,
        )

    return _classify_transcript_state(
        dispatch_id=dispatch_id,
        regions=regions,
        t_start=t_start,
        t_end=t_end,
        t_acc=t_acc,
        activity_re=activity_re,
        queued_re=queued_re,
        tab_to_queue=tab_to_queue,
        custom_re=custom_re,
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _run_pre_check(pre_text: str, dispatch_id: str, as_json: bool) -> int:
    ok, reason = check_pre_capture(pre_text, dispatch_id)
    if as_json:
        print(json.dumps({"ok": ok, "reason": reason, "dispatch_id": dispatch_id}))
    else:
        print(f"ok={'1' if ok else '0'}\nreason={reason}")
    if ok:
        return 0
    return 75 if "modal" in reason or "unrelated" in reason else 1


def _run_classify(pre_text: str, capture_path: Path, dispatch_id: str, as_json: bool) -> int:
    verdict = classify_capture(pre_text, _read_text(capture_path), dispatch_id)
    if as_json:
        print(json.dumps(verdict.to_dict()))
    else:
        print(f"delivery_state={verdict.state.value}")
        print(f"composer_clean={'1' if verdict.composer_clean else '0'}")
        print(f"tab_to_queue={'1' if verdict.tab_to_queue else '0'}")
        print(f"reason={verdict.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for bash orchestration helpers."""
    parser = argparse.ArgumentParser(description="Classify interactive tmux dispatch captures.")
    parser.add_argument("command", choices=("pre-check", "classify"))
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--pre", type=Path, required=True)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    pre_text = _read_text(args.pre)
    if args.command == "pre-check":
        return _run_pre_check(pre_text, args.dispatch_id, args.json)
    if args.capture is None:
        parser.error("--capture is required for classify")
    return _run_classify(pre_text, args.capture, args.dispatch_id, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
