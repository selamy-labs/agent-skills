# Real native installation and Git lifecycle contracts.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)


def command(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "tools.git_hooks", *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    assert git(root, "init", "-b", "main").returncode == 0
    assert git(root, "config", "user.name", "Example Contributor").returncode == 0
    assert git(root, "config", "user.email", "contributor@example.invalid").returncode == 0
    (root / ".gitignore").write_text("*.trace\n", encoding="utf-8")
    (root / "payload.txt").write_text("good\n", encoding="utf-8")
    (root / "check.py").write_text(
        "from pathlib import Path\n"
        "Path('canonical.trace').write_text('ran')\n"
        "payload = Path('payload.txt').read_text()\n"
        "print('canonical check: ' + payload.strip())\n"
        "raise SystemExit(0 if payload == 'good\\n' else 1)\n",
        encoding="utf-8",
    )
    (root / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: example-check\n"
        "        name: canonical fixture check\n"
        "        entry: python3 check.py\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        always_run: true\n",
        encoding="utf-8",
    )
    assert git(root, "add", ".").returncode == 0
    assert git(root, "commit", "-m", "test: valid initial fixture").returncode == 0
    return root


def test_normal_bootstrap_installs_hooks_that_judge_an_ordinary_commit(repo: Path) -> None:
    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert "0.5.5" in installed.stdout
    for name in ("pre-commit", "pre-push"):
        hook = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/" + name).stdout.strip())
        assert hook.is_file() and os.access(hook, os.X_OK)

    (repo / "payload.txt").write_text("bad\n", encoding="utf-8")
    assert git(repo, "add", "payload.txt").returncode == 0
    rejected = git(repo, "commit", "-m", "test: invalid fixture")
    assert rejected.returncode != 0
    assert "canonical check: bad" in rejected.stdout + rejected.stderr

    (repo / "payload.txt").write_text("good\n", encoding="utf-8")
    assert git(repo, "add", "payload.txt").returncode == 0
    corrected = git(repo, "commit", "--allow-empty", "-m", "test: corrected fixture")
    assert corrected.returncode == 0, corrected.stdout + corrected.stderr


@pytest.mark.parametrize("damage", ["missing", "replaced"])
def test_hook_refuses_a_missing_or_replaced_persistent_engine_without_path_fallback(repo: Path, damage: str) -> None:
    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    pin = common / "agent-skills-hooks" / "prek"
    trap = repo / "trap-bin"
    trap.mkdir()
    fake = trap / "prek"
    fake.write_text("#!/bin/sh\nprintf fallback > fallback.trace\nexit 0\n", encoding="utf-8")
    fake.chmod(0o700)
    if damage == "missing":
        pin.rename(pin.with_suffix(".retained"))
    else:
        pin.write_text("#!/bin/sh\nprintf replacement > replacement.trace\nexit 0\n", encoding="utf-8")
    (repo / "payload.txt").write_text("bad\n", encoding="utf-8")
    assert git(repo, "add", "payload.txt").returncode == 0
    env = os.environ.copy()
    env["PATH"] = str(trap) + os.pathsep + env["PATH"]
    rejected = subprocess.run(
        ["git", "commit", "-m", "test: damaged engine must reject"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert "pinned prek" in rejected.stdout + rejected.stderr
    assert not (repo / "fallback.trace").exists()
    assert not (repo / "replacement.trace").exists()


def test_install_preflights_both_stages_before_changing_any_hook_or_persistent_pin(repo: Path) -> None:
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    hooks = common / "hooks"
    foreign = b"#!/bin/sh\nexit 0\n"
    (hooks / "pre-commit").write_bytes(foreign)
    (hooks / "pre-commit").chmod(0o700)
    (hooks / "pre-push").mkdir()
    rejected = command(repo, "install")
    assert rejected.returncode != 0
    assert (hooks / "pre-commit").read_bytes() == foreign
    assert not (hooks / "pre-commit.legacy").exists()
    assert (hooks / "pre-push").is_dir()
    assert not (common / "agent-skills-hooks" / "prek").exists()


def install_with_legacy(repo: Path) -> Path:
    hook = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push").stdout.strip())
    hook.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "from pathlib import Path\n"
        "Path('legacy.trace').write_text(json.dumps({'args':sys.argv[1:], 'stdin':sys.stdin.read()}))\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return hook


def push_packet(repo: Path, packet: bytes, *args: str) -> subprocess.CompletedProcess[bytes]:
    hook = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push").stdout.strip())
    return subprocess.run([str(hook), *args], cwd=repo, input=packet, capture_output=True, check=False, timeout=30)


@pytest.mark.parametrize("kind", ["mixed", "deletions", "empty"])
def test_push_preserves_original_remote_arguments_and_complete_legacy_input(repo: Path, kind: str) -> None:
    import json

    install_with_legacy(repo)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    deletion = f"(delete) {'0' * len(head)} refs/tags/retired {head}\n"
    source = f"refs/heads/main {head} refs/heads/published {'0' * len(head)}\n"
    packet = {"mixed": source + deletion, "deletions": deletion, "empty": ""}[kind]
    remote = ("origin", "a remote URL with spaces")
    result = push_packet(repo, packet.encode(), *remote)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((repo / "legacy.trace").read_text()) == {"args": list(remote), "stdin": packet}


@pytest.mark.parametrize(
    "kind",
    ["two-sources", "non-head", "tag-source", "tabs", "double-space", "crlf", "nul", "oversize", "bad-remote"],
)
def test_push_rejects_unsupported_packets_before_legacy_or_canonical_checks(repo: Path, kind: str) -> None:
    older = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(repo, "commit", "--allow-empty", "-m", "test: current source tip").returncode == 0
    install_with_legacy(repo)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    source = f"refs/heads/main {head} refs/heads/published {'0' * len(head)}\n"
    packets = {
        "two-sources": source + source,
        "non-head": source.replace(head, older),
        "tag-source": source.replace("refs/heads/main", "refs/tags/source"),
        "tabs": source.replace(" ", "\t"),
        "double-space": source.replace(" ", "  ", 1),
        "crlf": source.replace("\n", "\r\n"),
        "nul": source + "\0",
        "oversize": "a" * (1024 * 1024 + 1),
        "bad-remote": source.replace("refs/heads/published", "refs/heads/a..b"),
    }
    result = push_packet(repo, packets[kind].encode(), "origin", "example.invalid")
    assert result.returncode != 0
    assert b"Hook configuration error:" in result.stderr
    assert not (repo / "legacy.trace").exists()
    assert not (repo / "canonical.trace").exists()


def test_push_rejects_missing_original_remote_arguments(repo: Path) -> None:
    install_with_legacy(repo)
    result = push_packet(repo, b"")
    assert result.returncode != 0
    assert b"original remote arguments" in result.stderr
    assert not (repo / "legacy.trace").exists()


def test_reinstall_preserves_existing_legacy_hooks_and_is_idempotent(repo: Path) -> None:
    hook = install_with_legacy(repo)
    hooks = hook.parent
    before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in hooks.iterdir() if p.is_file()}
    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert {p.name: (p.read_bytes(), p.stat().st_mode) for p in hooks.iterdir() if p.is_file()} == before


def test_install_refuses_a_foreign_hook_with_an_existing_legacy_backup(repo: Path) -> None:
    hook = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-commit").stdout.strip())
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    legacy = hook.with_suffix(".legacy")
    legacy.write_text("#!/bin/sh\nprintf keep > keep.trace\n", encoding="utf-8")
    before = (hook.read_bytes(), legacy.read_bytes())
    rejected = command(repo, "install")
    assert rejected.returncode != 0
    assert (hook.read_bytes(), legacy.read_bytes()) == before


def test_install_refuses_symlinked_persistent_state_without_writing_through_it(repo: Path, tmp_path: Path) -> None:
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    target = tmp_path / "unrelated state"
    target.mkdir()
    (common / "agent-skills-hooks").symlink_to(target, target_is_directory=True)
    rejected = command(repo, "install")
    assert rejected.returncode != 0
    assert not list(target.iterdir())


def test_bootstrap_honors_configured_hook_directory(repo: Path) -> None:
    hooks = repo.parent / "configured hooks"
    assert git(repo, "config", "core.hooksPath", str(hooks)).returncode == 0
    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert git(repo, "config", "--get", "core.hooksPath").stdout.strip() == str(hooks)
    assert all((hooks / stage).is_file() for stage in ("pre-commit", "pre-push"))
    (repo / "payload.txt").write_text("bad\n", encoding="utf-8")
    assert git(repo, "add", "payload.txt").returncode == 0
    rejected = git(repo, "commit", "-m", "test: custom hook directory")
    assert rejected.returncode != 0
    assert "canonical check: bad" in rejected.stdout + rejected.stderr


def test_fresh_linked_worktree_inherits_the_installed_lifecycle(repo: Path, tmp_path: Path) -> None:
    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    linked = tmp_path / "linked author"
    added = git(repo, "worktree", "add", "-b", "linked", str(linked))
    assert added.returncode == 0, added.stdout + added.stderr
    (linked / "payload.txt").write_text("bad\n", encoding="utf-8")
    assert git(linked, "add", "payload.txt").returncode == 0
    rejected = git(linked, "commit", "-m", "test: inherited lifecycle")
    assert rejected.returncode != 0
    assert "canonical check: bad" in rejected.stdout + rejected.stderr


def test_source_push_rejects_dirty_content_before_any_stage_verdict(repo: Path) -> None:
    install_with_legacy(repo)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    packet = f"refs/heads/main {head} refs/heads/published {'0' * len(head)}\n".encode()
    (repo / "payload.txt").write_text("bad\n", encoding="utf-8")
    rejected = push_packet(repo, packet, "origin", "example.invalid")
    assert rejected.returncode != 0
    assert b"Hook configuration error:" in rejected.stderr
    assert not (repo / "legacy.trace").exists()
    assert not (repo / "canonical.trace").exists()


def test_install_does_not_overwrite_state_changed_while_native_hooks_are_staged(repo: Path, monkeypatch) -> None:
    from tools import git_hooks

    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    state = common / "agent-skills-hooks"
    sentinel = b"another installer owns this state\n"
    original = subprocess.run

    def staged_child(args, **kwargs):
        result = original(args, **kwargs)
        if args[0].endswith("/prek") and "install" in args:
            state.mkdir()
            (state / "guard.py").write_bytes(sentinel)
        return result

    monkeypatch.chdir(repo)
    monkeypatch.setattr(git_hooks.subprocess, "run", staged_child)
    with pytest.raises(ValueError, match="changed during installation"):
        git_hooks.install()
    assert (state / "guard.py").read_bytes() == sentinel
    assert not (state / "prek").exists()
    assert not (common / "hooks/pre-commit").exists()


@pytest.mark.parametrize("kind", ["missing", "drifted"])
def test_dependency_contract_requires_the_maintained_prek_pin(repo: Path, kind: str) -> None:
    from tools.validate_skills import validate_requirements_dev_content

    content = (ROOT / "requirements-dev.txt").read_text()
    changed = content.replace("prek==0.5.5\n", "" if kind == "missing" else "prek==0.5.6\n")
    errors = validate_requirements_dev_content(changed)
    assert any("prek==0.5.5" in error for error in errors)


@pytest.mark.parametrize(
    "kind",
    ["blank-row", "upper-oid", "short-oid", "zero-delete", "wrong-delete-name", "unresolved-ref", "stale-ref"],
)
def test_push_policy_rejects_invalid_object_and_ref_contracts(repo: Path, monkeypatch, kind: str) -> None:
    from tools import git_hooks

    old = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(repo, "branch", "old").returncode == 0
    assert git(repo, "commit", "--allow-empty", "-m", "test: new checked-out tip").returncode == 0
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    zero = "0" * len(head)
    source = f"HEAD {head} refs/heads/published {zero}\n"
    packets = {
        "blank-row": source + "\n",
        "upper-oid": source.replace(head, head.upper()),
        "short-oid": source.replace(head, head[:-1]),
        "zero-delete": f"(delete) {zero} refs/heads/retired {zero}\n",
        "wrong-delete-name": f"refs/heads/main {zero} refs/heads/retired {old}\n",
        "unresolved-ref": source.replace("HEAD", "refs/heads/missing", 1),
        "stale-ref": source.replace("HEAD", "refs/heads/old", 1),
    }
    monkeypatch.chdir(repo)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        git_hooks.validate_push(["origin", "example.invalid"], packets[kind].encode())


@pytest.mark.parametrize("kind", ["head", "delete-only", "empty"])
def test_push_policy_accepts_head_or_deletion_batches_without_changing_the_source(
    repo: Path, monkeypatch, kind: str
) -> None:
    from tools import git_hooks

    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    zero = "0" * len(head)
    packets = {
        "head": f"HEAD {head} refs/heads/published {zero}\n(delete) {zero} refs/tags/retired {head}\n",
        "delete-only": f"(delete) {zero} refs/heads/retired {head}\n",
        "empty": "",
    }
    monkeypatch.chdir(repo)
    git_hooks.validate_push(["origin", "example.invalid"], packets[kind].encode())
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert git(repo, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("damage", ["missing-digest", "invalid-digest", "symlink", "unexecutable"])
def test_runtime_rejects_invalid_persistent_pin_state(repo: Path, monkeypatch, damage: str) -> None:
    from tools import git_hooks

    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    state = common / "agent-skills-hooks"
    pin = state / "prek"
    if damage == "missing-digest":
        (state / "prek.sha256").rename(state / "retained.sha256")
    elif damage == "invalid-digest":
        (state / "prek.sha256").write_text("not a digest", encoding="utf-8")
    elif damage == "symlink":
        pin.rename(state / "retained-prek")
        pin.symlink_to(state / "retained-prek")
    else:
        pin.chmod(0o600)
    monkeypatch.setattr(git_hooks, "__file__", str(state / "guard.py"))
    with pytest.raises(ValueError, match="pinned prek"):
        git_hooks.hook("pre-commit", common / "hooks", [])


def test_persistent_runtime_runs_without_the_bootstrap_python_environment(repo: Path) -> None:
    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    env = os.environ.copy()
    env["PATH"] = os.defpath
    for name in ["VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"]:
        env.pop(name, None)
    (repo / "payload.txt").write_text("bad\n", encoding="utf-8")
    assert git(repo, "add", "payload.txt").returncode == 0
    rejected = subprocess.run(
        ["git", "commit", "-m", "test: persistent runtime"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "canonical check: bad" in rejected.stdout + rejected.stderr
    (repo / "payload.txt").write_text("good\n", encoding="utf-8")
    assert git(repo, "add", "payload.txt").returncode == 0
    corrected = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "test: persistent runtime correction"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert corrected.returncode == 0, corrected.stdout + corrected.stderr


def test_install_refuses_an_active_installation_lock_before_hook_or_pin_publication(repo: Path) -> None:
    import fcntl

    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    with (common / "agent-skills-hooks.lock").open("a+") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rejected = command(repo, "install")
        assert rejected.returncode != 0
        assert not (common / "agent-skills-hooks/prek").exists()
        assert not (common / "hooks/pre-commit").exists()
    recovered = command(repo, "install")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr


def test_install_refuses_a_symlinked_installation_lock_without_touching_its_target(repo: Path, tmp_path: Path) -> None:
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    foreign = tmp_path / "unrelated lock"
    foreign.write_bytes(b"preserve owner state\n")
    (common / "agent-skills-hooks.lock").symlink_to(foreign)
    rejected = command(repo, "install")
    assert rejected.returncode != 0
    assert foreign.read_bytes() == b"preserve owner state\n"
    assert not (common / "agent-skills-hooks/prek").exists()
    assert not (common / "hooks/pre-commit").exists()


@pytest.mark.parametrize("damage", ["version", "missing-binary", "record-hash", "unexecutable"])
def test_native_package_admission_rejects_invalid_installations(tmp_path: Path, monkeypatch, damage: str) -> None:
    import base64
    import hashlib
    from types import SimpleNamespace

    from tools import git_hooks

    binary = tmp_path / "prek"
    binary.write_bytes(b"package executable fixture")
    binary.chmod(0o700)
    digest = base64.urlsafe_b64encode(hashlib.sha256(binary.read_bytes()).digest()).decode().rstrip("=")
    entry = SimpleNamespace(name="prek", hash=SimpleNamespace(mode="sha256", value=digest))
    package = SimpleNamespace(version="0.5.5", files=[entry], locate_file=lambda _: binary)
    if damage == "version":
        package.version = "0.5.6"
    elif damage == "missing-binary":
        package.files = []
    elif damage == "record-hash":
        entry.hash.value = "not the recorded package digest"
    else:
        binary.chmod(0o600)
    monkeypatch.setattr(git_hooks, "distribution", lambda _: package)
    with pytest.raises(ValueError):
        git_hooks.native_executable()


@pytest.mark.parametrize("suffix", [" ", "\t", "\n", "\r"])
def test_significant_hook_path_whitespace_keeps_exact_install_and_native_lifecycle(repo: Path, suffix: str) -> None:
    sibling = repo.parent / "configured hooks"
    sibling.mkdir()
    sentinel = sibling / "unrelated"
    sentinel.write_bytes(b"preserve sibling\n")
    hooks = repo.parent / ("configured hooks" + suffix)
    assert git(repo, "config", "core.hooksPath", str(hooks)).returncode == 0
    remote = repo.parent / "remote.git"
    assert git(repo, "init", "--bare", str(remote)).returncode == 0
    assert git(repo, "remote", "add", "origin", str(remote)).returncode == 0
    (repo / "payload.txt").write_text("bad\n", encoding="utf-8")
    assert git(repo, "add", "payload.txt").returncode == 0
    assert git(repo, "commit", "-m", "test: invalid baseline before installation").returncode == 0

    installed = command(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert (hooks / "pre-commit").is_file()
    assert (hooks / "pre-push").is_file()
    assert list(sibling.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve sibling\n"

    head = git(repo, "rev-parse", "HEAD").stdout
    rejected = git(repo, "commit", "--allow-empty", "-m", "test: invalid after installation")
    assert rejected.returncode != 0
    assert "canonical check: bad" in rejected.stdout + rejected.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == head
    pushed = git(repo, "push", "origin", "HEAD:refs/heads/verified")
    assert pushed.returncode != 0
    assert "canonical check: bad" in pushed.stdout + pushed.stderr
    assert git(repo, "ls-remote", "origin", "refs/heads/verified").stdout == ""

    (repo / "payload.txt").write_text("good\n", encoding="utf-8")
    assert git(repo, "add", "payload.txt").returncode == 0
    corrected = git(repo, "commit", "-m", "test: corrected after installation")
    assert corrected.returncode == 0, corrected.stdout + corrected.stderr
    accepted = git(repo, "push", "origin", "HEAD:refs/heads/verified")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert git(repo, "ls-remote", "origin", "refs/heads/verified").stdout.startswith(
        git(repo, "rev-parse", "HEAD").stdout.removesuffix("\n") + "\t"
    )
    assert list(sibling.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve sibling\n"


@pytest.mark.parametrize("suffix", [" ", "\t", "\n", "\r"])
def test_significant_checkout_whitespace_runs_in_the_actual_root(repo: Path, suffix: str) -> None:
    root = repo.with_name(repo.name + suffix)
    repo.rename(root)
    repo.mkdir()
    sentinel = repo / "unrelated"
    sentinel.write_bytes(b"preserve root sibling\n")
    installed = command(root, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    (root / "payload.txt").write_text("bad\n", encoding="utf-8")
    assert git(root, "add", "payload.txt").returncode == 0
    head = git(root, "rev-parse", "HEAD").stdout
    rejected = git(root, "commit", "-m", "test: exact whitespace checkout")
    assert rejected.returncode != 0
    assert "canonical check: bad" in rejected.stdout + rejected.stderr
    assert git(root, "rev-parse", "HEAD").stdout == head
    (root / "payload.txt").write_text("good\n", encoding="utf-8")
    assert git(root, "add", "payload.txt").returncode == 0
    corrected = git(root, "commit", "--allow-empty", "-m", "test: exact root recovery")
    assert corrected.returncode == 0, corrected.stdout + corrected.stderr
    assert list(repo.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve root sibling\n"
