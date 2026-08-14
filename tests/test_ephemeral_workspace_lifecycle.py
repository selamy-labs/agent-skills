from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_skill(name: str) -> str:
    return (ROOT / "skills" / name / "SKILL.md").read_text()


def section(text: str, heading: str) -> str:
    marker = f"## {heading}\n"
    start = text.index(marker) + len(marker)
    end = text.find("\n## ", start)
    return text[start:] if end == -1 else text[start:end]


def normalized(text: str) -> str:
    return " ".join(text.split())


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    executable = shutil.which("git")
    assert executable is not None
    result = subprocess.run(
        [executable, *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def resource_matrix(text: str) -> dict[str, tuple[str, str, str]]:
    rows: dict[str, tuple[str, str, str]] = {}
    for line in section(text, "Classify Before Creating").splitlines():
        if not line.startswith("| ") or line.startswith("| ---"):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if cells[0] == "Resource":
            continue
        assert len(cells) == 4
        rows[cells[0]] = cells[1], cells[2], cells[3]
    return rows


def test_resource_matrix_separates_exclusive_and_shared_cleanup() -> None:
    rows = resource_matrix(read_skill("ephemeral-workspace-lifecycle"))

    assert set(rows) == {
        "Linked Git worktree",
        "Full temporary clone",
        "Exclusive task directory",
        "Per-task build output or cache",
        "Shared or global cache",
    }
    assert "git worktree remove" in rows["Linked Git worktree"][2]
    assert "git worktree prune" not in rows["Linked Git worktree"][2]
    assert "freshly verified remote" in rows["Full temporary clone"][1]
    assert "no-symlink contained removal" in rows["Exclusive task directory"][2]
    assert "no process or active task references it" in rows["Per-task build output or cache"][1]
    assert rows["Shared or global cache"][1].startswith("Never as part of task teardown")
    assert "Cache-native" in rows["Shared or global cache"][2]


def test_registry_terminal_transition_does_not_block_its_own_removal() -> None:
    text = read_skill("ephemeral-workspace-lifecycle")
    registry = normalized(section(text, "Registry State"))

    assert "active -> terminal-removable -> removed" in registry
    assert "every task reference to be in a terminal state" in registry
    assert "replace the live registry entry with an immutable tombstone" in registry
    assert "tombstone is audit history, not a live reference" in registry
    assert "missing heartbeat or old modification time is not a terminal transition" in registry


def test_controller_owns_every_terminal_cleanup_edge() -> None:
    text = read_skill("ephemeral-workspace-lifecycle")
    creation = normalized(section(text, "At Creation"))
    ownership = normalized(section(text, "Finalization Ownership"))

    assert "temporary-directory APIs and `/tmp` as placement mechanisms" in creation
    assert "controller that allocates a resource owns its final disposition" in ownership
    assert "Register an exit observer before launching the worker" in ownership
    assert "success, merge or close, cancellation, iteration exhaustion" in ownership
    assert "ordinary client exit, and handled failure" in ownership
    assert "Do not make the worker process the sole owner of cleanup" in ownership
    assert "OOM, `SIGKILL`, host loss, and client crashes can bypass it" in ownership
    assert "reconcile any resource still marked `active`" in ownership


def test_each_resource_contract_fails_closed() -> None:
    text = read_skill("ephemeral-workspace-lifecycle")
    gates = normalized(section(text, "Common Safety Gates"))
    contracts = normalized(section(text, "Resource-Specific Removal"))

    assert "Refuse symlinked roots" in gates
    assert "missing/malformed manifests, and probe errors" in gates
    assert "Never replace a failed native cleanup" in gates
    assert "unrelated merged PR number is not proof" in contracts
    assert "squash or rebase merge does not make the original task HEAD reachable" in contracts
    assert "Git bundle stored outside every ephemeral root" in contracts
    assert "enumerates that exact commit and can restore it" in contracts
    assert "git worktree prune" in contracts
    assert "may only run as a separate repository maintenance operation" in contracts
    assert "Retain the clone if fetch, reachability, submodule" in contracts
    assert "inventory contains no unknown or shared resource" in contracts
    assert "task completion may release a reference but must not delete the cache tree" in contracts


def test_worktree_cleanup_detects_ignored_and_hidden_index_state() -> None:
    contracts = normalized(section(read_skill("ephemeral-workspace-lifecycle"), "Resource-Specific Removal"))

    assert "Run every Git safety probe as the resource-owning UID" in contracts
    assert "never as a privileged controller" in contracts
    assert "GIT_CONFIG_NOSYSTEM=1" in contracts
    assert "GIT_CONFIG_GLOBAL=/dev/null" in contracts
    assert "-c core.fsmonitor=false" in contracts
    assert "-c core.hooksPath=/dev/null" in contracts
    assert "git status --porcelain=v1 --untracked-files=all --ignored=traditional" in contracts
    assert "Treat ignored files as work" in contracts
    assert "any output retains the worktree" in contracts
    assert "git ls-files -v" in contracts
    assert "lowercase status tags (assume-unchanged)" in contracts
    assert "an `S` tag (skip-worktree)" in contracts
    assert "do not silently clear those flags" in contracts
    assert "Re-run both probes immediately before removal" in contracts


def test_git_probes_enumerate_ignored_descendants_and_hidden_flags(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Lifecycle Test")
    git(repo, "config", "user.email", "lifecycle@example.invalid")
    (repo / ".gitignore").write_text("cache/\n")
    (repo / "assume.txt").write_text("original\n")
    (repo / "skip.txt").write_text("original\n")
    git(repo, "add", ".gitignore", "assume.txt", "skip.txt")
    git(repo, "commit", "-m", "fixture")
    git(repo, "update-index", "--assume-unchanged", "assume.txt")
    git(repo, "update-index", "--skip-worktree", "skip.txt")
    (repo / "cache" / "nested").mkdir(parents=True)
    (repo / "cache" / "first.bin").write_text("one\n")
    (repo / "cache" / "nested" / "second.bin").write_text("two\n")

    status = git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=traditional",
    )
    index = git(repo, "ls-files", "-v")

    assert "!! cache/first.bin" in status
    assert "!! cache/nested/second.bin" in status
    assert "h assume.txt" in index
    assert "S skip.txt" in index


def test_sanitized_git_probe_disables_repository_fsmonitor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(f"#!/bin/sh\n: > '{marker}'\nprintf 'token\\n'\n")
    hook.chmod(0o700)
    git(repo, "config", "core.fsmonitor", str(hook))

    git(repo, "status", "--porcelain=v1")
    assert marker.exists(), "fixture must prove repository config can execute"
    marker.unlink()

    executable = shutil.which("git")
    assert executable is not None
    owner_home = tmp_path / "home"
    owner_home.mkdir()
    safe_env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_EXEC_PATH": git(repo, "--exec-path").strip(),
        "GIT_PAGER": "cat",
        "HOME": str(owner_home),
        "LC_ALL": "C",
        "PATH": str(Path(executable).parent),
    }
    git(
        repo,
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=traditional",
        env=safe_env,
    )

    assert not marker.exists()


def test_completion_and_dispatch_skills_carry_workspace_disposition() -> None:
    done = read_skill("process-aware-done")
    issues = read_skill("continuous-issue-resolution")
    executor = read_skill("low-level-executor-task-spec")

    assert "ephemeral-workspace-lifecycle" in done
    assert "named owner, reason, and expiry" in done
    assert "workspace disposition" in issues
    assert "The workspace lifecycle" in executor
