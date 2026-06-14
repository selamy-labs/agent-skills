"""Minimal loader for ``denylist.yml``.

Parses the simple subset of YAML used by the denylist (top-level keys mapping
to lists of scalars, plus ``secret_patterns`` which maps to a list of dicts).
No external dependencies required.
"""

from __future__ import annotations

import re
from pathlib import Path


def load_denylist(path: Path) -> dict:
    """Parse denylist.yml into a dict matching the expected schema."""
    text = path.read_text(encoding="utf-8")
    return _parse(text)


def _parse(text: str) -> dict:
    result: dict = {}
    current_key: str | None = None
    current_list: list | None = None
    in_dict_item = False
    current_dict: dict = {}

    for line in text.splitlines():
        stripped = line.rstrip()

        # Skip blank lines and comments
        if not stripped or stripped.lstrip().startswith("#"):
            continue

        # Top-level key (no leading whitespace, ends with colon)
        if re.match(r"^[a-z_]+:\s*$", stripped):
            _flush(result, current_key, current_list, current_dict, in_dict_item)
            current_key = stripped.rstrip(":").strip()
            current_list = []
            current_dict = {}
            in_dict_item = False
            continue

        # List item with dict start: "  - key: value"
        m_dict_item = re.match(r"^\s+-\s+(\w+):\s*(.+)$", stripped)
        # Simple list item: "  - value"
        m_list_item = re.match(r"^\s+-\s+(.+)$", stripped)
        # Continuation dict key: "    key: value"
        m_cont = re.match(r"^\s{4,}(\w+):\s*(.+)$", stripped)

        if m_dict_item and current_list is not None:
            key, val = m_dict_item.group(1), m_dict_item.group(2).strip()
            if in_dict_item and current_dict:
                current_list.append(dict(current_dict))
            current_dict = {key: _unquote(val)}
            in_dict_item = True
        elif m_cont and in_dict_item:
            key, val = m_cont.group(1), m_cont.group(2).strip()
            current_dict[key] = _unquote(val)
        elif m_list_item and current_list is not None:
            if in_dict_item and current_dict:
                current_list.append(dict(current_dict))
                current_dict = {}
                in_dict_item = False
            current_list.append(_unquote(m_list_item.group(1).strip()))

    _flush(result, current_key, current_list, current_dict, in_dict_item)
    return result


def _flush(result: dict, key: str | None, lst: list | None, dct: dict, in_dict: bool) -> None:
    if key is not None and lst is not None:
        if in_dict and dct:
            lst.append(dict(dct))
        result[key] = lst


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value
