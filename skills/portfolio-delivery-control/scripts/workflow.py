#!/usr/bin/env python3
"""Deterministic portfolio delivery control workflow-management module.

Provides guarded, atomic, zero-daemon workflow operations:
- admit: Admit and deduplicate real work from issue/spec.
- claim: Atomically claim admitted work with owner, session, worktree scope, and token.
- record-implementation: Bind Flash implementation commit/tree and mandatory gates.
- record-dispatch: Record coordinator review dispatch in the dispatch ledger.
- record-review: Bind closed review receipt with exact revision and ledger binding.
- execute-transition: Perform guarded side-effect transitions with provider reconciliation.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any

from admission import (
    DEFAULT_ALLOWED_ROOTS,
    inspect_modified_paths,
    paths_overlap,
    validate_spec_task,
    validate_worktree,
    verify_mandatory_gates,
)
from admission import (
    admit_work as _admit_work_impl,
)
from admission import (
    claim_work as _claim_work_impl,
)
from admission import (
    record_implementation as _record_impl_impl,
)
from providers import (
    ALLOWED_MERGE_IDENTITIES,
    APPROVED_BOT_IDENTITIES,
    evaluate_pr_ci,
    is_approved_identity,
    is_hex_40,
    is_hex_64,
    iso_timestamp,
    reconcile_and_complete_spec_task,
    reconcile_and_merge_pr,
    reconcile_and_publish_pr,
    reconcile_and_push,
    sha256_file,
    verify_provider_bot_actor,
)
from providers import (
    check_existing_pr as _check_existing_pr_impl,
)
from providers import (
    get_remote_branch_sha as _get_remote_branch_sha_impl,
)
from providers import (
    inspect_git_commit as _inspect_git_commit_impl,
)
from providers import (
    is_pr_already_merged as _is_pr_already_merged_impl,
)
from receipts import (
    CANONICAL_FAMILY_MODELS,
    SCHEMA_VERSION,
    VALID_FAMILIES,
    VALID_VERDICTS,
    check_qualification,
    validate_review_receipt,
    verify_dispatch_binding,
    verify_reviewer_uniqueness,
)
from receipts import (
    record_dispatch as _record_dispatch_impl,
)
from receipts import (
    record_review as _record_review_impl,
)
from transitions import (
    VALID_ACTIONS,
)
from transitions import (
    execute_transition as _execute_transition_impl,
)

UTC = datetime.UTC

_check_existing_pr = _check_existing_pr_impl
_get_remote_branch_sha = _get_remote_branch_sha_impl
_inspect_git_commit = _inspect_git_commit_impl
_is_hex_40 = is_hex_40
_is_hex_64 = is_hex_64
_is_pr_already_merged = _is_pr_already_merged_impl
_verify_mandatory_gates = verify_mandatory_gates
_verify_provider_bot_actor = verify_provider_bot_actor

__all__ = [
    "ALLOWED_MERGE_IDENTITIES",
    "APPROVED_BOT_IDENTITIES",
    "CANONICAL_FAMILY_MODELS",
    "DEFAULT_ALLOWED_ROOTS",
    "SCHEMA_VERSION",
    "VALID_ACTIONS",
    "VALID_FAMILIES",
    "VALID_VERDICTS",
    "_check_existing_pr",
    "_get_remote_branch_sha",
    "_inspect_git_commit",
    "_is_hex_40",
    "_is_hex_64",
    "_is_pr_already_merged",
    "_verify_mandatory_gates",
    "_verify_provider_bot_actor",
    "admit_work",
    "build_parser",
    "check_qualification",
    "claim_work",
    "evaluate_pr_ci",
    "execute_transition",
    "inspect_modified_paths",
    "is_approved_identity",
    "is_hex_40",
    "is_hex_64",
    "iso_timestamp",
    "load_coordinator_status",
    "main",
    "make_refusal",
    "paths_overlap",
    "reconcile_and_complete_spec_task",
    "reconcile_and_merge_pr",
    "reconcile_and_publish_pr",
    "reconcile_and_push",
    "record_dispatch",
    "record_implementation",
    "record_review",
    "save_coordinator_status",
    "sha256_file",
    "validate_review_receipt",
    "validate_spec_task",
    "validate_worktree",
    "verify_dispatch_binding",
    "verify_mandatory_gates",
    "verify_provider_bot_actor",
    "verify_reviewer_uniqueness",
    "workflow_lock",
    "write_receipt",
]


@contextlib.contextmanager
def workflow_lock(control_dir: Path | str):
    cdir = Path(control_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    lock_file = cdir / ".workflow.lock"
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def load_coordinator_status(control_dir: Path | str) -> dict[str, Any]:
    path = Path(control_dir) / "coordinator-status.json"
    now_iso = iso_timestamp()
    if not path.exists():
        return {
            "state": "active",
            "model": "coordinator-model",
            "wake_owner": "native-schedule",
            "observed_at": now_iso,
            "nextAction": "Advance admitted projects",
            "next_action": "Advance admitted projects",
            "delivery_control": {
                "schema_version": 1,
                "observed_at": now_iso,
                "directives": [],
                "pending_inputs": [],
                "projects": {},
                "work_items": {},
                "dispatch_ledger": {},
            },
        }
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if "delivery_control" not in doc or not isinstance(doc["delivery_control"], dict):
        doc["delivery_control"] = {
            "schema_version": 1,
            "observed_at": now_iso,
            "directives": [],
            "pending_inputs": [],
            "projects": {},
            "work_items": {},
            "dispatch_ledger": {},
        }
    doc.setdefault("state", "active")
    doc.setdefault("model", "coordinator-model")
    doc.setdefault("wake_owner", "native-schedule")
    doc.setdefault("observed_at", now_iso)
    doc.setdefault("nextAction", "Advance admitted projects")
    doc.setdefault("next_action", "Advance admitted projects")
    dc = doc["delivery_control"]
    dc.setdefault("projects", {})
    dc.setdefault("work_items", {})
    dc.setdefault("directives", [])
    dc.setdefault("pending_inputs", [])
    dc.setdefault("dispatch_ledger", {})
    return doc


def save_coordinator_status(control_dir: Path | str, doc: dict[str, Any]) -> None:
    cdir = Path(control_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / "coordinator-status.json"
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    tmp_path.replace(path)


def write_receipt(control_dir: Path | str, filename: str, data: dict[str, Any]) -> Path:
    rec_dir = Path(control_dir) / "receipts"
    rec_dir.mkdir(parents=True, exist_ok=True)
    rec_path = rec_dir / filename
    rec_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return rec_path


def make_refusal(
    control_dir: Path | str,
    work_id: str,
    reason_code: str,
    message: str,
    extra: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    rec_data: dict[str, Any] = {
        "status": "refused",
        "work_id": work_id,
        "reason_code": reason_code,
        "message": message,
        "observed_at": iso_now,
    }
    if extra:
        rec_data.update(extra)
    cdir = Path(control_dir)
    if cdir.exists():
        rec_dir = cdir / "receipts"
        rec_dir.mkdir(parents=True, exist_ok=True)
        rec_path = rec_dir / f"refusal_{work_id}_{reason_code}.json"
        rec_path.write_text(json.dumps(rec_data, indent=2), encoding="utf-8")
        rec_data["receipt_path"] = str(rec_path)
    return rec_data


def admit_work(
    control_dir: Path | str,
    repo: str,
    issue_num: int | str,
    spec_path: Path | str,
    task_id: str,
    base_branch: str = "main",
    target_branch: str | None = None,
    allowed_roots: list[str] | None = None,
    priority: int = 1,
    now: str | None = None,
) -> dict[str, Any]:
    with workflow_lock(control_dir):
        return _admit_work_impl(
            control_dir,
            repo,
            issue_num,
            spec_path,
            task_id,
            base_branch=base_branch,
            target_branch=target_branch,
            allowed_roots=allowed_roots,
            priority=priority,
            now=now,
        )


def claim_work(
    control_dir: Path | str,
    work_id: str,
    owner: str,
    session_id: str,
    worktree: Path | str,
    ttl_seconds: int = 1800,
    now: str | None = None,
) -> dict[str, Any]:
    with workflow_lock(control_dir):
        return _claim_work_impl(
            control_dir,
            work_id,
            owner,
            session_id,
            worktree,
            ttl_seconds=ttl_seconds,
            now=now,
        )


def record_implementation(
    control_dir: Path | str,
    work_id: str,
    claim_token: str,
    commit_sha: str,
    tree_sha: str,
    gate_results: dict[str, Any] | None = None,
    modified_paths: list[str] | None = None,
    patch_path: Path | str | None = None,
    patch_sha256: str | None = None,
    bundle_path: Path | str | None = None,
    bundle_sha256: str | None = None,
    author_receipt_path: Path | str | None = None,
    author_receipt_sha256: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    with workflow_lock(control_dir):
        return _record_impl_impl(
            control_dir,
            work_id,
            claim_token,
            commit_sha,
            tree_sha,
            gate_results=gate_results,
            modified_paths=modified_paths,
            patch_path=patch_path,
            patch_sha256=patch_sha256,
            bundle_path=bundle_path,
            bundle_sha256=bundle_sha256,
            author_receipt_path=author_receipt_path,
            author_receipt_sha256=author_receipt_sha256,
            now=now,
        )


def record_dispatch(
    control_dir: Path | str,
    work_id: str,
    dispatch_id: str,
    family: str,
    reviewer_session_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    with workflow_lock(control_dir):
        return _record_dispatch_impl(
            control_dir,
            work_id,
            dispatch_id,
            family,
            reviewer_session_id,
            now=now,
        )


def record_review(
    control_dir: Path | str,
    work_id: str,
    reviewer_id: str,
    model: str,
    family: str,
    session_id: str,
    verdict: str,
    report_path: Path | str,
    report_sha256: str,
    now: str | None = None,
) -> dict[str, Any]:
    with workflow_lock(control_dir):
        return _record_review_impl(
            control_dir,
            work_id,
            reviewer_id,
            model,
            family,
            session_id,
            verdict,
            report_path,
            report_sha256,
            now=now,
        )


def execute_transition(
    control_dir: Path | str,
    work_id: str,
    action: str,
    claim_token: str | None = None,
    remote: str = "origin",
    branch: str | None = None,
    pr_number: int | str | None = None,
    pr_title: str | None = None,
    pr_body: str | None = None,
    merge_identity: str = "selamy-labs-bot",
    command_runner: Any = None,
    now: str | None = None,
) -> dict[str, Any]:
    with workflow_lock(control_dir):
        return _execute_transition_impl(
            control_dir,
            work_id,
            action,
            claim_token=claim_token,
            remote=remote,
            branch=branch,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_body=pr_body,
            merge_identity=merge_identity,
            command_runner=command_runner,
            now=now,
        )


def _add_admit_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("admit", help="Admit new work item")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", required=True)
    p.add_argument("--spec-path", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--base-branch", default="main")
    p.add_argument("--target-branch")
    p.add_argument("--allowed-roots", nargs="+")
    p.add_argument("--priority", type=int, default=1)


def _add_claim_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("claim", help="Claim admitted work item")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--session-id", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--ttl-seconds", type=int, default=1800)


def _add_impl_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("record-implementation", help="Record implementation candidate")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--claim-token", required=True)
    p.add_argument("--commit-sha", required=True)
    p.add_argument("--tree-sha", required=True)
    p.add_argument("--gate-results")
    p.add_argument("--modified-paths", nargs="+")
    p.add_argument("--patch-path")
    p.add_argument("--patch-sha256")
    p.add_argument("--bundle-path")
    p.add_argument("--bundle-sha256")
    p.add_argument("--author-receipt-path")
    p.add_argument("--author-receipt-sha256")


def _add_dispatch_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("record-dispatch", help="Record coordinator review dispatch")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--family", required=True, choices=list(VALID_FAMILIES))
    p.add_argument("--reviewer-session-id", required=True)


def _add_review_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("record-review", help="Record reviewer verdict")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--reviewer-id", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--family", required=True, choices=list(VALID_FAMILIES))
    p.add_argument("--session-id", required=True)
    p.add_argument("--verdict", required=True, choices=list(VALID_VERDICTS))
    p.add_argument("--report-path", required=True)
    p.add_argument("--report-sha256", required=True)


def _add_trans_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("execute-transition", help="Execute guarded transition")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--action", required=True, choices=list(VALID_ACTIONS))
    p.add_argument("--claim-token")
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch")
    p.add_argument("--pr-number")
    p.add_argument("--pr-title")
    p.add_argument("--pr-body")
    p.add_argument("--merge-identity", default="selamy-labs-bot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded portfolio delivery control workflow manager")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_admit_parser(subparsers)
    _add_claim_parser(subparsers)
    _add_impl_parser(subparsers)
    _add_dispatch_parser(subparsers)
    _add_review_parser(subparsers)
    _add_trans_parser(subparsers)
    return parser


def _dispatch_cli(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.command
    if cmd == "admit":
        return admit_work(
            args.control_dir,
            args.repo,
            args.issue,
            args.spec_path,
            args.task_id,
            base_branch=args.base_branch,
            target_branch=args.target_branch,
            allowed_roots=args.allowed_roots,
            priority=args.priority,
        )
    if cmd == "claim":
        return claim_work(
            args.control_dir,
            args.work_id,
            args.owner,
            args.session_id,
            args.worktree,
            ttl_seconds=args.ttl_seconds,
        )
    if cmd == "record-implementation":
        gates = json.loads(args.gate_results) if args.gate_results else None
        return record_implementation(
            args.control_dir,
            args.work_id,
            args.claim_token,
            args.commit_sha,
            args.tree_sha,
            gate_results=gates,
            modified_paths=args.modified_paths,
            patch_path=args.patch_path,
            patch_sha256=args.patch_sha256,
            bundle_path=args.bundle_path,
            bundle_sha256=args.bundle_sha256,
            author_receipt_path=args.author_receipt_path,
            author_receipt_sha256=args.author_receipt_sha256,
        )
    if cmd == "record-dispatch":
        return record_dispatch(
            args.control_dir,
            args.work_id,
            args.dispatch_id,
            args.family,
            args.reviewer_session_id,
        )
    if cmd == "record-review":
        return record_review(
            args.control_dir,
            args.work_id,
            args.reviewer_id,
            args.model,
            args.family,
            args.session_id,
            args.verdict,
            args.report_path,
            args.report_sha256,
        )
    if cmd == "execute-transition":
        return execute_transition(
            args.control_dir,
            args.work_id,
            args.action,
            claim_token=args.claim_token,
            remote=args.remote,
            branch=args.branch,
            pr_number=args.pr_number,
            pr_title=args.pr_title,
            pr_body=args.pr_body,
            merge_identity=args.merge_identity,
        )
    return {"status": "error", "error": f"Unknown command {cmd}"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    res = _dispatch_cli(args)
    sys.stdout.write(json.dumps(res, indent=2) + "\n")
    if res.get("status") in {"refused", "error"}:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
