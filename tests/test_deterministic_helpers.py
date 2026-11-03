"""Boundary tests for packaged helpers and metadata enforcement."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.naming import validate_metadata

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "skills/gate-before-push/scripts/repository_preflight.py"
MONITOR = ROOT / "skills/github-pr-shepherding/scripts/review_monitor.py"
spec = importlib.util.spec_from_file_location("preflight", PREFLIGHT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    return tmp_path


def test_discovery_tracks_untracked_files_and_missing_configuration(tmp_path):
    root = repo(tmp_path)
    (root / "new.py").write_text("x = 1")
    report = preflight.discover(root, "HEAD")
    assert report["dirty"]
    assert "new.py" in report["status_porcelain"]
    assert report["head"] == report["base"]
    assert report["checks"] == []
    assert report["codegraph"] == "not_indexed"


def test_explicit_check_execution_captures_output_without_shell_expansion(tmp_path):
    root = repo(tmp_path)
    manifest = {
        "schema_version": 1,
        "checks": [
            {"name": "unit", "scope": "worktree", "argv": [sys.executable, "-c", 'print("$(touch unwanted)")']},
            {"name": "ci", "scope": "hosted"},
        ],
    }
    (root / ".repository-preflight.json").write_text(json.dumps(manifest))
    report = preflight.discover(root)
    log = tmp_path.parent / (tmp_path.name + ".log")
    result = preflight.run_check(report, "unit", log)
    assert result["status"] == "passed"
    assert "$(touch unwanted)" in log.read_text()
    assert not (root / "unwanted").exists()
    assert preflight.run_check(report, "ci", log)["status"] == "pending_hosted"


@pytest.mark.parametrize(
    "checks",
    [
        [{"name": "x", "scope": "worktree", "argv": "echo unsafe"}],
        [{"name": "x", "scope": "implicit", "argv": ["true"]}],
        [{"name": "x", "scope": "hosted"}, {"name": "x", "scope": "hosted"}],
    ],
)
def test_bad_manifest_rejected(tmp_path, checks):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "checks": checks}))
    with pytest.raises(ValueError):
        preflight.load_checks(path)


def test_ledger_cli_refuses_wrong_repository_or_actor_and_preserves_history(tmp_path):
    ledger = tmp_path / "ledger.json"
    common = [sys.executable, str(MONITOR), "--ledger", str(ledger), "--repo-id", "example/repo", "--actor", "reviewer"]
    subprocess.run([*common, "init"], check=True)
    original = ledger.read_text()
    assert subprocess.run([*common, "init"], capture_output=True).returncode != 0
    wrong = [*common, "claim", "--number", "1", "--head", "a", "--owner", "worker", "--repo-id", "other/repo"]
    assert subprocess.run(wrong, capture_output=True).returncode != 0
    assert ledger.read_text() == original
    wrong[-1] = "example/repo"
    wrong.extend(["--actor", "other"])
    assert subprocess.run(wrong, capture_output=True).returncode != 0
    assert ledger.read_text() == original


def test_metadata_helper_containment_and_compatible_audiences(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.touch()
    (tmp_path / "run.py").touch()
    assert not validate_metadata({"metadata": {"audience": "agent", "helpers": "run.py"}}, skill)
    assert not validate_metadata({"metadata": {"audience": "control-plane-only"}}, skill)
    assert not validate_metadata({}, skill)
    assert validate_metadata({"metadata": {"audience": "typo"}}, skill)
    assert validate_metadata({"metadata": {"helpers": "../outside.py"}}, skill)
    assert validate_metadata({"metadata": {"helpers": "missing.py"}}, skill)
    outside = tmp_path.parent / "outside.py"
    outside.touch()
    (tmp_path / "linked.py").symlink_to(outside)
    assert validate_metadata({"metadata": {"helpers": "linked.py"}}, skill)


def test_packet_uses_real_commits_and_writes_diff(tmp_path):
    from tests.test_review_monitor import monitor

    (tmp_path / "checkout").mkdir()
    root = repo(tmp_path / "checkout")
    base = preflight.git(root, "rev-parse", "HEAD")
    (root / "change.txt").write_text("changed")
    subprocess.run(["git", "-C", str(root), "add", "change.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "change",
        ],
        check=True,
    )
    head = preflight.git(root, "rev-parse", "HEAD")
    row = {"number": 1, "decision": "first_review", "head_sha": head, "base_sha": base}
    output = tmp_path / "packet"
    result = monitor.make_packet(row, {"pr": {}}, root, output, {"artifacts": [str(tmp_path / "absent")]})
    assert result["files"] == 1
    assert "+changed" in (output / "current.diff").read_text()
    assert json.loads((output / "packet.json").read_text())["evidence_receipts"][0]["missing"]


def test_packet_rejects_option_shaped_commit_before_git(tmp_path):
    from tests.test_review_monitor import monitor

    row = {"number": 1, "decision": "first_review", "head_sha": "--output=unexpected", "base_sha": "main"}
    with pytest.raises(ValueError, match="full commit"):
        monitor.make_packet(row, {"pr": {}}, tmp_path, tmp_path / "packet", {})
    assert not (tmp_path / "unexpected").exists()
