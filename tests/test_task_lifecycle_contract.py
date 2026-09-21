"""Representative rule-scenario tests for the task-lifecycle contract.

Each test pins one of 9 representative directive rules to observable skill
text, using phrases unique to the new rule, so a future edit that drops the
rule fails here instead of silently weakening the contract. Generic examples
only; no private paths, quotas, or incidents.
Mirrors the style of tests/test_ephemeral_workspace_lifecycle.py upstream.
"""

from __future__ import annotations

from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1] / "skills"


def read_skill(name: str) -> str:
    return (CANDIDATE / name / "SKILL.md").read_text()


def normalized(text: str) -> str:
    return " ".join(text.split())


def test_handoff_does_not_complete_parent_delivery() -> None:
    text = normalized(read_skill("process-aware-done"))
    assert "parent delivery and cleanup obligations stay tracked" in text
    assert "Closing a queue item, opening a PR, or parking a wait" in text
    assert "never completes the task" in text
    assert "handed-off stage with no tracked tail is incomplete" in text


def test_done_names_tail_and_cleanup_disposition() -> None:
    text = normalized(read_skill("process-aware-done"))
    assert "name the tail that owns the remainder" in text
    assert "has no verified disposition" in text
    assert "report `removed`, `retained`, or `blocked` with evidence" in text


def test_yield_parks_stage_without_closing_parent() -> None:
    text = normalized(read_skill("yield-on-wait"))
    assert "parks the current stage; it never completes the parent task" in text
    assert "Name the tail link or ID in the current item before switching away" in text
    assert "parking a wait and calling the parent delivery done" in text
    assert "parent delivery stays open until its own artifact and cleanup receipts exist" in text


def test_one_change_owns_one_queue_item() -> None:
    text = normalized(read_skill("using-laneq"))
    assert "One logical code change owns one queued directive" in text
    assert "not new top-level directives" in text
    assert "--parent 15" in text
    assert "the parent stays open while the review child is unfinished" in text


def test_partial_completion_queues_tail_before_closing() -> None:
    text = normalized(read_skill("using-laneq"))
    assert "queue the scoped tail first" in text
    assert "never mark the logical parent done before" in text
    assert "never laneq done 15 here" in text
    block = text.split("Partial completion")[1].split("Cleanup is a queued tail")[0]
    assert block.index("--parent 15") < block.index("thread-status 15")
    assert "laneq done 15" not in block.replace("never laneq done 15 here", "")


def test_bare_done_has_no_native_hook_rejection() -> None:
    text = normalized(read_skill("using-laneq"))
    assert "accepted natively" in text
    assert "only where an optional environment-specific guard" in text
    assert "rejected by the installed hook" not in text


def test_cleanup_is_queued_tail_with_receipt() -> None:
    text = normalized(read_skill("using-laneq"))
    assert "same task queues one idempotent cleanup item" in text
    assert "records a `removed`, `retained`, or `blocked` receipt with evidence" in text
    assert "never closes the cleanup obligation by itself" in text


def test_read_only_work_records_na_instead_of_cleanup() -> None:
    text = normalized(read_skill("using-laneq"))
    assert "worktree/branch/PR: N/A" in text


def test_stale_lease_is_not_terminal_proof() -> None:
    text = normalized(read_skill("using-laneq"))
    assert "never proves a task is terminal" in text
    assert "reconcile a stale lease with the owning task record" in text
