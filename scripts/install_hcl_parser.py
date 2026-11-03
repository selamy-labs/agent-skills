#!/usr/bin/env python3
"""Provision the authoritative, structure-preserving hclblocks parser binary from source."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def get_install_destination() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path.home() / ".local" / "share" / "agent-skills" / "bin" / "hclblocks"


def _is_temp_descendant(p: Path) -> bool:
    try:
        resolved = p.resolve()
        for parent in [resolved] + list(resolved.parents):
            if parent in (Path("/tmp"), Path("/var/tmp")):
                return True
    except OSError:
        return True
    return False


def _check_destination_safety(dest: Path) -> None:
    if _is_temp_descendant(dest):
        raise ValueError(f"Refusing to install into temporary directory descendant: {dest}")


def _ensure_private_parent(dest: Path) -> None:
    parent = dest.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)


def _build_hclblocks_binary(dest: Path) -> None:
    go_bin = shutil.which("go")
    if not go_bin:
        raise RuntimeError("Go compiler ('go') not found on PATH; required to build hclblocks")

    repo_root = Path(__file__).resolve().parents[1]
    hclblocks_dir = repo_root / "tools" / "hclblocks"
    if not (hclblocks_dir / "main.go").is_file():
        raise RuntimeError(f"hclblocks source not found at {hclblocks_dir}")

    _ensure_private_parent(dest)
    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"
    env["GOFLAGS"] = "-mod=readonly"
    env.pop("GONOSUMDB", None)
    cmd = ["go", "build", "-trimpath", "-ldflags=-s -w -buildid=", "-o", str(dest), "."]

    res = subprocess.run(cmd, cwd=str(hclblocks_dir), env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to build hclblocks:\n{res.stderr}")

    dest.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _smoke_test(dest: Path) -> None:
    if not dest.is_file():
        raise RuntimeError(f"Installed binary not found at {dest}")
    proc = subprocess.run(
        [str(dest)],
        input=b"locals {}\n",
        capture_output=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"hclblocks smoke test failed (exit {proc.returncode}): {err}")
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
        if not isinstance(data, dict) or not data.get("valid", False):
            raise RuntimeError(f"hclblocks smoke test invalid output: {proc.stdout.decode()}")
    except Exception as exc:
        raise RuntimeError(f"hclblocks smoke test failed parsing output: {exc}")


def install_parser(dest: Path) -> int:
    _check_destination_safety(dest)
    _build_hclblocks_binary(dest)
    _smoke_test(dest)
    print(f"hclblocks installed successfully from source at {dest}")
    return 0


def main() -> int:
    dest = get_install_destination()
    try:
        return install_parser(dest)
    except Exception as exc:
        print(f"Error: provisioning hcl parser failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
