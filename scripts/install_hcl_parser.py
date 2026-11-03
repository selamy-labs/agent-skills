#!/usr/bin/env python3
"""Provision the authoritative, structure-preserving hclblocks parser binary."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HCL_PARSER_VERSION = "1.0.0"

HCL_PARSER_SHA256: dict[str, str] = {
    "linux_amd64": "11ffdceeb328274f843f3670475db3b13b533e99433b2e561263c300be0fa945",
    "linux_arm64": "3bca0680bc3d8e6d7f2810079f85e7c23a9a4a9f3530b1a44047030aafd34cbc",
    "darwin_amd64": "f4e56769fa6399e7db3f8006e455158510e394d4c8074fc435e55afb7c89467f",
    "darwin_arm64": "85dc4d8f9af5cc9290e57628459b0a918404013457fc5b37166b1ecbdee1b462",
}


def get_platform_key() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    arch = arch_map.get(machine)
    if system in ("linux", "darwin") and arch in ("amd64", "arm64"):
        return f"{system}_{arch}"
    return None


def get_install_destination() -> Path:
    custom = os.environ.get("HCL_PARSER_PATH") or os.environ.get("HCLBLOCKS_PATH")
    if custom:
        return Path(custom)
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path.home() / ".local" / "bin" / "hclblocks"


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


def _build_hclblocks_binary(dest: Path) -> bytes:
    go_bin = shutil.which("go")
    if not go_bin:
        raise RuntimeError("Go compiler ('go') not found on PATH; required to build hclblocks")

    repo_root = Path(__file__).resolve().parents[1]
    hclblocks_dir = repo_root / "tools" / "hclblocks"
    if not (hclblocks_dir / "main.go").is_file():
        raise RuntimeError(f"hclblocks source not found at {hclblocks_dir}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_target = dest.with_suffix(".tmp")
    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"
    cmd = ["go", "build", "-trimpath", "-ldflags=-s -w -buildid=", "-o", str(temp_target), "main.go"]

    res = subprocess.run(cmd, cwd=str(hclblocks_dir), env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to build hclblocks:\n{res.stderr}")

    data = temp_target.read_bytes()
    temp_target.chmod(
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
    temp_target.replace(dest)
    return data


def _write_checksum_sidecar(dest: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    sha_file = dest.with_suffix(dest.suffix + ".sha256")
    sha_file.write_text(f"{digest}  {dest.name}\n", encoding="utf-8")
    sha_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return digest


def _verify_installed_binary(dest: Path, expected_digest: str) -> None:
    if not dest.is_file():
        raise RuntimeError(f"Installed binary not found at {dest}")
    actual_digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if actual_digest != expected_digest:
        raise RuntimeError(f"Installed binary digest mismatch: got {actual_digest}, expected {expected_digest}")


def install_parser(dest: Path) -> int:
    _check_destination_safety(dest)
    data = _build_hclblocks_binary(dest)
    digest = _write_checksum_sidecar(dest, data)
    _verify_installed_binary(dest, digest)
    print(f"hclblocks v{HCL_PARSER_VERSION} installed successfully at {dest} (SHA-256: {digest})")
    return 0


def main() -> int:
    platform_key = get_platform_key()
    if not platform_key or platform_key not in HCL_PARSER_SHA256:
        print(
            f"Unsupported platform: system={platform.system()}, machine={platform.machine()}",
            file=sys.stderr,
        )
        return 1

    dest = get_install_destination()
    try:
        return install_parser(dest)
    except Exception as exc:
        print(f"Error: provisioning hcl parser failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
