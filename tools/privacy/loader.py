"""Minimal loader for ``denylist.yml``.

Parses the simple subset of YAML used by the denylist (top-level keys mapping
to lists of scalars, plus ``secret_patterns`` which maps to a list of dicts).
No external dependencies required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def load_denylist(path: Path) -> dict[str, Any]:
    """Parse denylist.yml into a dict matching the expected schema."""
    text = path.read_text(encoding="utf-8")
    return _parse(text)


class _DenylistParser:
    def __init__(self) -> None:
        self.result: dict[str, Any] = {}
        self.current_key: str | None = None
        self.current_list: list[Any] | None = None
        self.in_dict_item = False
        self.current_dict: dict[str, str] = {}

    def flush(self) -> None:
        if self.current_key is not None and self.current_list is not None:
            if self.in_dict_item and self.current_dict:
                self.current_list.append(dict(self.current_dict))
            self.result[self.current_key] = self.current_list

    def handle_top_key(self, stripped: str) -> None:
        self.flush()
        self.current_key = stripped.rstrip(":").strip()
        self.current_list = []
        self.current_dict = {}
        self.in_dict_item = False

    def handle_dict_item(self, m: re.Match[str]) -> None:
        if self.current_list is None:
            return
        key, val = m.group(1), m.group(2).strip()
        if self.in_dict_item and self.current_dict:
            self.current_list.append(dict(self.current_dict))
        self.current_dict = {key: _unquote(val)}
        self.in_dict_item = True

    def handle_cont(self, m: re.Match[str]) -> None:
        if self.in_dict_item:
            key, val = m.group(1), m.group(2).strip()
            self.current_dict[key] = _unquote(val)

    def handle_list_item(self, m: re.Match[str]) -> None:
        if self.current_list is None:
            return
        if self.in_dict_item and self.current_dict:
            self.current_list.append(dict(self.current_dict))
            self.current_dict = {}
            self.in_dict_item = False
        self.current_list.append(_unquote(m.group(1).strip()))

    def process_line(self, line: str) -> None:
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            return

        if re.match(r"^[a-z_]+:\s*$", stripped):
            self.handle_top_key(stripped)
            return

        m_dict_item = re.match(r"^\s+-\s+(\w+):\s*(.+)$", stripped)
        if m_dict_item:
            self.handle_dict_item(m_dict_item)
            return

        m_cont = re.match(r"^\s{4,}(\w+):\s*(.+)$", stripped)
        if m_cont:
            self.handle_cont(m_cont)
            return

        m_list_item = re.match(r"^\s+-\s+(.+)$", stripped)
        if m_list_item:
            self.handle_list_item(m_list_item)

    def parse(self, text: str) -> dict[str, Any]:
        for line in text.splitlines():
            self.process_line(line)
        self.flush()
        return self.result


def _parse(text: str) -> dict[str, Any]:
    return _DenylistParser().parse(text)


def _flush(
    result: dict[str, Any],
    key: str | None,
    lst: list[Any] | None,
    dct: dict[str, str],
    in_dict: bool,
) -> None:
    if key is not None and lst is not None:
        if in_dict and dct:
            lst.append(dict(dct))
        result[key] = lst


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value
