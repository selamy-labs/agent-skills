#!/usr/bin/env python3
"""Provision the pinned, SHA-256-verified hcl2json parser binary."""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
import urllib.request
from pathlib import Path

HCL2JSON_VERSION = "0.6.9"

HCL2JSON_SHA256: dict[str, str] = {
    "linux_amd64": "b609e37094948c0b77cd8c6c0baaf8af2bd168c685c6e3334d671cc9692c34a5",
    "linux_arm64": "83ec38fa1583ea7dd5ceaa7fe63df0b3bc7beea6552554e2079bb178553251ca",
    "darwin_amd64": "dc1b8ef09746e8f498bc5eb7d949ad507a7ec5b3648fa3982cb97940251785cf",
    "darwin_arm64": "ec9306b47c1f8682e0717203ce893b8e5a60e0a5c0e181c4e85d9967657b98d2",
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


def get_download_url(platform_key: str) -> str:
    return f"https://github.com/tmccombs/hcl2json/releases/download/v{HCL2JSON_VERSION}/hcl2json_{platform_key}"


def download_and_verify(url: str, expected_sha256: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "agent-skills-install/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data: bytes = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f"SHA-256 mismatch for downloaded binary: expected {expected_sha256}, got {digest}")
    return data


def install_binary_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_suffix(".tmp")
    temp_target.write_bytes(data)
    temp_target.chmod(
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
    temp_target.replace(target)


def get_install_destinations() -> list[Path]:
    custom_dir = os.environ.get("HCL2JSON_INSTALL_DIR")
    if custom_dir:
        return [Path(custom_dir) / "hcl2json"]
    if len(sys.argv) > 1:
        return [Path(sys.argv[1])]
    return [Path(sys.prefix) / "bin" / "hcl2json"]


def _is_destination_already_installed(dest: Path, expected_sha256: str) -> bool:
    if not dest.is_file():
        return False
    try:
        return hashlib.sha256(dest.read_bytes()).hexdigest() == expected_sha256
    except OSError:
        return False


def main() -> int:
    platform_key = get_platform_key()
    if not platform_key or platform_key not in HCL2JSON_SHA256:
        print(
            f"Unsupported platform: system={platform.system()}, machine={platform.machine()}",
            file=sys.stderr,
        )
        return 1

    expected_sha256 = HCL2JSON_SHA256[platform_key]
    destinations = get_install_destinations()

    if all(_is_destination_already_installed(d, expected_sha256) for d in destinations):
        print(f"hcl2json v{HCL2JSON_VERSION} already installed and verified at {destinations[0]}")
        return 0

    url = get_download_url(platform_key)
    print(f"Downloading hcl2json v{HCL2JSON_VERSION} for {platform_key}...")
    try:
        data = download_and_verify(url, expected_sha256)
    except Exception as exc:
        print(f"Failed to download and verify hcl2json: {exc}", file=sys.stderr)
        return 1

    for dest in destinations:
        try:
            install_binary_bytes(dest, data)
            print(f"Installed hcl2json to {dest}")
        except OSError as exc:
            print(f"Warning: could not install to {dest}: {exc}", file=sys.stderr)

    print("hcl2json installation verified successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
