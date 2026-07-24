from __future__ import annotations

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


def test_each_resource_contract_fails_closed() -> None:
    text = read_skill("ephemeral-workspace-lifecycle")
    gates = normalized(section(text, "Common Safety Gates"))
    contracts = normalized(section(text, "Resource-Specific Removal"))

    assert "Refuse symlinked roots" in gates
    assert "missing/malformed manifests, and probe errors" in gates
    assert "Never replace a failed native cleanup" in gates
    assert "unrelated merged PR number is not proof" in contracts
    assert "Retain the clone if fetch, reachability, submodule" in contracts
    assert "inventory contains no unknown or shared resource" in contracts
    assert "task completion may release a reference but must not delete the cache tree" in contracts


def test_completion_and_dispatch_skills_carry_workspace_disposition() -> None:
    done = read_skill("process-aware-done")
    issues = read_skill("continuous-issue-resolution")
    executor = read_skill("low-level-executor-task-spec")

    assert "ephemeral-workspace-lifecycle" in done
    assert "named owner, reason, and expiry" in done
    assert "workspace disposition" in issues
    assert "The workspace lifecycle" in executor
