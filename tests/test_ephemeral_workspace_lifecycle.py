from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_skill(name: str) -> str:
    return (ROOT / "skills" / name / "SKILL.md").read_text()


def test_lifecycle_skill_requires_ownership_liveness_and_native_cleanup() -> None:
    text = read_skill("ephemeral-workspace-lifecycle")

    assert "named owner" in text
    assert "no live process, open file" in text
    assert "Never automatically delete dirty" in text
    assert "git worktree remove" in text
    assert "Directory modification time" in text
    assert "alone is not proof" in text


def test_completion_and_dispatch_skills_carry_workspace_disposition() -> None:
    done = read_skill("process-aware-done")
    issues = read_skill("continuous-issue-resolution")
    executor = read_skill("low-level-executor-task-spec")

    assert "ephemeral-workspace-lifecycle" in done
    assert "named owner, reason, and expiry" in done
    assert "workspace disposition" in issues
    assert "The workspace lifecycle" in executor
