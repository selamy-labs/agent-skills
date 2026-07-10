#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
PR_GROUP = "group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
PR_CANCEL = "cancel-in-progress: ${{ github.event_name == 'pull_request' }}"


def workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])


def triggers_pull_request(text: str) -> bool:
    return "\n  pull_request:" in f"\n{text}" or "\npull_request:" in f"\n{text}"


def test_pull_request_workflows_cancel_obsolete_runs() -> None:
    checked: list[str] = []
    for path in workflow_files():
        text = path.read_text()
        if not triggers_pull_request(text):
            continue

        checked.append(path.name)
        assert "concurrency:" in text, f"{path.name} must declare concurrency"
        assert PR_GROUP in text, f"{path.name} must group PR runs by workflow and PR number"
        assert PR_CANCEL in text, f"{path.name} must cancel only pull_request runs"

    assert checked == ["diagram-freshness.yml", "quality.yaml", "secret-scan.yml"]


def test_release_workflow_keeps_non_cancelling_serialisation() -> None:
    text = (WORKFLOWS_DIR / "release.yaml").read_text()

    assert "pull_request:" not in text
    assert "group: agent-skills-release" in text
    assert "cancel-in-progress: false" in text


def main() -> int:
    test_pull_request_workflows_cancel_obsolete_runs()
    test_release_workflow_keeps_non_cancelling_serialisation()
    print("PR workflow cancellation tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
