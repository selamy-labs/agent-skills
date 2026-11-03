#!/usr/bin/env python3
"""Deterministic portfolio delivery control workflow-management module.

Provides guarded, atomic, zero-daemon workflow operations:
- admit: Admit and deduplicate real work from issue/spec.
- claim: Atomically claim admitted work with owner, session, worktree scope, and token.
- record-implementation: Bind Flash implementation commit/tree and mandatory gates.
- record-review: Bind independent reviewer verdict with exact revision and report attestation.
- execute-transition: Perform guarded side-effect transitions with provider reconciliation.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

UTC = datetime.UTC
VALID_FAMILIES = {"argon", "opus", "sol"}
VALID_VERDICTS = {"SHIP", "REVISE"}
FORBIDDEN_MODELS = {"claude-fable-5-1"}
CANONICAL_STAGES = {
    "discovery",
    "ready",
    "implementing",
    "review",
    "ci",
    "merge_ready",
    "waiting",
    "blocked",
    "done",
}
VALID_ACTIONS = {"push", "publish_pr", "merge_pr", "complete_spec_task"}
PROTECTED_SKILL_REL = "skills/portfolio-delivery-control"
DEFAULT_ALLOWED_ROOTS = ["/tmp", "/home/pselamy", "/workspace"]
APPROVED_BOT_IDENTITIES = {
    "selamy-labs-bot <346154065+selamy-labs-bot@users.noreply.github.com>",
    "claude-cir <claude-cir@selamy.dev>",
}
APPROVED_BOT_EMAILS = {
    "346154065+selamy-labs-bot@users.noreply.github.com",
    "claude-cir@selamy.dev",
}
APPROVED_BOT_PAIRS = {
    ("selamy-labs-bot", "346154065+selamy-labs-bot@users.noreply.github.com"),
    ("claude-cir", "claude-cir@selamy.dev"),
}
ALLOWED_MERGE_IDENTITIES = {"selamy-labs-bot", "claude-cir"}

COMMIT_SHA_KEYS = [
    "commit_sha",
    "commitSha",
    "reviewedCommit",
    "reviewed_head_sha",
    "candidate_commit",
    "commit.sha",
    "target.commit_sha",
    "artifact_verification.head.actual",
    "artifact_verification.head.expected",
    "identityProof.bundle.head",
]
TREE_SHA_KEYS = [
    "tree_sha",
    "treeSha",
    "reviewedTree",
    "reviewed_tree_sha",
    "candidate_tree",
    "commit.tree_sha",
    "target.tree_sha",
    "artifact_verification.tree.actual",
    "artifact_verification.tree.expected",
    "identityProof.patch.appliedToParentGivesTree",
]


def _is_approved_identity(ident: str) -> bool:
    clean = ident.strip()
    if clean in APPROVED_BOT_IDENTITIES:
        return True
    if clean in ALLOWED_MERGE_IDENTITIES:
        return True
    if "<" in clean and clean.endswith(">"):
        name = clean[: clean.index("<")].strip()
        email = clean[clean.index("<") + 1 : -1].strip()
        if (name, email) in APPROVED_BOT_PAIRS:
            return True
        if not name and email in APPROVED_BOT_EMAILS:
            return True
    return False


def _lookup_key(data: dict[str, Any], key: str) -> Any:
    if key in data:
        return data[key]
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _extract_sha_from_report(data: dict[str, Any], keys: list[str]) -> str | None:
    for k in keys:
        val = _lookup_key(data, k)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_embedded_verdict(rep_data: dict[str, Any]) -> str | None:
    for key in ("verdict", "overall_verdict", "review_verdict", "decision", "summary.verdict"):
        val = _lookup_key(rep_data, key)
        if isinstance(val, str) and val.strip().upper() in VALID_VERDICTS:
            return val.strip().upper()
    status_val = rep_data.get("status")
    if isinstance(status_val, str) and status_val.strip().upper() in VALID_VERDICTS:
        return status_val.strip().upper()
    return None


def _extract_reviewer_strings(rev: Any) -> list[str]:
    if isinstance(rev, str) and rev.strip():
        return [rev.strip().lower()]
    if isinstance(rev, dict):
        return [str(v).strip().lower() for k in ("model", "name") if (v := rev.get(k)) and str(v).strip()]
    return []


CANONICAL_REVIEWER_MODELS: dict[str, set[str]] = {
    "opus": {
        "claude-opus-5-5",
        "claude-opus-5.5",
        "claude-opus-5",
        "claude-opus-4-6",
        "claude-opus-4.6",
        "claude-opus-4-5",
        "claude-opus-4.5",
        "claude-opus-4",
        "claude-3-opus",
        "claude-3-opus-20240229",
    },
    "argon": {
        "gemini-4.0-argon-high",
        "gemini-4.0-argon",
        "gemini-4-argon-high",
        "gemini-4-argon",
        "gemini-4.5-argon-high",
        "gemini-4.5-argon",
        "gemini-4-argon-thought",
    },
    "sol": {
        "gpt-6.1-sol-high",
        "gpt-6.1-sol",
        "gpt-6-sol-high",
        "gpt-6-sol",
        "gpt-5.4",
        "gpt-5.4-sol",
        "gpt-5.3-codex",
        "gpt-5-sol",
    },
}


def _normalize_model_name(model: str) -> str:
    clean = model.strip().lower()
    return re.sub(r"[\s_]+", "-", clean)


def _classify_family(model: str) -> str | None:
    if not model or not isinstance(model, str):
        return None
    clean = model.strip().lower()
    norm = _normalize_model_name(clean)
    for fam, allowed in CANONICAL_REVIEWER_MODELS.items():
        if clean in allowed or norm in allowed:
            return fam
    return None


REPORT_MODEL_FIELDS = (
    "model",
    "reviewer_model",
    "authorModel",
    "author_model",
    "reviewer.model",
    "metadata.model",
    "review_metadata.model",
)
_SESSION_TOKENS = ("session", "conversation")
_SESSION_KEYS = _SESSION_TOKENS
REPORT_SESSION_FIELDS = (
    "session_id",
    "sessionId",
    "reviewer_session_id",
    "reviewerSessionId",
    "conversation_id",
    "conversationId",
    "reviewer_conversation_id",
    "reviewerConversationId",
    "session",
    "reviewer.session_id",
    "reviewer.sessionId",
    "metadata.session_id",
    "metadata.conversation_id",
)


def _extract_report_models(rep_data: dict[str, Any]) -> list[str]:
    models: list[str] = []
    for k in REPORT_MODEL_FIELDS:
        v = _lookup_key(rep_data, k)
        if isinstance(v, str) and v.strip():
            models.append(v.strip())
    return models


def _is_session_key(k: Any) -> bool:
    k_lower = str(k).lower()
    return any(tok in k_lower for tok in _SESSION_TOKENS)


def _collect_session_list_item(item: Any, sessions: list[str]) -> None:
    if isinstance(item, str):
        sessions.append(item.strip() if item.strip() else "")
    elif isinstance(item, dict):
        _collect_sessions_from_dict(item, sessions)
    elif isinstance(item, list):
        _collect_session_list(item, sessions)
    else:
        sessions.append("")


def _collect_session_list(val: list[Any], sessions: list[str]) -> None:
    if not val:
        sessions.append("")
        return
    for item in val:
        _collect_session_list_item(item, sessions)


def _collect_session_key_value(val: Any, sessions: list[str]) -> None:
    if isinstance(val, str):
        sessions.append(val.strip() if val.strip() else "")
    elif isinstance(val, list):
        _collect_session_list(val, sessions)
    elif isinstance(val, dict):
        _collect_sessions_from_dict(val, sessions)
    else:
        sessions.append("")


def _collect_sessions_from_dict(d: dict[str, Any], sessions: list[str]) -> None:
    for k, v in d.items():
        if _is_session_key(k):
            _collect_session_key_value(v, sessions)
        elif isinstance(v, dict):
            _collect_sessions_from_dict(v, sessions)
        elif isinstance(v, list):
            _collect_sessions_from_list(v, sessions)


def _collect_sessions_from_list(lst: list[Any], sessions: list[str]) -> None:
    for item in lst:
        if isinstance(item, dict):
            _collect_sessions_from_dict(item, sessions)
        elif isinstance(item, list):
            _collect_sessions_from_list(item, sessions)


def _extract_report_sessions(report: Any) -> list[str]:
    sessions: list[str] = []
    if isinstance(report, dict):
        _collect_sessions_from_dict(report, sessions)
    elif isinstance(report, list):
        _collect_sessions_from_list(report, sessions)
    return sessions


_extract_all_report_sessions = _extract_report_sessions
_extract_nested_reviewer_sessions = _extract_report_sessions


def _extract_receipt_session(receipt_data: Any) -> list[str]:
    if isinstance(receipt_data, dict):
        return _extract_report_sessions(receipt_data)
    return []


def _verify_report_session(
    report: dict[str, Any] | None,
    receipt_data: dict[str, Any] | None,
    session_id: str,
) -> bool:
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    expected = session_id.strip()
    found = _extract_report_sessions(report)
    found.extend(_extract_receipt_session(receipt_data))
    if not found:
        return False
    return all(s == expected for s in found)


def _is_non_empty_val(val: Any) -> bool:
    if isinstance(val, str | bytes):
        return bool(str(val).strip())
    if isinstance(val, dict | list):
        return len(val) > 0
    return False


def _has_report_provenance(rep_data: dict[str, Any]) -> bool:
    keys = (
        "dispatch",
        "dispatch_id",
        "session_id",
        "session",
        "attestation",
        "sha256",
        "contentSha256",
        "report_sha256",
    )
    return any(_is_non_empty_val(rep_data.get(k)) for k in keys)


def _extract_attestation_signer(rep_data: dict[str, Any]) -> str:
    att = rep_data.get("attestation")
    if isinstance(att, dict):
        sb = att.get("signedBy")
        if isinstance(sb, str) and sb.strip():
            return sb.strip().lower()
    return ""


def _collect_embedded_model_strings(rep_data: dict[str, Any]) -> list[str]:
    out = [m.lower() for m in _extract_report_models(rep_data)]
    out.extend(_extract_reviewer_strings(rep_data.get("reviewer")))
    if not out:
        signer = _extract_attestation_signer(rep_data)
        if signer:
            out.append(signer)
    return out


def _check_rep_model_family(rep_data: dict[str, Any], fam: str) -> bool:
    for m in _extract_report_models(rep_data):
        if _classify_family(m) != fam:
            return False
    return True


def _verify_model_family(family: str, model: str, rep_data: dict[str, Any]) -> bool:
    fam = family.strip().lower()
    if fam not in CANONICAL_REVIEWER_MODELS or _classify_family(model) != fam:
        return False
    if not _has_report_provenance(rep_data):
        return False
    present_models = _extract_report_models(rep_data)
    if not present_models:
        return False
    clean_model = _normalize_model_name(model).lower()
    for m in present_models:
        if _normalize_model_name(m).lower() != clean_model:
            return False
        if _classify_family(m) != fam:
            return False
    return True


def _is_owner_or_session_match(wi: dict[str, Any], reviewer_id: str, session_id: str) -> bool:
    claim = wi.get("claim") or {}
    owner = claim.get("owner") or wi.get("owner", "")
    sess = claim.get("session_id", "")
    if reviewer_id and reviewer_id == owner:
        return True
    if session_id and (session_id == sess or session_id == owner):
        return True
    return False


def _is_model_forbidden_self(model: str, cand_model: str | None) -> bool:
    if "flash" in model.lower():
        return True
    return bool(cand_model and model == cand_model)


def _flatten_token_values(val: Any) -> list[str]:
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    if isinstance(val, dict):
        return [v.strip() for v in val.values() if isinstance(v, str) and v.strip()]
    return []


def _extract_embedded_identity_tokens(rep_data: dict[str, Any]) -> list[str]:
    keys = (
        "session",
        "session_id",
        "conversationId",
        "conversation_id",
        "authorModel",
        "author_model",
        "reviewer",
        "model",
        "dispatch",
        "dispatch_id",
        "attestation",
    )
    tokens: list[str] = []
    for k in keys:
        tokens.extend(_flatten_token_values(rep_data.get(k)))
    return tokens


def _is_token_self_match(tok: str, impl_owner: str, impl_session: str, impl_model: str) -> bool:
    tok_low = tok.lower()
    if "flash" in tok_low:
        return True
    if impl_session and tok == impl_session:
        return True
    if impl_owner and tok == impl_owner:
        return True
    if impl_model and tok_low == impl_model.lower():
        return True
    return False


def _check_rep_data_self(rep_data: dict[str, Any], wi: dict[str, Any], cand_model: str | None) -> bool:
    claim = wi.get("claim") or {}
    impl_owner = (claim.get("owner") or wi.get("owner", "")).strip()
    impl_session = claim.get("session_id", "").strip()
    impl_model = (cand_model or "").strip()
    tokens = _extract_embedded_identity_tokens(rep_data)
    return any(_is_token_self_match(t, impl_owner, impl_session, impl_model) for t in tokens)


def _check_self_review(
    wi: dict[str, Any],
    reviewer_id: str,
    session_id: str,
    model: str,
    rep_data: dict[str, Any] | None = None,
) -> bool:
    if _is_owner_or_session_match(wi, reviewer_id, session_id):
        return True
    cand = wi.get("candidate") or {}
    cand_model = cand.get("implementer_model") or (wi.get("claim") or {}).get("model")
    if _is_model_forbidden_self(model, cand_model):
        return True
    if rep_data and _check_rep_data_self(rep_data, wi, cand_model):
        return True
    return False


def _check_embedded_session(rep_data: dict[str, Any], rev_dict: dict[str, Any], session_id: str) -> bool:
    return _verify_report_session(rep_data, {"session_id": session_id}, session_id)


def _check_embedded_reviewer_id(rep_data: dict[str, Any], rev_dict: dict[str, Any], reviewer_id: str) -> bool:
    emb_rev = rep_data.get("reviewer_id") or rev_dict.get("id") or rev_dict.get("reviewer_id")
    if isinstance(emb_rev, str) and emb_rev.strip() and emb_rev.strip() != reviewer_id.strip():
        return False
    return True


def _check_embedded_account(rep_data: dict[str, Any], rev_dict: dict[str, Any], session_id: str) -> bool:
    emb_acc = rep_data.get("account") or rev_dict.get("account")
    if isinstance(emb_acc, str) and emb_acc.strip() and ":" in session_id:
        acc = emb_acc.strip().lower()
        if session_id.split(":")[0].strip().lower() != acc:
            return False
    return True


def _check_embedded_dispatch(rep_data: dict[str, Any], reviewer_id: str) -> bool:
    emb_disp = rep_data.get("dispatch") or rep_data.get("dispatch_id")
    if isinstance(emb_disp, str) and emb_disp.strip() and "dispatch" in reviewer_id.lower():
        if reviewer_id.strip() != emb_disp.strip():
            return False
    return True


def _check_embedded_consistency(rep_data: dict[str, Any], reviewer_id: str, session_id: str) -> tuple[bool, str]:
    rev_dict = rep_data.get("reviewer") if isinstance(rep_data.get("reviewer"), dict) else {}
    if not _check_embedded_session(rep_data, rev_dict, session_id):
        return False, "REVIEWER_SESSION_MISMATCH"
    if not _check_embedded_reviewer_id(rep_data, rev_dict, reviewer_id):
        return False, "REPORT_IDENTITY_MISMATCH"
    if not _check_embedded_account(rep_data, rev_dict, session_id):
        return False, "REPORT_IDENTITY_MISMATCH"
    if not _check_embedded_dispatch(rep_data, reviewer_id):
        return False, "REPORT_IDENTITY_MISMATCH"
    return True, ""


def _is_same_path(p1: Path | str, p2: Path | str) -> bool:
    try:
        return Path(p1).resolve() == Path(p2).resolve()
    except Exception:
        return str(p1) == str(p2)


def _is_review_report_match(r: dict[str, Any], report_path: Path | str, report_sha256: str) -> bool:
    if report_sha256 and r.get("report_sha256") == report_sha256:
        return True
    old_path = r.get("report_path")
    return bool(old_path and _is_same_path(old_path, report_path))


def _check_report_duplicate(reviews: list[dict[str, Any]], report_path: Path | str, report_sha256: str) -> bool:
    return any(_is_review_report_match(r, report_path, report_sha256) for r in reviews)


def _is_path_allowed(file_path: str, allowed_prefixes: list[str]) -> bool:
    clean_file = file_path.strip().replace("\\", "/")
    for allowed in allowed_prefixes:
        clean_prefix = allowed.strip().replace("\\", "/").rstrip("/")
        if not clean_prefix:
            return True
        if clean_file == clean_prefix or clean_file.startswith(clean_prefix + "/"):
            return True
    return False


def _is_protected_skill_file(f_norm: str) -> bool:
    return (
        f_norm == PROTECTED_SKILL_REL
        or f_norm.startswith(PROTECTED_SKILL_REL + "/")
        or f_norm.startswith("skills/portfolio-delivery-control")
    )


def _check_single_modified_path(
    f_norm: str, repo: str, protect_skill: bool, allowed_rel_paths: list[str] | None
) -> tuple[bool, str]:
    if _is_protected_skill_file(f_norm):
        if repo != "selamy-labs/agent-skills" or protect_skill:
            return False, "PROTECTED_PATH_MODIFIED"
    if allowed_rel_paths and not _is_path_allowed(f_norm, allowed_rel_paths):
        return False, "SCOPE_ESCAPE"
    return True, ""


def _inspect_modified_paths(
    worktree: Path,
    base_sha: str,
    commit_sha: str,
    repo: str,
    protect_skill: bool,
    allowed_rel_paths: list[str] | None,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    if not base_sha or not commit_sha:
        return True, ""
    code, diff_out = _run_cmd(
        ["git", "diff", "--no-renames", "--name-only", f"{base_sha}..{commit_sha}"],
        worktree,
        runner,
    )
    if code != 0:
        return False, "BASE_SHA_DRIFT"
    files = [f.strip().replace("\\", "/") for f in diff_out.splitlines() if f.strip()]
    for f_norm in files:
        ok, err = _check_single_modified_path(f_norm, repo, protect_skill, allowed_rel_paths)
        if not ok:
            return False, err
    return True, ""


def iso_timestamp(dt: datetime.datetime | None = None) -> str:
    current = dt or datetime.datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.isoformat()


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@contextlib.contextmanager
def workflow_lock(control_dir: Path | str):
    cdir = Path(control_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    lock_file = cdir / ".workflow.lock"
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
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
    return doc


def save_coordinator_status(control_dir: Path | str, doc: dict[str, Any]) -> None:
    cdir = Path(control_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    now_iso = iso_timestamp()
    doc["observed_at"] = now_iso
    if "delivery_control" in doc and isinstance(doc["delivery_control"], dict):
        doc["delivery_control"]["observed_at"] = now_iso
    path = cdir / "coordinator-status.json"
    tmp_path = cdir / f".coordinator-status.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def write_receipt(control_dir: Path | str, filename: str, data: dict[str, Any]) -> Path:
    rdir = Path(control_dir) / "receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    receipt_path = rdir / filename
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    return receipt_path


def make_refusal(
    control_dir: Path | str,
    work_id: str,
    reason_code: str,
    details: str,
    extra: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    observed_at = now or iso_timestamp()
    receipt = {
        "status": "refused",
        "reason_code": reason_code,
        "work_id": work_id,
        "observed_at": observed_at,
        "details": details,
    }
    if extra:
        receipt.update(extra)
    receipt_file = f"refusal_{work_id}_{reason_code}.json"
    receipt_path = write_receipt(control_dir, receipt_file, receipt)
    receipt["receipt_path"] = str(receipt_path)
    if reason_code == "CI_CHECK_FAILED" and extra and extra.get("action"):
        action = extra["action"]
        write_receipt(control_dir, f"refusal_{work_id}_{action}_{reason_code}.json", receipt)
    return receipt


def paths_overlap(left: str, right: str) -> bool:
    if any(c in left + right for c in "*?["):
        left_p, right_p = (re.split(r"[?*\[]", p, maxsplit=1)[0] for p in (left, right))
        return left_p.startswith(right_p) or right_p.startswith(left_p)
    return left == right or left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")


def _resolve_approval_path(path_str: str, control_dir: Path | str | None) -> Path | None:
    p = Path(path_str).expanduser()
    if p.is_file():
        return p.resolve()
    if control_dir:
        cd_p = (Path(control_dir) / path_str).expanduser()
        if cd_p.is_file():
            return cd_p.resolve()
    return None


def _extract_approval_dict(d: dict[str, Any], default_sha: str | None) -> tuple[str | None, str | None]:
    p = d.get("path") or d.get("authority")
    s = d.get("sha256") or d.get("sha") or default_sha
    path_val = str(p) if p else None
    sha_val = str(s) if s else None
    return path_val, sha_val


def _is_hex_40(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    clean = val.strip()
    return len(clean) == 40 and all(c in "0123456789abcdefABCDEF" for c in clean)


_is_valid_sha = _is_hex_40


def _is_hex_64(val: str) -> bool:
    return len(val) == 64 and all(c in "0123456789abcdefABCDEF" for c in val)


def _extract_approval_str(s: str, default_sha: str | None) -> tuple[str | None, str | None]:
    clean = s.strip()
    if "#" in clean:
        p, sha = clean.rsplit("#", 1)
        return p.strip(), sha.strip()
    if ":" in clean:
        p, sha = clean.rsplit(":", 1)
        return p.strip(), sha.strip()
    if _is_hex_64(clean):
        return None, clean
    sha_fallback = default_sha.strip() if default_sha else None
    return clean, sha_fallback


def _extract_approval_info(
    approval_ref: str | dict[str, Any] | None,
    approval_sha256: str | None = None,
) -> tuple[str | None, str | None]:
    if not approval_ref:
        return None, approval_sha256
    if isinstance(approval_ref, dict):
        return _extract_approval_dict(approval_ref, approval_sha256)
    if isinstance(approval_ref, str):
        return _extract_approval_str(approval_ref, approval_sha256)
    return None, approval_sha256


def _check_approval_withdrawal(raw_bytes: bytes) -> bool:
    text_lower = raw_bytes.decode("utf-8", errors="replace").lower()
    patterns = (
        r'"status":\s*"withdrawn"',
        r'"status":\s*"revoked"',
        r'"approved":\s*false',
        r"\bwithdrawn\b",
        r"\brevoked\b",
        r"\bdo\s+not\s+publish\b",
        r"\bdo\s+not\s+merge\b",
    )
    return any(re.search(pat, text_lower) for pat in patterns)


def _resolve_approval_file(
    path_str: str, control_dir: Path | str | None
) -> tuple[Path | None, bytes | None, str | None]:
    resolved = _resolve_approval_path(path_str, control_dir)
    if not resolved:
        return None, None, None
    read_ok, raw = _read_live_approval_file(resolved)
    if not read_ok:
        return None, None, None
    live_sha = hashlib.sha256(raw).hexdigest()
    return resolved, raw, live_sha


def _check_sha_match(live_sha: str, expected_sha: str | None) -> bool:
    if not expected_sha or not _is_hex_64(expected_sha):
        return False
    return live_sha.lower() == expected_sha.lower()


def _read_live_approval_file(resolved: Path) -> tuple[bool, bytes]:
    try:
        return True, resolved.read_bytes()
    except OSError:
        return False, b""


def _verify_approval_digest(live_sha: str, expected_sha: str | None, is_admission: bool) -> tuple[bool, str]:
    if is_admission:
        if expected_sha and not _check_sha_match(live_sha, expected_sha):
            return False, "APPROVAL_DIGEST_MISMATCH"
        return True, ""
    if not expected_sha or not _check_sha_match(live_sha, expected_sha):
        return False, "STALE_APPROVAL"
    return True, ""


def _verify_live_approval(
    approval_ref: str | dict[str, Any] | None,
    control_dir: Path | str | None = None,
    approval_sha256: str | None = None,
    is_admission: bool = False,
) -> tuple[bool, str]:
    if not approval_ref:
        return False, "ABSENT_APPROVAL"
    path_str, expected_sha = _extract_approval_info(approval_ref, approval_sha256)
    if not path_str:
        return (False, "ABSENT_APPROVAL") if not expected_sha else (False, "MISSING_APPROVAL")
    resolved, raw, live_sha = _resolve_approval_file(path_str, control_dir)
    if not live_sha or raw is None:
        return False, "MISSING_APPROVAL"
    if _check_approval_withdrawal(raw):
        return False, "APPROVAL_WITHDRAWN"
    return _verify_approval_digest(live_sha, expected_sha, is_admission)


def _extract_authority_str(ref: Any) -> str:
    if isinstance(ref, dict):
        return str(ref.get("path") or ref.get("authority") or json.dumps(ref))
    if isinstance(ref, str):
        sep = "#" if "#" in ref else (":" if ":" in ref else None)
        return ref.split(sep)[0] if sep else ref
    return str(ref)


def _check_approval_authority(approval_ref: Any, control_dir: Path | str | None = None) -> tuple[bool, str]:
    return _verify_live_approval(approval_ref, control_dir)


def _project_approval(approval_ref: Any, control_dir: Path | str | None = None) -> dict[str, Any]:
    if not approval_ref:
        return {"status": "absent", "reference": "", "authority": "absent"}
    ok, reason = _check_approval_authority(approval_ref, control_dir)
    if not ok:
        if reason == "APPROVAL_WITHDRAWN":
            status = "withdrawn"
        elif reason == "MISSING_APPROVAL":
            status = "missing"
        elif reason == "ABSENT_APPROVAL":
            status = "absent"
        else:
            status = "stale"
        return {
            "status": status,
            "reference": approval_ref,
            "authority": _extract_authority_str(approval_ref),
        }
    return {
        "status": "approved",
        "reference": approval_ref,
        "authority": _extract_authority_str(approval_ref),
    }


def _project_reviews(reviews: list[dict[str, Any]], fallback_ev: str) -> list[dict[str, Any]]:
    proj = []
    for r in reviews:
        role = r.get("role") or r.get("family", "reviewer")
        reviewer = r.get("reviewer") or r.get("reviewer_id") or f"reviewer-{role}"
        ev = [str(r.get("report_path"))] if r.get("report_path") else [fallback_ev]
        proj.append(
            {
                "role": role,
                "reviewer": reviewer,
                "model": r.get("model", ""),
                "revision": r.get("commit_sha", ""),
                "verdict": r.get("verdict", "SHIP"),
                "evidence": ev,
            }
        )
    return proj


def _find_provider_ci_evidence(transitions: list[dict[str, Any]], commit_sha: str) -> tuple[bool, list[str]]:
    has_provider_ci = False
    ci_evidence: list[str] = []
    for t in transitions:
        if t.get("commit_sha") == commit_sha and t.get("action") == "merge_pr":
            has_provider_ci = True
            if t.get("evidence"):
                ci_evidence.append(t["evidence"])
    return has_provider_ci, ci_evidence


def _project_branch_protection(p_row: dict[str, Any], ci_evidence: list[str]) -> None:
    if p_row.get("branch_protection", {}).get("verified") is True:
        bp_ev = p_row["branch_protection"].setdefault("evidence", [])
        for ev_item in ci_evidence:
            if ev_item not in bp_ev:
                bp_ev.append(ev_item)
    elif "branch_protection" not in p_row:
        p_row["branch_protection"] = {"verified": False, "evidence": []}


def _project_work_item(
    doc: dict[str, Any],
    wi: dict[str, Any],
    iso_now: str,
    receipt_str: str | None = None,
    control_dir: Path | str | None = None,
) -> dict[str, Any]:
    project = wi.get("project", "default")
    p_row = doc["delivery_control"]["projects"].setdefault(project, {})
    p_row["stage"] = wi.get("stage", "ready")
    p_row["last_progress_at"] = iso_now
    p_row["observed_at"] = iso_now
    p_row["approval"] = _project_approval(wi.get("approval_ref"), control_dir)

    cand = wi.get("candidate") or {}
    commit_sha = cand.get("commit_sha") or wi.get("base_sha", "")
    p_row["revision"] = commit_sha

    # Unconditionally project reviews from work item
    p_row["reviews"] = _project_reviews(wi.get("reviews", []), receipt_str or "")

    if receipt_str:
        ev = p_row.setdefault("evidence", [])
        if receipt_str not in ev:
            ev.append(receipt_str)

    has_ci, ci_evidence = _find_provider_ci_evidence(wi.get("transitions", []), commit_sha)
    status_str = "success" if has_ci else "pending"
    p_row["ci"] = {"revision": commit_sha, "status": status_str, "evidence": ci_evidence}
    _project_branch_protection(p_row, ci_evidence)

    return p_row


# -----------------------------------------------------------------------------
# 1. Admit Work
# -----------------------------------------------------------------------------


def _validate_base_sha(base_sha: str | None) -> bool:
    return bool(base_sha and isinstance(base_sha, str) and re.fullmatch(r"[0-9a-fA-F]{40}", base_sha))


def _validate_spec_task(spec_path: Path | str | None, task_id: str) -> bool:
    if not spec_path:
        return False
    path = Path(spec_path)
    if not path.exists():
        return False
    try:
        content = path.read_text(encoding="utf-8")
        pattern = re.compile(rf"^(\s*-\s*\[)[ xX](\]\s+{re.escape(task_id)})(?=\s|:|$)(.*)$", re.MULTILINE)
        return bool(pattern.search(content) or task_id in content)
    except OSError:
        return False


def _is_duplicate_project_item(projects: dict[str, Any], work_id: str) -> bool:
    for p in projects.values():
        if isinstance(p, dict):
            wi = p.get("work_item")
            if isinstance(wi, dict) and (wi.get("work_id") == work_id or wi.get("admission_key") == work_id):
                return True
    return False


def _is_duplicate_work(doc: dict[str, Any], work_id: str) -> bool:
    dc = doc.get("delivery_control", {})
    if work_id in dc.get("work_items", {}):
        return True
    return _is_duplicate_project_item(dc.get("projects", {}), work_id)


def _extract_directive_str(ref: str) -> dict[str, str] | None:
    sep = "#" if "#" in ref else (":" if ":" in ref else None)
    if not sep:
        return None
    p, s = ref.split(sep, 1)
    if _is_hex_64(s):
        return {"path": p, "sha256": s.lower()}
    return None


def _extract_directive_dict(ref: dict[str, Any]) -> dict[str, str] | None:
    p = ref.get("path") or ref.get("authority")
    s = ref.get("sha256") or ref.get("sha")
    if p and s and _is_hex_64(str(s)):
        return {"path": str(p), "sha256": str(s).lower()}
    return None


def _extract_directive_entry(ref: Any) -> dict[str, str] | None:
    if isinstance(ref, str):
        return _extract_directive_str(ref)
    if isinstance(ref, dict):
        return _extract_directive_dict(ref)
    return None


def _record_applied_directive(dc: dict[str, Any], ref: Any) -> None:
    entry = _extract_directive_entry(ref)
    if not entry:
        return
    directives = dc.setdefault("directives", [])
    if not any(d.get("path") == entry["path"] and d.get("sha256") == entry["sha256"] for d in directives):
        directives.append(entry)


def _record_admitted_work(
    doc: dict[str, Any],
    work_id: str,
    project: str,
    work_item: dict[str, Any],
    iso_now: str,
    receipt_str: str,
    control_dir: Path | str | None = None,
) -> None:
    dc = doc["delivery_control"]
    dc["work_items"][work_id] = work_item
    _record_applied_directive(dc, work_item.get("approval_ref"))
    projects = dc["projects"]
    approval_entry = _project_approval(work_item.get("approval_ref"), control_dir)
    if project not in projects or not isinstance(projects[project], dict):
        projects[project] = {
            "owner": "",
            "stage": "ready",
            "next_action": f"Claim work {work_id}",
            "observed_at": iso_now,
            "last_progress_at": iso_now,
            "evidence": [receipt_str],
            "approval": approval_entry,
            "work_item": work_item,
            "workers": [],
        }
    else:
        p_row = projects[project]
        p_row["stage"] = "ready"
        p_row["next_action"] = f"Claim work {work_id}"
        p_row["observed_at"] = iso_now
        p_row["last_progress_at"] = iso_now
        p_row["work_item"] = work_item
        p_row["approval"] = approval_entry
        evidence_list = p_row.setdefault("evidence", [])
        if receipt_str not in evidence_list:
            evidence_list.append(receipt_str)


def _validate_admit_params(
    control_dir: Path | str,
    work_id: str,
    base_sha: str,
    approval_ref: str | dict[str, Any],
    approval_sha256: str | None,
    spec_path: Path | str | None,
    task_id: str,
    verify_spec: bool,
    iso_now: str,
) -> dict[str, Any] | None:
    if not _validate_base_sha(base_sha):
        return make_refusal(
            control_dir,
            work_id,
            "INVALID_BASE_SHA",
            f"Base SHA '{base_sha}' is not a valid 40-character hex string",
            now=iso_now,
        )
    path_str, expected_sha = _extract_approval_info(approval_ref, approval_sha256)
    if expected_sha and not _is_hex_64(expected_sha):
        return make_refusal(
            control_dir,
            work_id,
            "STALE_APPROVAL",
            f"Approval expected digest '{expected_sha}' is not a valid 64-hex SHA-256",
            now=iso_now,
        )
    appr_ok, appr_err = _verify_live_approval(approval_ref, control_dir, approval_sha256, is_admission=True)
    if not appr_ok:
        return make_refusal(
            control_dir,
            work_id,
            appr_err,
            f"Approval verification failed: {appr_err}",
            now=iso_now,
        )
    if verify_spec and spec_path and not _validate_spec_task(spec_path, task_id):
        return make_refusal(
            control_dir,
            work_id,
            "MISSING_SPEC_TASK",
            f"Task '{task_id}' not found in spec at '{spec_path}'",
            now=iso_now,
        )
    return None


def _resolve_approval_sha(
    clean_path: str, explicit_sha: str | None, control_dir: Path | str | None
) -> tuple[Path | None, str]:
    if not clean_path:
        return None, explicit_sha.lower() if explicit_sha else ""
    resolved, _, live_sha = _resolve_approval_file(clean_path, control_dir)
    if not explicit_sha:
        return resolved, live_sha or ""
    if not live_sha or not _check_sha_match(live_sha, explicit_sha):
        raise ValueError("Approval content drifted before pin")
    return resolved, explicit_sha.lower()


def _compute_stored_approval(
    approval_ref: str | dict[str, Any],
    approval_sha256: str | None = None,
    control_dir: Path | str | None = None,
) -> dict[str, Any]:
    path_str, explicit_sha = _extract_approval_info(approval_ref, approval_sha256)
    resolved, stored_sha = _resolve_approval_sha(path_str or "", explicit_sha, control_dir)
    target_path = str(resolved) if resolved else (path_str or "")
    if isinstance(approval_ref, dict):
        res = dict(approval_ref)
        res["path"] = target_path or str(approval_ref.get("path") or "")
        res["sha256"] = stored_sha
        return res
    return {"path": target_path or str(approval_ref), "sha256": stored_sha}


def _build_admitted_work_item(
    work_id: str,
    project: str,
    repository: str,
    task_id: str,
    spec_path: Path | str | None,
    base_sha: str,
    stored_approval: Any,
    req_action: str,
    prerequisites: list[str] | None,
    iso_now: str,
) -> dict[str, Any]:
    prereqs = list(prerequisites) if prerequisites else []
    return {
        "work_id": work_id,
        "admission_key": work_id,
        "project": project,
        "repository": repository,
        "task_id": task_id,
        "spec_path": str(spec_path) if spec_path else None,
        "base_sha": base_sha,
        "stage": "admitted",
        "prerequisites": prereqs,
        "approval_ref": stored_approval,
        "required_completion_action": req_action,
        "observed_at": iso_now,
        "claim": None,
    }


def admit_work(
    control_dir: Path | str,
    work_id: str,
    project: str,
    repository: str,
    task_id: str,
    base_sha: str,
    approval_ref: str | dict[str, Any],
    approval_sha256: str | None = None,
    required_completion_action: str = "merge_pr",
    spec_path: Path | str | None = None,
    prerequisites: list[str] | None = None,
    verify_spec: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    param_refusal = _validate_admit_params(
        control_dir,
        work_id,
        base_sha,
        approval_ref,
        approval_sha256,
        spec_path,
        task_id,
        verify_spec,
        iso_now,
    )
    if param_refusal:
        return param_refusal

    with workflow_lock(control_dir):
        recheck_ok, recheck_err = _verify_live_approval(approval_ref, control_dir, approval_sha256, is_admission=True)
        if not recheck_ok:
            return make_refusal(
                control_dir,
                work_id,
                recheck_err,
                f"Approval verification failed after lock: {recheck_err}",
                now=iso_now,
            )
        doc = load_coordinator_status(control_dir)
        if _is_duplicate_work(doc, work_id):
            return make_refusal(
                control_dir,
                work_id,
                "DUPLICATE_WORK_ID",
                f"Work ID '{work_id}' already admitted",
                now=iso_now,
            )

        stored_approval = _compute_stored_approval(approval_ref, approval_sha256, control_dir)
        req_action = (
            required_completion_action
            if required_completion_action in {"push", "publish_pr", "merge_pr"}
            else "merge_pr"
        )
        work_item = _build_admitted_work_item(
            work_id,
            project,
            repository,
            task_id,
            spec_path,
            base_sha,
            stored_approval,
            req_action,
            prerequisites,
            iso_now,
        )
        receipt = dict(work_item)
        receipt["status"] = "admitted"
        receipt_path = write_receipt(control_dir, f"admission_{work_id}.json", receipt)
        receipt["receipt_path"] = str(receipt_path)

        _record_admitted_work(doc, work_id, project, work_item, iso_now, str(receipt_path), control_dir)
        save_coordinator_status(control_dir, doc)
        return receipt


admit_work_item = admit_work


# -----------------------------------------------------------------------------
# 2. Claim Work
# -----------------------------------------------------------------------------


def _is_path_contained_in_root(target: Path, root: Path | str) -> bool:
    target_res = target.resolve()
    root_res = Path(root).resolve()
    try:
        return target_res == root_res or target_res.is_relative_to(root_res)
    except (AttributeError, ValueError):
        try:
            target_res.relative_to(root_res)
            return True
        except ValueError:
            return False


def _validate_worktree(worktree: Path | str | None, allowed_roots: list[str]) -> tuple[bool, str, str]:
    if not worktree or not isinstance(worktree, str | Path):
        return False, "SCOPE_ESCAPE", "Missing or invalid worktree path"
    wt_resolved = Path(worktree).resolve()
    in_root = any(_is_path_contained_in_root(wt_resolved, r) for r in allowed_roots)
    if not in_root:
        return False, "WORKSPACE_OUTSIDE_ALLOWED_ROOT", f"Worktree {wt_resolved} escapes allowed workspace roots"
    if PROTECTED_SKILL_REL in str(wt_resolved) or "/skills/portfolio-delivery-control" in str(wt_resolved):
        return (
            False,
            "PROTECTED_PATH_MODIFIED",
            f"Worktree {wt_resolved} overlaps protected staged skill directory",
        )
    return True, "", ""


def _validate_single_write_path(wp: Any) -> tuple[bool, str]:
    if not isinstance(wp, str) or not wp.strip():
        return False, "Invalid write path entry"
    if ".." in wp.split("/") or ".." in wp.split("\\"):
        return False, f"Write path {wp} attempts directory traversal"
    if PROTECTED_SKILL_REL in wp or "/skills/portfolio-delivery-control" in wp:
        return (
            False,
            f"Write path {wp} overlaps protected staged skill directory",
        )
    return True, ""


def _validate_write_paths(write_paths: list[str] | None) -> tuple[bool, str]:
    if not write_paths or not isinstance(write_paths, list):
        return False, "Missing or invalid write_paths list"
    for wp in write_paths:
        ok, err = _validate_single_write_path(wp)
        if not ok:
            return False, err
    return True, ""


def _validate_scope(
    worktree: Path | str | None,
    write_paths: list[str] | None,
    allowed_roots: list[str],
) -> tuple[bool, str, str]:
    ok, err_code, err_msg = _validate_worktree(worktree, allowed_roots)
    if not ok:
        return False, err_code, err_msg
    wp_ok, wp_err = _validate_write_paths(write_paths)
    if not wp_ok:
        return False, "SCOPE_ESCAPE", wp_err
    return True, "", ""


def _check_prerequisites_met(doc: dict[str, Any], prerequisites: list[str]) -> bool:
    work_items = doc.get("delivery_control", {}).get("work_items", {})
    for pid in prerequisites:
        if pid not in work_items:
            return False
        pstage = work_items[pid].get("stage")
        if pstage not in ("done", "complete", "merged"):
            return False
    return True


def _claim_is_active(claim: dict[str, Any] | None, now_dt: datetime.datetime) -> bool:
    if not claim:
        return False
    exp_str = claim.get("expires_at")
    if not exp_str:
        return False
    try:
        return datetime.datetime.fromisoformat(exp_str) > now_dt
    except (ValueError, TypeError):
        return False


def _single_path_overlaps_list(sx: str, l2: list[Any]) -> bool:
    for y in l2:
        sy = str(y).strip()
        if sy and paths_overlap(sx, sy):
            return True
    return False


def _paths_overlap(p1: list[str] | str, p2: list[str] | str) -> bool:
    l1 = [p1] if isinstance(p1, str) else list(p1)
    l2 = [p2] if isinstance(p2, str) else list(p2)
    for x in l1:
        sx = str(x).strip()
        if sx and _single_path_overlaps_list(sx, l2):
            return True
    return False


_paths_list_overlap = _paths_overlap


def _check_work_id_claim(
    wi: dict[str, Any] | None, work_id: str, owner: str, now_dt: datetime.datetime
) -> tuple[bool, str]:
    if wi:
        c = wi.get("claim")
        if c and c.get("owner") != owner and _claim_is_active(c, now_dt):
            return True, f"Active claim already held on {work_id} by {c.get('owner')}"
    return False, ""


def _is_same_or_unspecified_repo(c_repo: str, req_repo: str) -> bool:
    if not c_repo or not req_repo:
        return True
    return c_repo == req_repo


def _is_global_claim_conflict(c: dict[str, Any], owner_id: str, session_id: str) -> bool:
    c_owner = c.get("owner_id") or c.get("owner")
    if c_owner == owner_id:
        return True
    if session_id and c.get("session_id") == session_id:
        return True
    return False


def _is_claim_conflict(
    c: dict[str, Any],
    owner_id: str,
    session_id: str,
    repository: Any = "",
    worktree: str = "",
    write_paths: list[str] | None = None,
) -> bool:
    if isinstance(repository, list):
        write_paths, repository, worktree = repository, "", ""
    if _is_global_claim_conflict(c, owner_id, session_id):
        return True
    c_repo = str(c.get("repository", "")).strip()
    req_repo = str(repository).strip()
    c_wt = str(c.get("worktree", "")).strip()
    req_wt = str(worktree).strip()
    same_scope = (
        not c_repo or not req_repo or c_repo == req_repo or bool(c_wt and req_wt and _paths_overlap([c_wt], [req_wt]))
    )
    if not same_scope:
        return False
    return _paths_overlap(c.get("write_paths", []), write_paths or [])


def _check_single_item_conflict(
    wi: dict[str, Any],
    owner: str,
    session_id: str,
    write_paths: list[str],
    now_dt: datetime.datetime,
    repository: str = "",
    worktree: str = "",
) -> bool:
    c = wi.get("claim")
    if not c or not _claim_is_active(c, now_dt):
        return False
    c_repo = c.get("repository") or wi.get("repository", "")
    claim_dict = c if "repository" in c else {**c, "repository": c_repo}
    return _is_claim_conflict(claim_dict, owner, session_id, repository, worktree, write_paths)


def _find_writer_conflict(
    doc: dict[str, Any],
    owner: str,
    session_id: str,
    work_id: str,
    write_paths: list[str],
    now_dt: datetime.datetime,
    repository: str = "",
    worktree: str = "",
) -> tuple[bool, str]:
    work_items = doc.get("delivery_control", {}).get("work_items", {})
    for wid, wi in work_items.items():
        if wid == work_id:
            continue
        if _check_single_item_conflict(wi, owner, session_id, write_paths, now_dt, repository, worktree):
            return True, f"Active claim on {wid} conflicts with owner, session, or write_paths"
    return False, ""


def _find_competing_claim(
    doc: dict[str, Any],
    work_id: str,
    owner: str,
    now_dt: datetime.datetime,
) -> tuple[bool, str]:
    work_items = doc.get("delivery_control", {}).get("work_items", {})
    held, err = _check_work_id_claim(work_items.get(work_id), work_id, owner, now_dt)
    if held:
        return True, err
    return False, ""


def _update_project_claim_state(
    doc: dict[str, Any],
    project: str,
    work_id: str,
    owner: str,
    claim_token: str,
    expires_at: str,
    worktree: Path,
    write_paths: list[str],
    model: str,
    approval_ref: Any,
    iso_now: str,
    receipt_str: str,
    control_dir: Path | str | None = None,
) -> None:
    dc = doc["delivery_control"]
    p_row = dc["projects"].setdefault(project, {})
    p_row["owner"] = owner
    p_row["stage"] = "implementing"
    p_row["last_progress_at"] = iso_now
    p_row["observed_at"] = iso_now
    p_row["approval"] = _project_approval(approval_ref, control_dir)
    evidence_list = p_row.setdefault("evidence", [])
    if receipt_str not in evidence_list:
        evidence_list.append(receipt_str)

    p_row["work_item"] = {
        "work_id": work_id,
        "admission_key": work_id,
        "claim_token": claim_token,
        "claim_expires_at": expires_at,
        "stage": "implementing",
        "owner": owner,
        "attempt": 1,
    }
    p_row["workers"] = [
        {
            "id": owner,
            "role": "implementation",
            "model": model,
            "user": owner,
            "worktree": str(worktree),
            "write_paths": write_paths,
            "sandbox": True,
            "permission_bypass": False,
        }
    ]


def claim_work(
    control_dir: Path | str,
    work_id: str,
    owner: str,
    worktree: Path | str,
    write_paths: list[str],
    ttl_seconds: int = 3600,
    model: str = "gemini-3.8-flash-high",
    allowed_roots: list[str] | None = None,
    session_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    now_dt = datetime.datetime.fromisoformat(iso_now)
    roots = allowed_roots or DEFAULT_ALLOWED_ROOTS
    actual_session = session_id or owner

    scope_ok, scope_code, scope_err = _validate_scope(worktree, write_paths, roots)
    if not scope_ok:
        return make_refusal(
            control_dir,
            work_id,
            scope_code or "SCOPE_ESCAPE",
            scope_err,
            extra={"owner": owner},
            now=iso_now,
        )

    with workflow_lock(control_dir):
        doc = load_coordinator_status(control_dir)
        dc = doc["delivery_control"]
        if work_id not in dc["work_items"]:
            return make_refusal(
                control_dir,
                work_id,
                "WORK_NOT_ADMITTED",
                f"Work ID '{work_id}' not admitted",
                extra={"owner": owner},
                now=iso_now,
            )

        wi = dc["work_items"][work_id]
        if not _check_prerequisites_met(doc, wi.get("prerequisites", [])):
            return make_refusal(
                control_dir,
                work_id,
                "UNMET_PREREQUISITES",
                f"Prerequisites for '{work_id}' are not completed/merged",
                extra={"owner": owner},
                now=iso_now,
            )

        repository = str(wi.get("repository", "")).strip()
        worktree_str = str(Path(worktree).resolve())
        has_conflict, conf_err = _find_writer_conflict(
            doc,
            owner,
            actual_session,
            work_id,
            write_paths,
            now_dt,
            repository=repository,
            worktree=worktree_str,
        )
        if has_conflict:
            return make_refusal(
                control_dir,
                work_id,
                "WRITER_CONFLICT",
                conf_err,
                extra={"owner": owner},
                now=iso_now,
            )

        comp_claim, comp_err = _find_competing_claim(doc, work_id, owner, now_dt)
        if comp_claim:
            return make_refusal(
                control_dir,
                work_id,
                "COMPETING_ACTIVE_CLAIM",
                comp_err,
                extra={"owner": owner},
                now=iso_now,
            )

        claim_token = f"claim_{secrets.token_hex(20)}"
        expires_at = (now_dt + datetime.timedelta(seconds=ttl_seconds)).isoformat()
        wt_resolved = Path(worktree).resolve()

        wi["claim"] = {
            "owner": owner,
            "session_id": actual_session,
            "claim_token": claim_token,
            "repository": repository,
            "worktree": str(wt_resolved),
            "write_paths": write_paths,
            "allowed_rel_paths": list(write_paths),
            "model": model,
            "expires_at": expires_at,
            "claimed_at": iso_now,
        }
        wi["stage"] = "implementing"

        receipt = {
            "status": "claimed",
            "work_id": work_id,
            "owner": owner,
            "session_id": actual_session,
            "claim_token": claim_token,
            "repository": repository,
            "worktree": str(wt_resolved),
            "write_paths": write_paths,
            "allowed_rel_paths": list(write_paths),
            "expires_at": expires_at,
            "observed_at": iso_now,
        }
        receipt_path = write_receipt(control_dir, f"claim_{work_id}.json", receipt)
        receipt["receipt_path"] = str(receipt_path)

        project = wi.get("project", "default")
        _update_project_claim_state(
            doc,
            project,
            work_id,
            owner,
            claim_token,
            expires_at,
            wt_resolved,
            write_paths,
            model,
            wi.get("approval_ref"),
            iso_now,
            str(receipt_path),
            control_dir,
        )
        save_coordinator_status(control_dir, doc)
        return receipt


# -----------------------------------------------------------------------------
# 3. Record Implementation
# -----------------------------------------------------------------------------


def _verify_active_claim(
    wi: dict[str, Any], owner: str, claim_token: str, now_dt: datetime.datetime
) -> tuple[bool, str]:
    claim = wi.get("claim")
    if not claim or claim.get("owner") != owner or claim.get("claim_token") != claim_token:
        return False, "STALE_CLAIM"
    exp_str = claim.get("expires_at")
    if not exp_str:
        return False, "CLAIM_EXPIRED"
    try:
        if datetime.datetime.fromisoformat(exp_str) <= now_dt:
            return False, "CLAIM_EXPIRED"
    except (ValueError, TypeError):
        return False, "CLAIM_EXPIRED"
    return True, ""


def _verify_mandatory_gates(gate_results: dict[str, Any] | None) -> bool:
    if not isinstance(gate_results, dict):
        return False
    if "test_exit_code" in gate_results:
        return gate_results["test_exit_code"] == 0
    tests = gate_results.get("tests") or gate_results.get("pytest")
    if isinstance(tests, dict):
        if tests.get("exit_code") == 0 or tests.get("status") == "passed":
            return True
    return False


def _verify_artifact_hashes(artifacts: list[tuple[str, str | None]]) -> bool:
    for path_str, expected_hash in artifacts:
        if path_str and expected_hash:
            p = Path(path_str)
            if not p.exists() or sha256_file(p).lower() != expected_hash.lower():
                return False
    return True


def _run_cmd(cmd: list[str], cwd: Path | str, runner: Callable[..., Any] | None = None) -> tuple[int, str]:
    if runner is not None:
        res = runner(cmd, cwd=cwd)
        if isinstance(res, tuple):
            return res[0], res[1]
        return getattr(res, "returncode", 0), getattr(res, "stdout", "")
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    return p.returncode, p.stdout.strip()


def _verify_head_and_tree(
    worktree: Path,
    commit_sha: str,
    tree_sha: str,
    runner: Callable[..., Any] | None,
) -> tuple[bool, str]:
    code, head = _run_cmd(["git", "rev-parse", "HEAD"], worktree, runner)
    if code != 0 or head != commit_sha:
        return False, "HEAD_MISMATCH"
    code, tree = _run_cmd(["git", "rev-parse", "HEAD^{tree}"], worktree, runner)
    if code != 0 or tree != tree_sha:
        return False, "TREE_MISMATCH"
    return True, ""


def _verify_candidate_parent(
    worktree: Path,
    parent_sha: str,
    base_sha: str,
    runner: Callable[..., Any] | None,
) -> tuple[bool, str]:
    code, parent = _run_cmd(["git", "rev-parse", "HEAD^1"], worktree, runner)
    if code != 0:
        return False, "BASE_SHA_DRIFT"
    clean_parent = parent.strip()
    clean_p = parent_sha.strip() if parent_sha else ""
    clean_b = base_sha.strip() if base_sha else ""
    if clean_p and clean_parent != clean_p:
        return False, "BASE_SHA_DRIFT"
    if clean_b and clean_parent != clean_b:
        if clean_p and clean_parent == clean_p:
            return False, "PARENT_BASE_MISMATCH"
        return False, "BASE_SHA_DRIFT"
    return True, ""


def _verify_ancestry_and_count(
    worktree: Path,
    base_sha: str,
    runner: Callable[..., Any] | None,
) -> tuple[bool, str]:
    if base_sha:
        code, count_rev = _run_cmd(["git", "rev-list", "--count", f"HEAD..{base_sha}"], worktree, runner)
        if code == 0 and count_rev.strip() and int(count_rev.strip()) != 0:
            return False, "BASE_SHA_DRIFT"
        anc_code, _ = _run_cmd(["git", "merge-base", "--is-ancestor", base_sha, "HEAD"], worktree, runner)
        if anc_code != 0:
            return False, "BASE_SHA_DRIFT"
    code, count_str = _run_cmd(["git", "rev-list", "--count", f"{base_sha}..HEAD"], worktree, runner)
    if code != 0:
        return False, "BASE_SHA_DRIFT"
    try:
        if int(count_str.strip()) != 1:
            return False, "NON_SINGLE_COMMIT_RANGE"
    except ValueError:
        return False, "NON_SINGLE_COMMIT_RANGE"
    return True, ""


def _verify_parent_and_count(
    worktree: Path,
    parent_sha: str,
    base_sha: str,
    runner: Callable[..., Any] | None,
) -> tuple[bool, str]:
    p_ok, p_err = _verify_candidate_parent(worktree, parent_sha, base_sha, runner)
    if not p_ok:
        return False, p_err
    return _verify_ancestry_and_count(worktree, base_sha, runner)


def _inspect_git_commit(
    worktree: Path,
    commit_sha: str,
    tree_sha: str,
    parent_sha: str,
    base_sha: str,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    ok, err = _verify_head_and_tree(worktree, commit_sha, tree_sha, runner)
    if not ok:
        return False, err
    ok, err = _verify_parent_and_count(worktree, parent_sha, base_sha, runner)
    if not ok:
        return False, err
    code, ident = _run_cmd(["git", "log", "-1", "--format=%an <%ae>|%cn <%ce>"], worktree, runner)
    if code != 0 or "|" not in ident:
        return False, "INVALID_COMMIT_IDENTITY"
    author_ident, committer_ident = ident.strip().split("|", 1)
    if not (_is_approved_identity(author_ident) and _is_approved_identity(committer_ident)):
        return False, "INVALID_COMMIT_IDENTITY"
    return True, ""


def _invalidate_prior_reviews_if_revision_changed(wi: dict[str, Any], commit_sha: str, tree_sha: str) -> None:
    old_cand = wi.get("candidate", {})
    old_commit = old_cand.get("commit_sha") or wi.get("revision")
    old_tree = old_cand.get("tree_sha") or wi.get("tree_sha")
    if old_commit and (old_commit != commit_sha or old_tree != tree_sha):
        wi["reviews"] = []
        wi["qualified"] = False
        wi["remediation_required"] = False
        wi["defects"] = []
        wi["invalidated_reviews_from_sha"] = old_commit
        wi["transitions"] = [
            t for t in wi.get("transitions", []) if t.get("commit_sha") == commit_sha and t.get("tree_sha") == tree_sha
        ]


def _verify_impl_claim_and_git(
    control_dir: Path | str,
    doc: dict[str, Any],
    work_id: str,
    owner: str,
    claim_token: str,
    now_dt: datetime.datetime,
    commit_sha: str,
    tree_sha: str,
    parent_sha: str,
    git_runner: Callable[..., Any] | None,
    iso_now: str,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    dc = doc["delivery_control"]
    if work_id not in dc["work_items"]:
        ref = make_refusal(control_dir, work_id, "WORK_NOT_ADMITTED", "Work ID not admitted", now=iso_now)
        return False, ref, None
    wi = dc["work_items"][work_id]
    claim_ok, claim_err = _verify_active_claim(wi, owner, claim_token, now_dt)
    if not claim_ok:
        ref = make_refusal(control_dir, work_id, claim_err, "Claim token invalid or expired", now=iso_now)
        return False, ref, None
    wt_path = Path(wi["claim"]["worktree"])
    git_ok, git_err = _inspect_git_commit(wt_path, commit_sha, tree_sha, parent_sha, wi.get("base_sha", ""), git_runner)
    if not git_ok:
        ref = make_refusal(control_dir, work_id, git_err, f"Git inspection failed: {git_err}", now=iso_now)
        return False, ref, None

    repo = wi.get("repository", "")
    protect = wi.get("protect_skill_path", False)
    allowed = wi["claim"].get("allowed_rel_paths") or wi["claim"].get("write_paths")
    diff_ok, diff_err = _inspect_modified_paths(
        wt_path, wi.get("base_sha", ""), commit_sha, repo, protect, allowed, git_runner
    )
    if not diff_ok:
        ref = make_refusal(control_dir, work_id, diff_err, f"Path inspection failed: {diff_err}", now=iso_now)
        return False, ref, None

    return True, None, wi


def _populate_candidate_and_project(
    doc: dict[str, Any],
    wi: dict[str, Any],
    round: int,
    commit_sha: str,
    tree_sha: str,
    parent_sha: str,
    patch_path: Path | str | None,
    patch_sha256: str | None,
    bundle_path: Path | str | None,
    bundle_sha256: str | None,
    receipt_path: Path | str | None,
    receipt_sha256: str | None,
    gate_results: dict[str, Any],
    iso_now: str,
    control_dir: Path | str | None = None,
) -> None:
    wi["candidate"] = {
        "round": round,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "parent_sha": parent_sha,
        "implementer_model": wi.get("claim", {}).get("model", "gemini-3.8-flash-high"),
        "patch_path": str(patch_path) if patch_path else None,
        "patch_sha256": patch_sha256,
        "bundle_path": str(bundle_path) if bundle_path else None,
        "bundle_sha256": bundle_sha256,
        "receipt_path": str(receipt_path) if receipt_path else None,
        "receipt_sha256": receipt_sha256,
        "gate_results": gate_results,
        "recorded_at": iso_now,
    }
    wi["revision"] = commit_sha
    wi["commit_sha"] = commit_sha
    wi["head"] = commit_sha
    wi["tree_sha"] = tree_sha
    wi["stage"] = "review"

    p_row = _project_work_item(doc, wi, iso_now, control_dir=control_dir)
    if "work_item" in p_row and isinstance(p_row["work_item"], dict):
        p_row["work_item"]["revision"] = commit_sha
        p_row["work_item"]["stage"] = "review"


def _check_impl_artifacts(
    patch_path: Path | str | None,
    patch_sha256: str | None,
    bundle_path: Path | str | None,
    bundle_sha256: str | None,
    receipt_path: Path | str | None,
    receipt_sha256: str | None,
) -> bool:
    artifacts = [
        (str(patch_path) if patch_path else "", patch_sha256),
        (str(bundle_path) if bundle_path else "", bundle_sha256),
        (str(receipt_path) if receipt_path else "", receipt_sha256),
    ]
    return _verify_artifact_hashes(artifacts)


def record_implementation(
    control_dir: Path | str,
    work_id: str,
    owner: str,
    claim_token: str,
    round: int,
    commit_sha: str,
    tree_sha: str,
    parent_sha: str,
    gate_results: dict[str, Any],
    patch_path: Path | str | None = None,
    patch_sha256: str | None = None,
    bundle_path: Path | str | None = None,
    bundle_sha256: str | None = None,
    receipt_path: Path | str | None = None,
    receipt_sha256: str | None = None,
    git_runner: Callable[..., Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    now_dt = datetime.datetime.fromisoformat(iso_now)
    if not _verify_mandatory_gates(gate_results):
        return make_refusal(
            control_dir,
            work_id,
            "MISSING_MANDATORY_GATE",
            "Mandatory quality gates missing or failed",
            now=iso_now,
        )

    if not _check_impl_artifacts(patch_path, patch_sha256, bundle_path, bundle_sha256, receipt_path, receipt_sha256):
        return make_refusal(
            control_dir,
            work_id,
            "ARTIFACT_HASH_MISMATCH",
            "Artifact SHA-256 did not match expected hash",
            now=iso_now,
        )

    with workflow_lock(control_dir):
        doc = load_coordinator_status(control_dir)
        ok, ref, wi = _verify_impl_claim_and_git(
            control_dir, doc, work_id, owner, claim_token, now_dt, commit_sha, tree_sha, parent_sha, git_runner, iso_now
        )
        if not ok or wi is None:
            return ref or {}

        _invalidate_prior_reviews_if_revision_changed(wi, commit_sha, tree_sha)
        _populate_candidate_and_project(
            doc,
            wi,
            round,
            commit_sha,
            tree_sha,
            parent_sha,
            patch_path,
            patch_sha256,
            bundle_path,
            bundle_sha256,
            receipt_path,
            receipt_sha256,
            gate_results,
            iso_now,
            control_dir,
        )

        receipt = {
            "status": "recorded",
            "stage": "review",
            "work_id": work_id,
            "round": round,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "observed_at": iso_now,
        }
        trans_path = write_receipt(control_dir, f"transition_{work_id}_r{round}.json", receipt)
        receipt["receipt_path"] = str(trans_path)
        save_coordinator_status(control_dir, doc)
        return receipt


# -----------------------------------------------------------------------------
# 4. Record Review
# -----------------------------------------------------------------------------


def _parse_and_verify_review_report(report_path: Path | str, report_sha256: str) -> tuple[bool, str, dict[str, Any]]:
    p = Path(report_path)
    if not p.exists() or sha256_file(p).lower() != report_sha256.lower():
        return False, "ARTIFACT_HASH_MISMATCH", {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False, "REPORT_INVALID", {}
        return True, "", data
    except (json.JSONDecodeError, OSError):
        return False, "REPORT_INVALID", {}


def _verify_review_report(
    report_data: dict[str, Any] | Path | str,
    candidate: dict[str, Any],
) -> bool:
    if isinstance(report_data, str | Path):
        try:
            data = json.loads(Path(report_data).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
    else:
        data = report_data
    if not isinstance(data, dict):
        return False
    rev_commit = _extract_sha_from_report(data, COMMIT_SHA_KEYS)
    rev_tree = _extract_sha_from_report(data, TREE_SHA_KEYS)
    cand_commit = candidate.get("commit_sha")
    cand_tree = candidate.get("tree_sha")
    if not _is_hex_40(rev_commit) or not _is_hex_40(cand_commit) or rev_commit != cand_commit:
        return False
    if not _is_hex_40(rev_tree) or not _is_hex_40(cand_tree) or rev_tree != cand_tree:
        return False
    return True


_verify_review_revisions = _verify_review_report


def _read_report_sessions_from_disk(rep_p: str | Path | None) -> list[str]:
    if not rep_p:
        return []
    p = Path(rep_p)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _extract_report_sessions(data)
    except Exception:
        pass
    return []


def _collect_review_sessions(r: dict[str, Any]) -> set[str]:
    sessions: set[str] = set()
    for s in _extract_report_sessions(r):
        if s:
            sessions.add(s)
    for s in r.get("embedded_sessions", []):
        if isinstance(s, str) and s.strip():
            sessions.add(s.strip())
    for disk_s in _read_report_sessions_from_disk(r.get("report_path")):
        if disk_s:
            sessions.add(disk_s)
    return sessions


def _verify_reviewer_uniqueness(
    reviews: list[dict[str, Any]],
    family: str,
    session_id: str,
    rep_sessions: list[str] | None = None,
) -> tuple[bool, str]:
    curr = {session_id.strip()}
    if rep_sessions:
        curr.update(s.strip() for s in rep_sessions if s.strip())
    for r in reviews:
        if r.get("family") == family:
            return False, "DUPLICATE_REVIEWER_FAMILY"
        existing = _collect_review_sessions(r)
        if curr.intersection(existing):
            return False, "DUPLICATE_REVIEWER_SESSION"
    return True, ""


def _check_qualification(reviews: list[dict[str, Any]], commit_sha: str, tree_sha: str) -> bool:
    ship_revs = [
        r
        for r in reviews
        if r.get("verdict") == "SHIP" and r.get("commit_sha") == commit_sha and r.get("tree_sha") == tree_sha
    ]
    families = {r.get("family") for r in ship_revs}
    sessions = {r.get("session_id") for r in ship_revs}
    return families == VALID_FAMILIES and len(sessions) >= 3 and len(ship_revs) >= 3


def _process_revise_verdict(
    wi: dict[str, Any],
    p_row: dict[str, Any],
    report_data: dict[str, Any],
    review_entry: dict[str, Any],
    rec_filename: str,
    control_dir: Path | str | None = None,
) -> None:
    defects = report_data.get("defects") or report_data.get("defect_ids") or report_data.get("findings") or []
    wi["stage"] = "implementing"
    wi["remediation_required"] = True
    wi["defects"] = defects
    wi["qualified"] = False
    reviews = wi.setdefault("reviews", [])
    reviews.append(review_entry)
    p_row["stage"] = "implementing"
    p_row["next_action"] = f"Remediate defects for {wi.get('work_id')}"
    p_row["reviews"] = _project_reviews(reviews, rec_filename)
    p_row["approval"] = _project_approval(wi.get("approval_ref"), control_dir)
    p_row["ci"] = {"revision": wi.get("revision", ""), "status": "pending", "evidence": []}
    if "branch_protection" not in p_row:
        p_row["branch_protection"] = {"verified": False, "evidence": []}
    if "work_item" in p_row and isinstance(p_row["work_item"], dict):
        p_row["work_item"]["stage"] = "implementing"


def _process_ship_verdict(
    wi: dict[str, Any],
    p_row: dict[str, Any],
    review_entry: dict[str, Any],
    commit_sha: str,
    tree_sha: str,
    rec_filename: str,
    control_dir: Path | str | None = None,
) -> bool:
    reviews = wi.setdefault("reviews", [])
    reviews.append(review_entry)
    qualified = _check_qualification(reviews, commit_sha, tree_sha)
    wi["qualified"] = qualified
    p_row["reviews"] = _project_reviews(reviews, rec_filename)
    p_row["approval"] = _project_approval(wi.get("approval_ref"), control_dir)
    if qualified:
        wi["remediation_required"] = False
        wi["defects"] = []
        wi["stage"] = "merge_ready"
        p_row["stage"] = "merge_ready"
        p_row["next_action"] = f"Merge candidate {commit_sha[:8]}"
        p_row["ci"] = {"revision": commit_sha, "status": "pending", "evidence": []}
        if "branch_protection" not in p_row:
            p_row["branch_protection"] = {"verified": False, "evidence": []}
        if "work_item" in p_row and isinstance(p_row["work_item"], dict):
            p_row["work_item"]["stage"] = "merge_ready"
    return qualified


def _validate_review_args(model: str, family: str, verdict: str) -> tuple[bool, str]:
    if model in FORBIDDEN_MODELS:
        return False, "FORBIDDEN_MODEL"
    if "flash" in model.lower():
        return False, "SELF_REVIEW_FORBIDDEN"
    if family not in VALID_FAMILIES or verdict not in VALID_VERDICTS:
        return False, "INVALID_REVIEW_PARAMETERS"
    if _classify_family(model) != family.strip().lower():
        return False, "UNAPPROVED_REVIEWER_MODEL"
    return True, ""


def _load_and_validate_review_report(
    report_path: Path | str,
    report_sha256: str,
    verdict: str,
    family: str,
    model: str,
    reviewer_id: str = "",
    session_id: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    rep_ok, rep_err, rep_data = _parse_and_verify_review_report(report_path, report_sha256)
    if not rep_ok:
        return False, rep_err, {}
    emb_verdict = _extract_embedded_verdict(rep_data)
    if not emb_verdict or emb_verdict.upper() != verdict.upper():
        return False, "REPORT_VERDICT_MISMATCH", {}
    if not _verify_model_family(family, model, rep_data):
        return False, "REPORT_IDENTITY_MISMATCH", {}
    return True, "", rep_data


def _check_review_candidate_revision(wi: dict[str, Any]) -> tuple[bool, str]:
    cand = wi.get("candidate", {})
    cand_commit = wi.get("commit_sha") or cand.get("commit_sha")
    cand_tree = wi.get("tree_sha") or cand.get("tree_sha")
    if _is_hex_40(cand_commit) and _is_hex_40(cand_tree):
        return True, ""
    return False, "MISSING_CANDIDATE_REVISION"


def _verify_review_preconditions(
    control_dir: Path | str,
    doc: dict[str, Any],
    work_id: str,
    reviewer_id: str,
    session_id: str,
    model: str,
    rep_data: dict[str, Any],
    family: str,
    iso_now: str,
    report_path: Path | str = "",
    report_sha256: str = "",
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    dc = doc["delivery_control"]
    if work_id not in dc["work_items"]:
        ref = make_refusal(
            control_dir,
            work_id,
            "WORK_NOT_ADMITTED",
            "Work ID not admitted",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )
        return False, ref, None
    wi = dc["work_items"][work_id]
    cand_ok, cand_err = _check_review_candidate_revision(wi)
    if not cand_ok:
        ref = make_refusal(
            control_dir,
            work_id,
            cand_err,
            "No candidate implementation or candidate revision is not 40-hex",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )
        return False, ref, None
    cand = wi.get("candidate", {})
    if not _verify_review_report(rep_data, cand):
        ref = make_refusal(
            control_dir,
            work_id,
            "REVISION_MISMATCH",
            "Revision mismatch in review report",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )
        return False, ref, None
    if _check_report_duplicate(wi.get("reviews", []), report_path, report_sha256):
        ref = make_refusal(
            control_dir,
            work_id,
            "REPORT_IDENTITY_MISMATCH",
            "Duplicate review report reuse across reviewers forbidden",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )
        return False, ref, None
    if _check_self_review(wi, reviewer_id, session_id, model, rep_data):
        ref = make_refusal(
            control_dir,
            work_id,
            "SELF_REVIEW_FORBIDDEN",
            "Self-review by implementer or flash model forbidden",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )
        return False, ref, None
    emb_ok, emb_err = _check_embedded_consistency(rep_data, reviewer_id, session_id)
    if not emb_ok:
        ref = make_refusal(
            control_dir,
            work_id,
            emb_err,
            f"Embedded reviewer or session contradicts review invocation: {emb_err}",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )
        return False, ref, None
    rep_sessions = _extract_report_sessions(rep_data)
    uniq_ok, uniq_err = _verify_reviewer_uniqueness(wi.get("reviews", []), family, session_id, rep_sessions)
    if not uniq_ok:
        ref = make_refusal(
            control_dir,
            work_id,
            uniq_err,
            f"Duplicate review: {uniq_err}",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )
        return False, ref, None
    return True, None, wi


def _build_review_receipt(
    work_id: str,
    verdict: str,
    commit_sha: str,
    tree_sha: str,
    family: str,
    model: str,
    session_id: str,
    conv_id: str,
    rep_sessions: list[str],
    report_path: Path | str,
    iso_now: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    receipt = {
        "status": "recorded",
        "verdict": verdict,
        "work_id": work_id,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "revision": commit_sha,
        "head": commit_sha,
        "reviewer_family": family,
        "reviewer_model": model,
        "reviewer_session_id": session_id,
        "conversation_id": conv_id,
        "embedded_sessions": rep_sessions or [session_id],
        "report_path": str(report_path),
        "observed_at": iso_now,
    }
    receipt.update(extra)
    return receipt


def _prepare_review_entry(
    wi: dict[str, Any],
    reviewer_id: str,
    family: str,
    model: str,
    session_id: str,
    verdict: str,
    report_path: Path | str,
    report_sha256: str,
    rep_sessions: list[str],
    iso_now: str,
) -> tuple[dict[str, Any], str, str, str]:
    cand = wi["candidate"]
    commit_sha = str(wi.get("commit_sha") or cand["commit_sha"])
    tree_sha = str(wi.get("tree_sha") or cand["tree_sha"])
    conv_id = rep_sessions[0] if rep_sessions else session_id
    entry_sessions = rep_sessions if rep_sessions else [session_id]
    review_entry = {
        "reviewer_id": reviewer_id,
        "role": family,
        "model": model,
        "reviewer_model": model,
        "family": family,
        "reviewer_family": family,
        "session_id": session_id,
        "reviewer_session_id": session_id,
        "conversation_id": conv_id,
        "embedded_sessions": entry_sessions,
        "verdict": verdict,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "revision": commit_sha,
        "head": commit_sha,
        "report_path": str(report_path),
        "report_sha256": report_sha256,
        "observed_at": iso_now,
    }
    return review_entry, commit_sha, tree_sha, conv_id


def _apply_review_verdict(
    wi: dict[str, Any],
    p_row: dict[str, Any],
    verdict: str,
    rep_data: dict[str, Any],
    review_entry: dict[str, Any],
    commit_sha: str,
    tree_sha: str,
    rec_filename: str,
    control_dir: Path | str,
) -> dict[str, Any]:
    if verdict == "REVISE":
        _process_revise_verdict(wi, p_row, rep_data, review_entry, rec_filename, control_dir=control_dir)
        return {
            "stage": "implementing",
            "remediation_required": True,
            "defects": wi.get("defects", []),
        }
    qualified = _process_ship_verdict(
        wi, p_row, review_entry, commit_sha, tree_sha, rec_filename, control_dir=control_dir
    )
    return {
        "stage": wi.get("stage", "review"),
        "qualified": qualified,
    }


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
    iso_now = now if now else iso_timestamp()
    args_ok, args_err = _validate_review_args(model, family, verdict)
    if not args_ok:
        return make_refusal(
            control_dir,
            work_id,
            args_err,
            f"Invalid parameters: {args_err}",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )
    rep_ok, rep_err, rep_data = _load_and_validate_review_report(
        report_path, report_sha256, verdict, family, model, reviewer_id, session_id
    )
    if not rep_ok:
        return make_refusal(
            control_dir,
            work_id,
            rep_err,
            f"Review report verification failed: {rep_err}",
            extra={"reviewer_id": reviewer_id},
            now=iso_now,
        )

    with workflow_lock(control_dir):
        doc = load_coordinator_status(control_dir)
        ok, ref, wi = _verify_review_preconditions(
            control_dir,
            doc,
            work_id,
            reviewer_id,
            session_id,
            model,
            rep_data,
            family,
            iso_now,
            report_path=report_path,
            report_sha256=report_sha256,
        )
        if not ok or wi is None:
            return ref or {}

        rep_sessions = _extract_report_sessions(rep_data)
        review_entry, commit_sha, tree_sha, conv_id = _prepare_review_entry(
            wi, reviewer_id, family, model, session_id, verdict, report_path, report_sha256, rep_sessions, iso_now
        )
        project = wi.get("project", "default")
        p_row = doc["delivery_control"]["projects"].setdefault(project, {})
        p_row["last_progress_at"] = iso_now
        p_row["observed_at"] = iso_now

        rec_filename = f"review_{work_id}_{family}_{session_id}.json"
        extra = _apply_review_verdict(
            wi, p_row, verdict, rep_data, review_entry, commit_sha, tree_sha, rec_filename, control_dir
        )
        receipt = _build_review_receipt(
            work_id=work_id,
            verdict=verdict,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            family=family,
            model=model,
            session_id=session_id,
            conv_id=conv_id,
            rep_sessions=rep_sessions,
            report_path=report_path,
            iso_now=iso_now,
            extra=extra,
        )
        rec_path = write_receipt(control_dir, rec_filename, receipt)
        receipt["receipt_path"] = str(rec_path)
        save_coordinator_status(control_dir, doc)
        return receipt


# -----------------------------------------------------------------------------
# 5. Execute Transition
# -----------------------------------------------------------------------------


def _guard_approval_and_defects(wi: dict[str, Any], control_dir: Path | str | None = None) -> tuple[bool, str]:
    if not wi.get("approval_ref"):
        return False, "ABSENT_APPROVAL"
    if wi.get("remediation_required") or wi.get("defects"):
        return False, "UNRESOLVED_REVISE_FINDINGS"
    ok, err = _verify_live_approval(wi.get("approval_ref"), control_dir)
    if not ok:
        return False, err
    return True, ""


def _guard_claim_freshness(wi: dict[str, Any], claim_token: str | None, now_dt: datetime.datetime) -> tuple[bool, str]:
    if not claim_token:
        return False, "MISSING_OR_EXPIRED_CLAIM"
    claim = wi.get("claim")
    if not claim or claim.get("claim_token") != claim_token:
        return False, "STALE_CLAIM"
    exp_str = claim.get("expires_at")
    if not exp_str:
        return False, "CLAIM_EXPIRED"
    try:
        if datetime.datetime.fromisoformat(exp_str) <= now_dt:
            return False, "CLAIM_EXPIRED"
    except (ValueError, TypeError):
        return False, "CLAIM_EXPIRED"
    return True, ""


def _check_receipt_file_on_disk(
    control_dir: Path | str,
    work_id: str,
    family: str,
    session_id: str,
    commit_sha: str,
    tree_sha: str,
) -> bool:
    rec_path = Path(control_dir) / "receipts" / f"review_{work_id}_{family}_{session_id}.json"
    if not rec_path.exists():
        return False
    try:
        data = json.loads(rec_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    r_commit = data.get("commit_sha") or data.get("revision")
    r_tree = data.get("tree_sha")
    if not _is_hex_40(r_commit) or not _is_hex_40(r_tree):
        return False
    return r_commit == commit_sha and r_tree == tree_sha


def _extract_receipt_commit_tree(r: dict[str, Any]) -> tuple[str, str]:
    commit = str(r.get("commit_sha") or r.get("revision") or "").strip()
    tree = str(r.get("tree_sha") or "").strip()
    return commit, tree


def _extract_receipt_family_session(r: dict[str, Any]) -> tuple[str, str]:
    fam = str(r.get("family") or r.get("reviewer_family") or "").strip()
    sess = str(r.get("session_id") or r.get("reviewer_session_id") or "").strip()
    return fam, sess


def _check_single_review_receipt(
    r: dict[str, Any],
    cand_commit: str,
    cand_tree: str,
    control_dir: Path | str | None,
    work_id: str,
) -> bool:
    r_commit, r_tree = _extract_receipt_commit_tree(r)
    if not (_is_hex_40(r_commit) and _is_hex_40(r_tree)):
        return False
    if r_commit != cand_commit or r_tree != cand_tree:
        return False
    if not (control_dir and work_id):
        return True
    fam, sess = _extract_receipt_family_session(r)
    return _check_receipt_file_on_disk(control_dir, work_id, fam, sess, cand_commit, cand_tree)


def _check_ship_reviews_match(
    reviews: list[dict[str, Any]],
    commit_sha: str | None,
    tree_sha: str | None,
    control_dir: Path | str | None = None,
    work_id: str = "",
) -> tuple[bool, str]:
    ship_revs = [r for r in reviews if r.get("verdict") == "SHIP"]
    if len(ship_revs) < 3:
        return False, "MISSING_QUALIFICATION"
    cand_commit = str(commit_sha or "").strip()
    cand_tree = str(tree_sha or "").strip()
    if not _is_hex_40(cand_commit) or not _is_hex_40(cand_tree):
        return False, "STALE_REVIEW_REVISION"
    for r in ship_revs:
        if not _check_single_review_receipt(r, cand_commit, cand_tree, control_dir, work_id):
            return False, "STALE_REVIEW_REVISION"
    return True, ""


def _check_spec_task_prerequisite(wi: dict[str, Any], cand: dict[str, Any]) -> bool:
    cand_commit = cand.get("commit_sha", "")
    cand_tree = cand.get("tree_sha", "")
    cur_transitions = [
        t for t in wi.get("transitions", []) if t.get("commit_sha") == cand_commit and t.get("tree_sha") == cand_tree
    ]
    req_action = wi.get("required_completion_action") or "merge_pr"
    if req_action == "merge_pr":
        allowed = {"merge_pr"}
    elif req_action == "publish_pr":
        allowed = {"publish_pr", "merge_pr"}
    elif req_action == "push":
        allowed = {"push", "publish_pr", "merge_pr"}
    else:
        allowed = {"merge_pr"}
    return any(t.get("action") in allowed for t in cur_transitions)


def _guard_candidate_reviews_and_gates(
    wi: dict[str, Any], cand: dict[str, Any], control_dir: Path | str | None
) -> tuple[bool, str]:
    cand_commit = wi.get("commit_sha") or cand.get("commit_sha")
    cand_tree = wi.get("tree_sha") or cand.get("tree_sha")
    ok, err = _check_ship_reviews_match(
        wi.get("reviews", []),
        cand_commit,
        cand_tree,
        control_dir=control_dir,
        work_id=str(wi.get("work_id") or ""),
    )
    if not ok:
        return False, err
    if not _verify_mandatory_gates(cand.get("gate_results")):
        return False, "MISSING_MANDATORY_GATE"
    return True, ""


def _guard_pre_transition(
    wi: dict[str, Any],
    action: str,
    claim_token: str | None,
    now_dt: datetime.datetime,
    control_dir: Path | str | None = None,
) -> tuple[bool, str]:
    ok, err = _guard_approval_and_defects(wi, control_dir)
    if not ok:
        return False, err
    ok, err = _guard_claim_freshness(wi, claim_token, now_dt)
    if not ok:
        return False, err
    if wi.get("qualified") is not True:
        return False, "MISSING_QUALIFICATION"
    cand = wi.get("candidate", {})
    ok, err = _guard_candidate_reviews_and_gates(wi, cand, control_dir)
    if not ok:
        return False, err
    if action == "complete_spec_task" and not _check_spec_task_prerequisite(wi, cand):
        return False, "MISSING_TRANSITION_EVIDENCE"
    return True, ""


def _check_live_git_revs(
    wt_path: Path, cand: dict[str, Any], runner: Callable[..., Any] | None
) -> tuple[bool, str, str]:
    code, head = _run_cmd(["git", "rev-parse", "HEAD"], wt_path, runner)
    if code != 0 or head != cand.get("commit_sha"):
        return False, "", ""
    code, tree = _run_cmd(["git", "rev-parse", "HEAD^{tree}"], wt_path, runner)
    if code != 0 or tree != cand.get("tree_sha"):
        return False, "", ""
    return True, head, tree


def _guard_live_git(
    wt_path: Path,
    cand: dict[str, Any],
    reviews: list[dict[str, Any]],
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    ok, head, tree = _check_live_git_revs(wt_path, cand, runner)
    if not ok:
        return False, "HEAD_CHANGED"
    ok, err = _check_ship_reviews_match(reviews, head, tree)
    if not ok:
        return False, err
    return True, ""


CI_SHA_KEYS = ("head_sha", "commit_sha", "sha", "headSha", "revision")


def _extract_check_sha(c: dict[str, Any]) -> str:
    for k in CI_SHA_KEYS:
        v = c.get(k)
        if v:
            return str(v).strip()
    return ""


def _is_ci_entry_successful(st: str, concl: str, state: str) -> bool:
    if state == "SUCCESS":
        return True
    if concl == "SUCCESS" and st in {"COMPLETED", "SUCCESS", ""}:
        return True
    return st == "SUCCESS" and concl in {"SUCCESS", ""}


def _check_single_ci(c: Any, commit_sha: str, require_sha: bool = False) -> bool:
    if not isinstance(c, dict):
        return False
    st = str(c.get("status", "")).upper()
    concl = str(c.get("conclusion", "")).upper()
    state = str(c.get("state", "")).upper()
    if not _is_ci_entry_successful(st, concl, state):
        return False
    sha_val = _extract_check_sha(c)
    if require_sha and not sha_val:
        return False
    if sha_val and sha_val != commit_sha:
        return False
    return True


def _verify_ci_checks_exact_head(checks: list[dict[str, Any]], commit_sha: str, require_sha: bool = False) -> bool:
    if not checks or not isinstance(checks, list):
        return False
    return all(_check_single_ci(c, commit_sha, require_sha=require_sha) for c in checks)


def _get_remote_branch_sha(
    worktree: Path | str,
    remote: str,
    branch: str,
    runner: Callable[..., Any] | None = None,
) -> str | None:
    target_ref = f"refs/heads/{branch}"
    code, out = _run_cmd(["git", "ls-remote", "--heads", remote, target_ref], Path(worktree), runner)
    if code != 0 or not out.strip():
        return None
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == target_ref:
            return parts[0]
    return None


def _reconcile_and_push(
    worktree: Path,
    remote: str = "origin",
    branch: str = "",
    base_branch: str = "main",
    commit_sha: str = "",
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    if branch in {"main", "master", base_branch}:
        return False, {"error": f"Cannot push to protected branch '{branch}'", "reason_code": "PROTECTED_BRANCH_PUSH"}
    remote_sha = _get_remote_branch_sha(worktree, remote, branch, runner)
    if remote_sha == commit_sha:
        return True, {"reconciled_existing_effect": True, "remote_sha": commit_sha}
    if remote_sha:
        cmd = [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{branch}:{remote_sha}",
            remote,
            f"{commit_sha}:refs/heads/{branch}",
        ]
    else:
        cmd = ["git", "push", remote, f"{commit_sha}:refs/heads/{branch}"]
    p_code, push_out = _run_cmd(cmd, worktree, runner)
    if p_code != 0:
        return False, {"error": push_out}
    return True, {"reconciled_existing_effect": False, "push_output": push_out}


def _is_pr_already_merged(info: dict[str, Any]) -> tuple[bool, str]:
    if info.get("state") != "MERGED":
        return False, ""
    merged_at = info.get("mergedAt")
    if not isinstance(merged_at, str) or not merged_at.strip():
        return False, ""
    mc = info.get("mergeCommit")
    mc_oid = mc.get("oid") if isinstance(mc, dict) else (mc if isinstance(mc, str) else "")
    if not _is_hex_40(mc_oid):
        return False, ""
    return True, str(mc_oid)


def _evaluate_existing_pr_data(info: dict[str, Any], commit_sha: str) -> tuple[bool, Any]:
    state = info.get("state")
    if state == "CLOSED":
        return False, "PR_NOT_OPEN"
    if state == "OPEN":
        if info.get("headRefOid") != commit_sha:
            return False, None
        return True, {
            "reconciled_existing_effect": True,
            "pr_number": info.get("number"),
            "pr_url": info.get("url"),
            "state": "OPEN",
        }
    if state == "MERGED":
        merged, oid = _is_pr_already_merged(info)
        if merged and info.get("headRefOid") == commit_sha:
            return True, {
                "reconciled_existing_effect": True,
                "pr_number": info.get("number"),
                "pr_url": info.get("url"),
                "state": "MERGED",
                "merge_commit_sha": oid,
            }
        return False, "PR_NOT_OPEN"
    return False, "PR_NOT_OPEN"


def _check_existing_pr(
    target: Path | str,
    branch_or_sha: str,
    commit_sha: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, Any]:
    if commit_sha is not None:
        cmd = ["gh", "pr", "view", branch_or_sha, "--json", "number,url,state,headRefOid"]
        code, out = _run_cmd(cmd, Path(target), runner)
        if code != 0 or not out.strip():
            return False, None
        raw_out = out
        expected_sha = commit_sha
    else:
        raw_out = str(target)
        expected_sha = branch_or_sha

    try:
        info = json.loads(raw_out)
        if isinstance(info, dict):
            return _evaluate_existing_pr_data(info, expected_sha)
    except (json.JSONDecodeError, TypeError):
        pass
    return False, None


def _parse_pr_view(out: str) -> dict[str, Any] | None:
    try:
        data = json.loads(out)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _verify_created_pr(v_code: int, v_out: str, commit_sha: str) -> tuple[bool, dict[str, Any]]:
    if v_code != 0:
        return False, {"error": f"Failed to query created PR: {v_out}", "reason_code": "CREATED_PR_VIEW_FAILED"}
    pr_data = _parse_pr_view(v_out)
    if not pr_data:
        return False, {"error": "Invalid JSON from gh pr view", "reason_code": "PR_HEAD_MISMATCH"}
    if pr_data.get("state") != "OPEN" or pr_data.get("headRefOid") != commit_sha:
        return False, {
            "error": f"Created PR state '{pr_data.get('state')}' or headRefOid '{pr_data.get('headRefOid')}' mismatch",
            "reason_code": "PR_HEAD_MISMATCH",
        }
    return True, pr_data


def _reconcile_and_publish_pr(
    worktree: Path,
    branch: str,
    base_branch: str,
    commit_sha: str,
    title: str,
    body: str,
    runner: Callable[..., Any] | None = None,
    remote: str = "origin",
) -> tuple[bool, dict[str, Any]]:
    matched, pr_info = _check_existing_pr(worktree, branch, commit_sha, runner=runner)
    if matched and isinstance(pr_info, dict):
        return True, pr_info
    if pr_info == "PR_NOT_OPEN":
        return False, {
            "error": f"Existing PR for branch '{branch}' is not open",
            "reason_code": "PR_NOT_OPEN",
        }

    remote_sha = _get_remote_branch_sha(worktree, remote, branch, runner)
    if not remote_sha or remote_sha != commit_sha:
        return False, {
            "error": f"Remote branch '{branch}' head '{remote_sha}' does not match commit '{commit_sha}'",
            "reason_code": "REMOTE_HEAD_MISMATCH",
        }

    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--head",
        branch,
        "--base",
        base_branch,
    ]
    code, pr_out = _run_cmd(cmd, worktree, runner)
    if code != 0:
        return False, {"error": pr_out}

    v_code, v_out = _run_cmd(
        ["gh", "pr", "view", branch, "--json", "number,url,state,headRefOid"],
        worktree,
        runner,
    )
    v_ok, v_res = _verify_created_pr(v_code, v_out, commit_sha)
    if not v_ok:
        return False, v_res
    return True, {
        "reconciled_existing_effect": False,
        "pr_number": v_res.get("number"),
        "pr_url": v_res.get("url"),
        "output": pr_out,
    }


def _is_known_bot_login(login: str) -> bool:
    if not login:
        return False
    return login in ALLOWED_MERGE_IDENTITIES or login.endswith("[bot]")


def _is_provider_bot_login_allowed(login: str, acc_type: str, merge_identity: str) -> bool:
    if not _is_known_bot_login(login):
        return False
    req = (merge_identity or "").strip()
    if not req:
        return True
    if login == req or login == f"{req}[bot]":
        return True
    return False


def _verify_provider_bot_actor(
    worktree: Path, merge_identity: str, runner: Callable[..., Any] | None = None
) -> tuple[bool, str]:
    code, out = _run_cmd(["gh", "api", "user"], worktree, runner)
    if code != 0:
        return False, "DISALLOWED_MERGE_IDENTITY"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False, "DISALLOWED_MERGE_IDENTITY"
    if not isinstance(data, dict):
        return False, "DISALLOWED_MERGE_IDENTITY"
    login = str(data.get("login") or "").strip()
    acc_type = str(data.get("type") or "").strip()
    if not _is_provider_bot_login_allowed(login, acc_type, merge_identity):
        return False, "DISALLOWED_MERGE_IDENTITY"
    return True, ""


_verify_merge_actor_identity = _verify_provider_bot_actor


def _check_merge_ci_requirements(
    info: dict[str, Any], commit_sha: str, ci_checks: list[dict[str, Any]] | None
) -> tuple[bool, str]:
    prov_checks = info.get("statusCheckRollup")
    if not isinstance(prov_checks, list) or not prov_checks:
        return False, "MISSING_EXACT_HEAD_CI"
    if not _verify_ci_checks_exact_head(prov_checks, commit_sha, require_sha=False):
        return False, "MISSING_EXACT_HEAD_CI"
    if ci_checks is not None:
        if not ci_checks or not _verify_ci_checks_exact_head(ci_checks, commit_sha, require_sha=True):
            return False, "MISSING_EXACT_HEAD_CI"
    return True, ""


def _stamp_rollup_head_sha(rollup: Any, commit_sha: str) -> None:
    if not isinstance(rollup, list) or not _is_valid_sha(commit_sha):
        return
    for item in rollup:
        if isinstance(item, dict):
            c_sha = (
                item.get("head_sha")
                or item.get("commit_sha")
                or item.get("sha")
                or item.get("headSha")
                or item.get("revision")
            )
            if not c_sha:
                item["head_sha"] = commit_sha


def _build_pr_view_cmd(repo: str | Path | None, pr_number: str | int, cwd: Path | str | None) -> tuple[list[str], Path]:
    repo_name = str(repo).strip() if isinstance(repo, str) else ""
    worktree = Path(cwd or (repo if isinstance(repo, Path) else "."))
    cmd = ["gh", "pr", "view", str(pr_number)]
    if repo_name:
        cmd.extend(["--repo", repo_name])
    cmd.extend(["--json", "state,mergedAt,mergeCommit,headRefOid"])
    return cmd, worktree


def _post_merge_verify(
    repo: str | Path | None,
    pr_number: str | int,
    cand_sha: str,
    runner: Callable[..., Any] | None = None,
    cwd: Path | str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    cmd, worktree = _build_pr_view_cmd(repo, pr_number, cwd)
    v_code, v_out = _run_cmd(cmd, worktree, runner)
    if v_code != 0:
        return False, "MERGE_NOT_COMPLETED", {"error": f"Failed to verify merge state: {v_out}"}
    post_info = _parse_pr_view(v_out) or {}
    merged_post, post_oid = _is_pr_already_merged(post_info)
    if not merged_post:
        st = post_info.get("state")
        ma = post_info.get("mergedAt")
        return (
            False,
            "MERGE_NOT_COMPLETED",
            {
                "error": f"PR {pr_number} was queued or not merged (state={st}, mergedAt={ma})",
                "post_info": post_info,
            },
        )
    post_head = str(post_info.get("headRefOid") or "").strip()
    if not post_head or post_head != cand_sha:
        return (
            False,
            "CANDIDATE_REVISION_MISMATCH",
            {
                "error": f"Post-merge PR headRefOid {post_head} != {cand_sha}",
                "reason_code": "CANDIDATE_REVISION_MISMATCH",
                "post_info": post_info,
            },
        )
    return True, post_oid, post_info


def _reconcile_and_merge_pr(
    worktree: Path,
    pr_ref: str,
    commit_sha: str,
    merge_identity: str = "selamy-labs-bot",
    ci_checks: list[dict[str, Any]] | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    id_ok, id_err = _verify_provider_bot_actor(worktree, merge_identity, runner)
    if not id_ok:
        return False, {"error": "Provider authenticated user disallowed", "reason_code": id_err}
    code, out = _run_cmd(
        ["gh", "pr", "view", pr_ref, "--json", "state,mergeCommit,headRefOid,statusCheckRollup,mergedAt"],
        worktree,
        runner,
    )
    if code != 0:
        return False, {"error": out}
    info = _parse_pr_view(out)
    if not info:
        return False, {"error": "Failed to parse gh pr view output"}

    head_ref = str(info.get("headRefOid") or "").strip()
    if head_ref != commit_sha:
        return False, {"error": f"PR head {head_ref} != {commit_sha}", "reason_code": "PR_HEAD_MISMATCH"}

    _stamp_rollup_head_sha(info.get("statusCheckRollup"), commit_sha)
    ci_ok, ci_err = _check_merge_ci_requirements(info, commit_sha, ci_checks)
    if not ci_ok:
        return False, {"error": "CI checks missing or failed", "reason_code": ci_err}

    merged, mc_oid = _is_pr_already_merged(info)
    if merged:
        return True, {
            "reconciled_existing_effect": True,
            "state": "MERGED",
            "merge_commit": mc_oid,
            "merge_commit_sha": mc_oid,
            "merged_at": info.get("mergedAt"),
        }

    merge_cmd = ["gh", "pr", "merge", pr_ref, "--squash", "--match-head-commit", commit_sha]
    m_code, merge_out = _run_cmd(merge_cmd, worktree, runner)
    if m_code != 0:
        return False, {"error": merge_out, "reason_code": "MERGE_FAILED"}

    post_ok, post_err_code, post_data = _post_merge_verify(worktree, pr_ref, commit_sha, runner)
    if not post_ok:
        return False, {"error": post_data.get("error", "Merge was not completed"), "reason_code": post_err_code}

    post_oid = post_err_code
    post_info = post_data
    return True, {
        "reconciled_existing_effect": False,
        "state": "MERGED",
        "merge_commit": post_oid,
        "merge_commit_sha": post_oid,
        "merged_at": post_info.get("mergedAt"),
        "output": merge_out,
    }


def _reconcile_and_complete_spec_task(spec_path: Path | str | None, task_id: str) -> tuple[bool, dict[str, Any]]:
    if not spec_path:
        return False, {"error": "Missing spec_path for complete_spec_task"}
    p = Path(spec_path)
    if not p.exists():
        return False, {"error": f"Spec path {spec_path} does not exist"}
    content = p.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\s*-\s*\[)([ xX])(\]\s+{re.escape(task_id)})(?=\s|:|$)(.*)$", re.MULTILINE)
    matches = list(pattern.finditer(content))
    if not matches:
        return False, {"error": f"Task pattern for '{task_id}' not found in {spec_path}"}
    if any(m.group(2).lower() == "x" for m in matches):
        return True, {"reconciled_existing_effect": True, "task_id": task_id}

    def _repl(m: re.Match) -> str:
        return f"{m.group(1)}x{m.group(3)}{m.group(4)}"

    new_content = pattern.sub(_repl, content, count=1)
    p.write_text(new_content, encoding="utf-8")
    return True, {"reconciled_existing_effect": False, "task_id": task_id}


def _dispatch_action(
    action: str,
    wt_path: Path,
    branch: str,
    base_branch: str,
    commit_sha: str,
    pr_ref: str,
    pr_title: str,
    pr_body: str,
    spec_path: str | None,
    task_id: str,
    ci_checks: list[dict[str, Any]] | None,
    runner: Callable[..., Any] | None,
    merge_identity: str = "selamy-labs-bot",
    remote: str = "origin",
) -> tuple[bool, dict[str, Any]]:
    if action == "push":
        return _reconcile_and_push(wt_path, remote, branch, base_branch, commit_sha, runner)
    if action == "publish_pr":
        return _reconcile_and_publish_pr(
            wt_path, branch, base_branch, commit_sha, pr_title, pr_body, runner, remote=remote
        )
    if action == "merge_pr":
        return _reconcile_and_merge_pr(wt_path, pr_ref, commit_sha, merge_identity, ci_checks, runner)
    if action == "complete_spec_task":
        return _reconcile_and_complete_spec_task(spec_path, task_id)
    return False, {"error": f"Unknown action {action}"}


def _verify_transition_guards(
    control_dir: Path | str,
    doc: dict[str, Any],
    work_id: str,
    action: str,
    claim_token: str | None,
    now_dt: datetime.datetime,
    command_runner: Callable[..., Any] | None,
    iso_now: str,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    dc = doc["delivery_control"]
    if work_id not in dc["work_items"]:
        ref = make_refusal(control_dir, work_id, "WORK_NOT_ADMITTED", "Work ID not admitted", now=iso_now)
        return False, ref, None
    wi = dc["work_items"][work_id]
    guard_ok, guard_err = _guard_pre_transition(wi, action, claim_token, now_dt, control_dir=control_dir)
    if not guard_ok:
        ref = make_refusal(control_dir, work_id, guard_err, f"Guard failed: {guard_err}", now=iso_now)
        return False, ref, None
    cand = wi.get("candidate", {})
    wt_path = Path(wi.get("claim", {}).get("worktree", "."))
    live_ok, live_err = _guard_live_git(wt_path, cand, wi.get("reviews", []), command_runner)
    if not live_ok:
        ref = make_refusal(control_dir, work_id, live_err, f"Live git drifted: {live_err}", now=iso_now)
        return False, ref, None
    repo = wi.get("repository", "")
    protect = wi.get("protect_skill_path", False)
    allowed = wi.get("claim", {}).get("allowed_rel_paths") or wi.get("claim", {}).get("write_paths")
    diff_ok, diff_err = _inspect_modified_paths(
        wt_path, wi.get("base_sha", ""), cand.get("commit_sha", ""), repo, protect, allowed, command_runner
    )
    if not diff_ok:
        ref = make_refusal(control_dir, work_id, diff_err, f"Path inspection failed: {diff_err}", now=iso_now)
        return False, ref, None
    return True, None, wi


def _record_merge_pr_outcome(p_row: dict[str, Any], commit_sha: str, receipt_str: str, work_id: str) -> None:
    p_row["ci"] = {"revision": commit_sha, "status": "success", "evidence": [receipt_str]}
    if p_row.get("branch_protection", {}).get("verified") is True:
        bp_ev = p_row["branch_protection"].setdefault("evidence", [])
        if receipt_str not in bp_ev:
            bp_ev.append(receipt_str)
    p_row["next_action"] = f"Complete spec task for {work_id}"


def _record_transition_outcome(
    doc: dict[str, Any],
    wi: dict[str, Any],
    action: str,
    commit_sha: str,
    receipt_str: str,
    iso_now: str,
) -> None:
    cand = wi.get("candidate", {})
    tree_sha = cand.get("tree_sha", "")
    trans_entry = {
        "action": action,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "evidence": receipt_str,
        "observed_at": iso_now,
    }
    wi.setdefault("transitions", []).append(trans_entry)
    project = wi.get("project", "default")
    p_row = doc["delivery_control"]["projects"].setdefault(project, {})
    p_row["last_progress_at"] = iso_now
    p_row["observed_at"] = iso_now
    wid = str(wi.get("work_id", ""))
    if action == "push":
        p_row["next_action"] = f"Create or merge PR for {wid}"
    elif action == "publish_pr":
        p_row["next_action"] = f"Merge PR for {wid}"
    elif action == "merge_pr":
        _record_merge_pr_outcome(p_row, commit_sha, receipt_str, wid)
    elif action == "complete_spec_task":
        wi["stage"] = "done"
        p_row["stage"] = "done"
        p_row["next_action"] = "Work item complete"
        if "work_item" in p_row and isinstance(p_row["work_item"], dict):
            p_row["work_item"]["stage"] = "done"


_update_ledger_for_action = _record_transition_outcome


def _build_action_params(
    work_id: str,
    branch: str | None,
    pr_number: int | str | None,
    pr_title: str | None,
    pr_body: str | None,
) -> tuple[str, str, str, str]:
    target_branch = branch if branch else f"feat/{work_id}"
    pr_ref = str(pr_number) if pr_number else target_branch
    title = pr_title if pr_title else f"feat: implement {work_id}"
    body = pr_body if pr_body else f"Automated PR for {work_id}"
    return target_branch, pr_ref, title, body


DISALLOWED_CI_STATUSES = {
    "FAILURE",
    "FAILED",
    "ERROR",
    "CANCELLED",
    "SKIPPED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "STALE",
    "IN_PROGRESS",
    "QUEUED",
    "PENDING",
    "MISSING",
}
ALLOWED_CI_CONCLUSIONS = {"SUCCESS", "PASS", "PASSED"}
ALLOWED_CI_STATUSES = {"COMPLETED", "SUCCESS", "PASS", "PASSED", ""}


def _extract_ci_conclusion_and_status(c: dict[str, Any]) -> tuple[str, str]:
    raw_concl = c.get("conclusion")
    concl = str(raw_concl).strip().upper() if raw_concl else ""
    raw_st = c.get("status") if c.get("status") is not None else c.get("state")
    st = str(raw_st).strip().upper() if raw_st else ""
    return concl, st


def _is_ci_check_passing(c: Any, cand_sha: str) -> bool:
    if not isinstance(c, dict):
        return False
    c_sha = _extract_check_sha(c)
    if not _is_valid_sha(c_sha) or c_sha != cand_sha:
        return False
    concl, st = _extract_ci_conclusion_and_status(c)
    if concl in DISALLOWED_CI_STATUSES or st in DISALLOWED_CI_STATUSES:
        return False
    concl_ok = (concl in ALLOWED_CI_CONCLUSIONS) if concl else (st in ALLOWED_CI_CONCLUSIONS)
    return concl_ok and (st in ALLOWED_CI_STATUSES)


def _extract_caller_ci_checks(ci_checks: Any) -> tuple[bool | None, list[dict[str, Any]] | None]:
    if isinstance(ci_checks, list):
        return None, ci_checks
    if not isinstance(ci_checks, dict):
        return None, None
    st = ci_checks.get("status")
    if st and str(st).lower() not in {"passing", "success"}:
        return False, []
    for k in ("checks", "ci_checks", "statusCheckRollup"):
        val = ci_checks.get(k)
        if isinstance(val, list):
            return None, val
    return None, None


def _check_project_row_ci(p_row: dict[str, Any], commit_sha: str) -> tuple[bool | None, list[dict[str, Any]] | None]:
    p_ci = p_row.get("ci")
    if not isinstance(p_ci, dict) or p_ci.get("revision") != commit_sha:
        return None, None
    ci_st = str(p_ci.get("status", "")).lower()
    if ci_st in {"failing", "failed", "failure"}:
        return False, []
    if ci_st in {"passing", "success"}:
        succ = [{"name": "provider_ci", "status": "COMPLETED", "conclusion": "SUCCESS", "head_sha": commit_sha}]
        return True, succ
    return None, None


def _fetch_ci_checks_from_provider(
    repo: str,
    pr_num: str | int,
    cand_sha: str,
    runner: Callable[..., Any] | None = None,
    cwd: Path | str | None = None,
) -> list[dict[str, Any]] | None:
    if not _is_valid_sha(cand_sha):
        return None
    pr_ref = str(pr_num)
    cmd = ["gh", "pr", "view", pr_ref]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(["--json", "headRefOid,statusCheckRollup"])
    work_dir = Path(cwd) if cwd else Path(".")
    code, out = _run_cmd(cmd, work_dir, runner)
    if code != 0:
        return None
    info = _parse_pr_view(out)
    if not info or str(info.get("headRefOid", "")).strip() != cand_sha:
        return None
    rollup = info.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return None
    checks = [dict(item) for item in rollup if isinstance(item, dict)]
    _stamp_rollup_head_sha(checks, cand_sha)
    return checks


def _query_live_pr_checks(
    command_runner: Callable[..., Any] | None, wt_path: Path, pr_ref: str, commit_sha: str
) -> list[dict[str, Any]] | None:
    return _fetch_ci_checks_from_provider("", pr_ref, commit_sha, runner=command_runner, cwd=wt_path)


def _resolve_spec_task_checks(
    ci_checks: Any,
    p_row: dict[str, Any],
    commit_sha: str,
    command_runner: Callable[..., Any] | None,
    wt_path: Path,
    pr_ref: str,
) -> tuple[bool | None, list[dict[str, Any]] | None]:
    res, checks = _extract_caller_ci_checks(ci_checks)
    if res is not None or checks is not None:
        return res, checks
    res, checks = _check_project_row_ci(p_row, commit_sha)
    if res is True and checks is not None:
        return res, checks
    live_checks = _query_live_pr_checks(command_runner, wt_path, pr_ref, commit_sha)
    if live_checks is not None:
        return None, live_checks
    if res is False:
        return False, checks
    return None, None


def _guard_spec_task_ci(
    doc: dict[str, Any],
    wi: dict[str, Any],
    commit_sha: str,
    ci_checks: Any,
    command_runner: Callable[..., Any] | None,
    wt_path: Path,
    pr_ref: str,
) -> tuple[bool, str, list[dict[str, Any]]]:
    project = wi.get("project", "default")
    p_row = doc["delivery_control"]["projects"].setdefault(project, {})
    early_result, checks = _resolve_spec_task_checks(ci_checks, p_row, commit_sha, command_runner, wt_path, pr_ref)
    if early_result is False:
        return False, "provider CI status is failing", checks or []
    if early_result is True and checks is not None:
        return True, "", checks
    if not checks:
        return False, "required checks are missing", []
    if not all(_is_ci_check_passing(c, commit_sha) for c in checks):
        return False, "exact-head provider required checks failed", checks
    return True, "", checks


def _check_merge_pr_ci_failure(
    p_row: dict[str, Any],
    commit_sha: str,
    ci_checks: list[dict[str, Any]] | None,
    command_runner: Callable[..., Any] | None,
    wt_path: Path,
    pr_ref: str,
) -> tuple[bool, list[dict[str, Any]]]:
    if ci_checks is not None:
        return False, []
    p_ci = p_row.get("ci")
    if not (isinstance(p_ci, dict) and p_ci.get("revision") == commit_sha):
        return False, []
    if str(p_ci.get("status", "")).lower() not in {"failing", "failed", "failure"}:
        return False, []
    live = _query_live_pr_checks(command_runner, wt_path, pr_ref, commit_sha)
    if live is not None and all(_is_ci_check_passing(c, commit_sha) for c in live):
        return False, []
    return True, p_ci.get("checks", [])


def _refuse_spec_task_ci_failure(
    control_dir: Path | str,
    doc: dict[str, Any],
    wi: dict[str, Any],
    work_id: str,
    commit_sha: str,
    ci_err: str,
    checks: list[dict[str, Any]],
    cand: dict[str, Any],
    iso_now: str,
) -> dict[str, Any]:
    project = wi.get("project", "default")
    p_row = doc["delivery_control"]["projects"].setdefault(project, {})
    p_row["stage"] = "review"
    wi["stage"] = "merge_ready"
    save_coordinator_status(control_dir, doc)
    task_id = str(wi.get("task_id") or "")
    msg = (
        f"Refused complete_spec_task for {task_id}: exact-head provider required "
        f"checks failed on {commit_sha} ({ci_err})."
    )
    extra = {
        "action": "complete_spec_task",
        "code": "CI_CHECK_FAILED",
        "commit_sha": commit_sha,
        "tree_sha": cand.get("tree_sha", ""),
        "head": commit_sha,
        "revision": commit_sha,
        "task_id": task_id,
        "provider_checks": checks,
        "message": msg,
    }
    return make_refusal(control_dir, work_id, "CI_CHECK_FAILED", msg, extra=extra, now=iso_now)


def _refuse_merge_pr_ci_failure(
    control_dir: Path | str,
    work_id: str,
    commit_sha: str,
    cand: dict[str, Any],
    task_id: str,
    fail_checks: list[dict[str, Any]],
    iso_now: str,
) -> dict[str, Any]:
    extra = {
        "action": "merge_pr",
        "code": "CI_CHECK_FAILED",
        "commit_sha": commit_sha,
        "tree_sha": cand.get("tree_sha", ""),
        "head": commit_sha,
        "revision": commit_sha,
        "task_id": task_id,
        "provider_checks": fail_checks,
        "message": f"Refused merge_pr for {work_id}: exact-head provider CI status is failing.",
    }
    return make_refusal(control_dir, work_id, "CI_CHECK_FAILED", extra["message"], extra=extra, now=iso_now)


def _perform_transition_execution(
    control_dir: Path | str,
    doc: dict[str, Any],
    wi: dict[str, Any],
    work_id: str,
    action: str,
    params: tuple[str, str, str, str],
    base_branch: str,
    ci_checks: list[dict[str, Any]] | None,
    command_runner: Callable[..., Any] | None,
    iso_now: str,
    merge_identity: str = "selamy-labs-bot",
    remote: str = "origin",
) -> dict[str, Any]:
    cand = wi.get("candidate", {})
    wt_path = Path(wi.get("claim", {}).get("worktree", "."))
    target_branch, pr_ref, title, body = params
    commit_sha = cand.get("commit_sha", "")

    if action == "complete_spec_task":
        ci_ok, ci_err, checks = _guard_spec_task_ci(doc, wi, commit_sha, ci_checks, command_runner, wt_path, pr_ref)
        if not ci_ok:
            return _refuse_spec_task_ci_failure(
                control_dir, doc, wi, work_id, commit_sha, ci_err, checks, cand, iso_now
            )

    if action == "merge_pr":
        project = wi.get("project", "default")
        p_row = doc["delivery_control"]["projects"].setdefault(project, {})
        fail_ci, fail_checks = _check_merge_pr_ci_failure(p_row, commit_sha, ci_checks, command_runner, wt_path, pr_ref)
        if fail_ci:
            return _refuse_merge_pr_ci_failure(
                control_dir, work_id, commit_sha, cand, str(wi.get("task_id") or ""), fail_checks, iso_now
            )

    act_ok, act_res = _dispatch_action(
        action,
        wt_path,
        target_branch,
        base_branch,
        commit_sha,
        pr_ref,
        title,
        body,
        wi.get("spec_path"),
        wi.get("task_id", ""),
        ci_checks,
        command_runner,
        merge_identity=merge_identity,
        remote=remote,
    )
    if not act_ok:
        reason = act_res.get("reason_code", "PROVIDER_EXECUTION_FAILED")
        return make_refusal(
            control_dir,
            work_id,
            reason,
            act_res.get("error", "Execution failed"),
            now=iso_now,
        )
    receipt: dict[str, Any] = {
        "status": "transitioned",
        "action": action,
        "work_id": work_id,
        "commit_sha": commit_sha,
        "reconciled_existing_effect": act_res.get("reconciled_existing_effect", False),
        "provider_result": act_res,
        "observed_at": iso_now,
    }
    if "merge_commit_sha" in act_res:
        receipt["merge_commit_sha"] = act_res["merge_commit_sha"]
    rec_path = write_receipt(control_dir, f"transition_{work_id}_{action}.json", receipt)
    receipt["receipt_path"] = str(rec_path)
    _record_transition_outcome(doc, wi, action, commit_sha, str(rec_path), iso_now)
    save_coordinator_status(control_dir, doc)
    return receipt


def _validate_transition_args(
    control_dir: Path | str,
    work_id: str,
    action: str,
    branch: str | None,
    base_branch: str,
    merge_identity: str,
    iso_now: str,
) -> dict[str, Any] | None:
    if action not in VALID_ACTIONS:
        return make_refusal(control_dir, work_id, "INVALID_ACTION", f"Action '{action}' unsupported", now=iso_now)
    target_branch = branch if branch else f"feat/{work_id}"
    if action == "push" and target_branch in {"main", "master", base_branch}:
        return make_refusal(
            control_dir,
            work_id,
            "PROTECTED_BRANCH_PUSH",
            f"Cannot push to protected branch '{target_branch}'",
            now=iso_now,
        )
    if action == "merge_pr" and merge_identity not in ALLOWED_MERGE_IDENTITIES:
        return make_refusal(
            control_dir,
            work_id,
            "DISALLOWED_MERGE_IDENTITY",
            f"Merge identity '{merge_identity}' not in allowed bot identities",
            now=iso_now,
        )
    return None


def execute_transition(
    control_dir: Path | str,
    work_id: str,
    action: str,
    claim_token: str | None = None,
    remote: str = "origin",
    branch: str | None = None,
    base_branch: str = "main",
    pr_number: int | str | None = None,
    pr_title: str | None = None,
    pr_body: str | None = None,
    merge_identity: str = "selamy-labs-bot",
    ci_checks: list[dict[str, Any]] | None = None,
    command_runner: Callable[..., Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    iso_now = now or iso_timestamp()
    now_dt = datetime.datetime.fromisoformat(iso_now)
    arg_ref = _validate_transition_args(control_dir, work_id, action, branch, base_branch, merge_identity, iso_now)
    if arg_ref is not None:
        return arg_ref

    with workflow_lock(control_dir):
        doc = load_coordinator_status(control_dir)
        ok, ref, wi = _verify_transition_guards(
            control_dir, doc, work_id, action, claim_token, now_dt, command_runner, iso_now
        )
        if not ok or wi is None:
            return ref or {}

        params = _build_action_params(work_id, branch, pr_number, pr_title, pr_body)
        return _perform_transition_execution(
            control_dir,
            doc,
            wi,
            work_id,
            action,
            params,
            base_branch,
            ci_checks,
            command_runner,
            iso_now,
            merge_identity=merge_identity,
            remote=remote,
        )


# -----------------------------------------------------------------------------
# CLI Entrypoint
# -----------------------------------------------------------------------------


def _add_admit_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("admit", help="Admit work from issue/spec")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--repository", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--spec-path")
    p.add_argument("--base-sha", required=True)
    p.add_argument("--approval-ref", required=True)
    p.add_argument("--approval-sha256")
    p.add_argument(
        "--required-completion-action",
        default="merge_pr",
        choices=["push", "publish_pr", "merge_pr"],
    )
    p.add_argument("--prerequisites", nargs="*", default=[])
    p.add_argument("--verify-spec", action="store_true", default=True)


def _add_claim_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("claim", help="Atomically claim work")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--write-paths", nargs="+", required=True)
    p.add_argument("--ttl-seconds", type=int, default=3600)
    p.add_argument("--model", default="gemini-3.8-flash-high")
    p.add_argument("--session-id")


def _add_impl_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("record-implementation", help="Record implementation result")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--claim-token", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--commit-sha", required=True)
    p.add_argument("--tree-sha", required=True)
    p.add_argument("--parent-sha", required=True)
    p.add_argument("--patch-path")
    p.add_argument("--patch-sha256")
    p.add_argument("--bundle-path")
    p.add_argument("--bundle-sha256")
    p.add_argument("--receipt-path")
    p.add_argument("--receipt-sha256")
    p.add_argument("--gate-results", default='{"test_exit_code": 0}')


def _add_review_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("record-review", help="Record reviewer report")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--reviewer-id", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--family", required=True)
    p.add_argument("--session-id", required=True)
    p.add_argument("--verdict", required=True)
    p.add_argument("--report-path", required=True)
    p.add_argument("--report-sha256", required=True)


def _add_trans_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("execute-transition", help="Execute guarded transition")
    p.add_argument("--control-dir", required=True)
    p.add_argument("--work-id", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--claim-token")
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch")
    p.add_argument("--base-branch", default="main")
    p.add_argument("--pr-number")
    p.add_argument("--pr-title")
    p.add_argument("--pr-body")
    p.add_argument("--merge-identity", default="selamy-labs-bot")
    p.add_argument("--ci-checks")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded portfolio delivery control workflow manager")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    _add_admit_parser(sub)
    _add_claim_parser(sub)
    _add_impl_parser(sub)
    _add_review_parser(sub)
    _add_trans_parser(sub)
    return parser


def _dispatch_cli(args: argparse.Namespace) -> dict[str, Any]:
    if args.subcommand == "admit":
        return admit_work(
            control_dir=args.control_dir,
            work_id=args.work_id,
            project=args.project,
            repository=args.repository,
            task_id=args.task_id,
            base_sha=args.base_sha,
            approval_ref=args.approval_ref,
            approval_sha256=getattr(args, "approval_sha256", None),
            required_completion_action=getattr(args, "required_completion_action", "merge_pr"),
            spec_path=args.spec_path,
            prerequisites=args.prerequisites,
            verify_spec=args.verify_spec,
        )
    if args.subcommand == "claim":
        return claim_work(
            control_dir=args.control_dir,
            work_id=args.work_id,
            owner=args.owner,
            worktree=args.worktree,
            write_paths=args.write_paths,
            ttl_seconds=args.ttl_seconds,
            model=args.model,
            session_id=args.session_id,
        )
    if args.subcommand == "record-implementation":
        gates = json.loads(args.gate_results)
        return record_implementation(
            control_dir=args.control_dir,
            work_id=args.work_id,
            owner=args.owner,
            claim_token=args.claim_token,
            round=args.round,
            commit_sha=args.commit_sha,
            tree_sha=args.tree_sha,
            parent_sha=args.parent_sha,
            gate_results=gates,
            patch_path=args.patch_path,
            patch_sha256=args.patch_sha256,
            bundle_path=args.bundle_path,
            bundle_sha256=args.bundle_sha256,
            receipt_path=args.receipt_path,
            receipt_sha256=args.receipt_sha256,
        )
    if args.subcommand == "record-review":
        return record_review(
            control_dir=args.control_dir,
            work_id=args.work_id,
            reviewer_id=args.reviewer_id,
            model=args.model,
            family=args.family,
            session_id=args.session_id,
            verdict=args.verdict,
            report_path=args.report_path,
            report_sha256=args.report_sha256,
        )
    if args.subcommand == "execute-transition":
        checks = json.loads(args.ci_checks) if args.ci_checks else None
        return execute_transition(
            control_dir=args.control_dir,
            work_id=args.work_id,
            action=args.action,
            claim_token=args.claim_token,
            remote=args.remote,
            branch=args.branch,
            base_branch=args.base_branch,
            pr_number=args.pr_number,
            pr_title=args.pr_title,
            pr_body=args.pr_body,
            merge_identity=args.merge_identity,
            ci_checks=checks,
        )
    return {"status": "error", "error": f"Unknown subcommand {args.subcommand}"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    res = _dispatch_cli(args)
    print(json.dumps(res, indent=2))
    if res.get("status") == "refused":
        return 2
    if res.get("status") == "error":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
