#!/usr/bin/env python3
"""Constrain Gemini CLI 0.51 container-runtime invocations.

This executable is copied to a private directory as ``docker`` or ``podman``
and placed first on Gemini's PATH. It never interprets shell input.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

SECRET_ENV_PREFIXES = (
    "GOOGLE_API_KEY=",
    "GEMINI_GATEWAY_API_KEY=",
)
FORBIDDEN_FLAGS = {"--privileged", "--cap-add", "--device", "--pid", "--ipc", "--uts"}
MAIN_NETWORK = "gemini-cli-sandbox"
PROXY_NETWORK = "gemini-cli-sandbox-proxy"
LOOPBACK = ".".join(("127", "0", "0", "1"))


def fail(message: str) -> NoReturn:
    print(f"orchestrate-gemini provider guard: {message}", file=sys.stderr)
    raise SystemExit(64)


def private_config() -> tuple[Path, Path, Path, str, str, list[Path], bool]:
    try:
        real = Path(os.environ["ORCHESTRATE_GEMINI_PROVIDER_REAL"])
        workspace = Path(os.environ["ORCHESTRATE_GEMINI_WORKSPACE"]).resolve()
        state = Path(os.environ["ORCHESTRATE_GEMINI_STATE_DIR"]).resolve()
        image = os.environ["ORCHESTRATE_GEMINI_SANDBOX_IMAGE"]
        label = os.environ["ORCHESTRATE_GEMINI_CONTAINER_LABEL"]
        allowed = [Path(value).resolve() for value in json.loads(os.environ["ORCHESTRATE_GEMINI_ALLOWED_PATHS"])]
        live_value = os.environ["ORCHESTRATE_GEMINI_LIVE_MODE"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        fail(f"invalid private configuration: {error}")
    if not real.is_absolute() or not workspace.is_absolute() or not state.is_absolute():
        fail("control paths must be absolute")
    if live_value not in {"0", "1"}:
        fail("live mode must be explicit")
    return real, workspace, state, image, label, allowed, live_value == "1"


def mount_source(specification: str) -> Path:
    source = specification.split(":", 1)[0]
    if not source.startswith("/"):
        fail("relative and named mounts are forbidden")
    return Path(source).resolve()


def force_read_only(specification: str) -> str:
    fields = specification.split(":")
    if len(fields) == 1:
        return f"{fields[0]}:{fields[0]}:ro"
    if len(fields) == 2:
        return f"{fields[0]}:{fields[1]}:ro"
    options = {item for item in fields[2].split(",") if item not in {"rw", "ro"}}
    options.add("ro")
    return f"{fields[0]}:{fields[1]}:{','.join(sorted(options))}"


def rewrite_pair(
    item: str,
    value: str,
    proxy: bool,
    workspace: Path,
    state: Path,
) -> list[str]:
    if item == "--add-host":
        return []
    if item in {"-p", "--publish"}:
        if not proxy or value != "8877:8877":
            fail("published ports are forbidden")
        return [item, f"{LOOPBACK}:8877:8877"]
    if item in {"-v", "--volume"}:
        source = mount_source(value)
        if source != workspace and workspace not in source.parents and state not in source.parents:
            fail(f"mount source is outside the lane: {source}")
        writable_runtime_mounts = {state / "tmp", state / "gemini-home" / ".gemini"}
        return [item, value if source in writable_runtime_mounts else force_read_only(value)]
    if item == "--env" and value.startswith(SECRET_ENV_PREFIXES):
        fail("Gemini 0.51 attempted secret-bearing runtime argv")
    if item == "--env" and value.startswith("GEMINI_API_KEY="):
        if value != "GEMINI_API_KEY=__ORCHESTRATE_GEMINI_RUNTIME_SECRET__":
            fail("Gemini 0.51 attempted secret-bearing runtime argv")
        credential = os.environ.get("ORCHESTRATE_GEMINI_CREDENTIAL_ENV_FILE", "")
        if not credential or not Path(credential).is_file():
            fail("private credential env file is unavailable")
        return ["--env-file", credential]
    return [item, value]


def rewrite_runtime_options(
    arguments: list[str],
    image_index: int,
    proxy: bool,
    workspace: Path,
    state: Path,
) -> tuple[list[str], list[str]]:
    networks: list[str] = []
    rewritten = arguments[:1]
    index = 1
    paired = {"--add-host", "-p", "--publish", "-v", "--volume", "--env", "--network"}
    while index < image_index:
        item = arguments[index]
        if item in FORBIDDEN_FLAGS or any(item.startswith(f"{flag}=") for flag in FORBIDDEN_FLAGS):
            fail(f"forbidden runtime flag: {item}")
        if item in paired:
            if index + 1 >= image_index:
                fail(f"malformed paired runtime option: {item}")
            if item == "--network":
                networks.append(arguments[index + 1])
                rewritten.extend((item, arguments[index + 1]))
            else:
                rewritten.extend(rewrite_pair(item, arguments[index + 1], proxy, workspace, state))
            index += 2
            continue
        if item.startswith("--network="):
            networks.append(item.split("=", 1)[1])
        rewritten.append(item)
        index += 1
    return rewritten, networks


def validate_networks(proxy: bool, live_mode: bool, networks: list[str]) -> None:
    if proxy:
        if not live_mode or networks != [PROXY_NETWORK]:
            fail("proxy container must use only the fixed external proxy network")
    elif live_mode and networks != [MAIN_NETWORK]:
        fail("live worker must use only Gemini's attested internal network")
    elif not live_mode and networks != ["none"]:
        fail("validation worker must use only the no-network namespace")


def guarded_run(arguments: list[str]) -> list[str]:
    real, workspace, state, image, label, allowed, live_mode = private_config()
    del real
    if image not in arguments:
        fail("runtime image differs from the immutable goal")
    image_index = arguments.index(image)
    proxy = PROXY_NETWORK in arguments
    rewritten, networks = rewrite_runtime_options(arguments, image_index, proxy, workspace, state)
    validate_networks(proxy, live_mode, networks)
    rewritten.extend(("--label", label))
    rewritten.extend(("--label", f"io.selamy.orchestrate-gemini.owner-uid={os.getuid()}"))
    if not proxy:
        for path in allowed:
            rewritten.extend(("--volume", f"{path}:{path}:rw"))
    rewritten.extend(arguments[image_index:])
    return rewritten


def inspect_network(real: Path, name: str, expected_internal: str) -> int:
    result = subprocess.run(
        [str(real), "network", "inspect", "--format", "{{.Internal}}", name],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip() != expected_internal:
        fail(f"pre-existing network has unsafe isolation: {name}")
    return result.returncode


def guard_network(real: Path, arguments: list[str]) -> int | None:
    if arguments == ["network", "inspect", MAIN_NETWORK]:
        return inspect_network(real, MAIN_NETWORK, "true")
    if arguments == ["network", "inspect", PROXY_NETWORK]:
        return inspect_network(real, PROXY_NETWORK, "false")
    if arguments == ["network", "create", "--internal", MAIN_NETWORK]:
        return None
    if arguments == ["network", "create", PROXY_NETWORK]:
        return None
    if arguments == ["network", "connect", MAIN_NETWORK, PROXY_NETWORK]:
        return None
    fail("unexpected container network operation")


def main() -> int:
    real, _workspace, _state, _image, _label, _allowed, _live_mode = private_config()
    arguments = sys.argv[1:]
    if not arguments:
        fail("missing runtime subcommand")
    if arguments[0] == "network":
        result = guard_network(real, arguments)
        if result is not None:
            return result
    if arguments[0] == "run":
        arguments = guarded_run(arguments)
    os.execve(real, [str(real), *arguments], os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
