#!/usr/bin/env python3
"""Read-only structural portfolio checks; this program grants no authority."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

STAGES = {"discovery", "ready", "implementing", "review", "ci", "merge_ready", "waiting", "blocked", "done"}
READY_ACTIVE_STAGES = {"discovery", "ready", "implementing", "review", "ci", "merge_ready"}
ACTIVE_CLAIM_STAGES = {"implementing", "implementation", "review", "reviewing"}
TRANSITION_CLAIM_STAGES = {"implementation", "review"}
VALID_TRANSITIONS = {"admission", "implementation", "review", "merge", "postmerge", "complete"}
TIME_KEYS = ("observedAt", "updatedAt", "updated_at", "observed_at")
COLLECTIONS = ("items", "prioritized_operator_unblock_decisions", "pending_actions")
MAX_INPUT_BYTES = 2 * 1024 * 1024
FORBIDDEN_EVIDENCE = ("todo", "placeholder", "unverified", "forged", "dummy", "none")
DISPATCH_TOKEN_PATTERN = re.compile(r"\[dispatch:(agy-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_.:-]+)\]")
DISPATCH_END_PATTERN = re.compile(r"\[dispatch-end:(agy-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_.:-]+)\]")
STANDALONE_ACCEPTANCE_PATTERN = re.compile(
    r"^\s*\[dispatch-accepted:(agy-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_.:-]+)\]\s*$",
    re.MULTILINE,
)
TERMINAL_STATUSES = {"consumed", "completed", "read_verified", "failed"}


def bounded_bytes(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds size limit")
    return raw


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(UTC)


def evidence(value):
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def is_hex_sha(rev):
    return isinstance(rev, str) and bool(re.fullmatch(r"[0-9a-f]{40}", rev))


def escape_json_pointer(token):
    return str(token).replace("~", "~0").replace("/", "~1")


escape_json_pointer_token = escape_json_pointer


def is_forged_evidence_item(item):
    if not isinstance(item, str):
        return True
    lowered = item.lower()
    trans = str.maketrans("0134578@", "oleastba")
    leeted = lowered.translate(trans)
    return any(forbidden in lowered or forbidden in leeted for forbidden in FORBIDDEN_EVIDENCE)


def bound_value(obj, max_str_len=240, max_list_len=50):
    if isinstance(obj, str):
        if len(obj) > max_str_len:
            return obj[: max_str_len - 3] + "..."
        return obj
    if isinstance(obj, list):
        return [bound_value(v, max_str_len, max_list_len) for v in obj[:max_list_len]]
    if isinstance(obj, dict):
        return {k: bound_value(v, max_str_len, max_list_len) for k, v in obj.items()}
    return obj


def revision(row):
    return next((row[k] for k in ("revision", "reviewed_head_sha", "head_sha", "headCommit") if row.get(k)), None)


def canonical_sha256(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Audit:
    def __init__(self, now):
        self.now = now
        self.diagnostics = []

    def add(
        self,
        code,
        message,
        project=None,
        schema=False,
        next_permissible_action=None,
        control_id=None,
        control_status=None,
    ):
        entry = {
            "code": code,
            "project": project,
            "message": message,
            "severity": "schema_error" if schema else "attention",
        }
        if next_permissible_action:
            entry["next_permissible_action"] = next_permissible_action
        if control_id is not None:
            entry["control_id"] = control_id
        if control_status is not None:
            entry["control_status"] = control_status
        self.diagnostics.append(entry)

    def read(self, path):
        try:
            raw = bounded_bytes(path)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("top level must be an object")
            return value, hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError) as exc:
            self.add("INPUT_INVALID", f"{path.name}: {type(exc).__name__}", schema=True)
            return {}, None

    def time(self, value, label, project=None):
        try:
            parsed = timestamp(value)
            if (parsed - self.now).total_seconds() > 300:
                raise ValueError("future timestamp")
            return parsed
        except (ValueError, TypeError, OverflowError):
            self.add("TIMESTAMP_INVALID", label, project, schema=True)
            return None

    def fresh(self, value, minutes, code, label, project=None, control_id=None, control_status=None):
        parsed = self.time(value, label, project)
        if parsed and (self.now - parsed).total_seconds() > minutes * 60:
            self.add(code, label, project, control_id=control_id, control_status=control_status)


def coordinator_sources(a, doc):
    nested = doc.get("coordinator", {})
    if not isinstance(nested, dict):
        a.add("COORDINATOR_INVALID", "coordinator must be an object", schema=True)
        nested = {}
    sources = []
    for name, row in [("root", doc), ("coordinator", nested)]:
        times = [a.time(row[k], f"{name}.{k}") for k in TIME_KEYS if k in row]
        valid = [t for t in times if t]
        if len(set(valid)) > 1:
            a.add("ALIAS_DRIFT", f"{name} timestamp aliases disagree")
        sources.append((max(valid) if valid else datetime.min.replace(tzinfo=UTC), name, row))
    return sorted(sources, key=lambda item: (item[0], item[1] == "root"), reverse=True)


def field_candidates(a, sources, field, aliases):
    candidates = []
    for source_time, name, row in sources:
        values = [(k, row[k]) for k in aliases if row.get(k)]
        if field == "model":
            values = values[:1]  # Display names are not model IDs.
        for key, value in values:
            if text(value) or (field == "next_action" and evidence(value)):
                candidates.append((source_time, name, key, value))
            else:
                a.add("COORDINATOR_FIELD_INVALID", f"{name}.{key}", schema=True)
    return candidates


def select_field(a, candidates, field, snapshot_minutes):
    if not candidates:
        a.add("COORDINATOR_FIELD_MISSING", field, schema=True)
        return None, None
    if len({json.dumps(v, sort_keys=True) for _, _, _, v in candidates}) > 1:
        a.add("ALIAS_DRIFT", f"Coordinator {field} aliases disagree")
    source_time, name, key, value = candidates[0]
    observed = source_time.isoformat() if source_time.year > 1 else None
    a.fresh(observed, snapshot_minutes, "COORDINATOR_FIELD_STALE", f"{field} source observation")
    return value if field != "next_action" else "present", {"source": f"{name}.{key}", "observed_at": observed}


def coordinator_view(a, doc, snapshot_minutes):
    sources = coordinator_sources(a, doc)
    observed = sources[0][0].isoformat() if sources[0][0].year > 1 else None
    if observed is None:
        a.add("COORDINATOR_TIME_MISSING", "No valid coordinator timestamp", schema=True)
    else:
        a.fresh(observed, snapshot_minutes, "COORDINATOR_STALE", "Coordinator observation")
    out = {"observed_at": observed, "field_sources": {}}
    fields = [
        ("state", ("state",)),
        ("model", ("model_id", "model", "model_display")),
        ("wake_owner", ("wake_owner",)),
        ("next_action", ("nextAction", "next_actions", "next")),
    ]
    for field, aliases in fields:
        out[field], source = select_field(a, field_candidates(a, sources, field, aliases), field, snapshot_minutes)
        if source:
            out["field_sources"][field] = source
    return out


def check_single_review(a, project, review, role, row_rev):
    if review.get("revision") != row_rev or review.get("verdict") != "SHIP" or not evidence(review.get("evidence")):
        a.add("REVIEW_NOT_CURRENT", f"{role} review is not SHIP at this revision with evidence", project)
    if not text(review.get("reviewer")):
        a.add("REVIEW_IDENTITY_MISSING", f"{role} reviewer identity missing", project)
        return None
    return review["reviewer"]


def check_single_role_review(a, project, reviews, role, target_rev, identities):
    role_reviews = [r for r in reviews if isinstance(r, dict) and r.get("role") == role]
    target_matches = (
        [r for r in role_reviews if r.get("revision") == target_rev] if is_hex_sha(target_rev) else role_reviews
    )

    if target_matches:
        if len(target_matches) != 1:
            a.add("REVIEW_MISSING_OR_DUPLICATE", f"Expected one {role} review", project)
            return
        selected = target_matches[0]
    elif len(role_reviews) == 1:
        selected = role_reviews[0]
    else:
        a.add("REVIEW_MISSING_OR_DUPLICATE", f"Expected one {role} review", project)
        return

    ident = check_single_review(a, project, selected, role, target_rev)
    if ident:
        identities.append(ident)


def review_checks_compat(reviews, policy, target_rev):
    req_roles = policy.get("required_review_roles", []) if isinstance(policy, dict) else []
    dummy = Audit(datetime.now(UTC))
    row = {"reviews": reviews if isinstance(reviews, list) else [], "revision": target_rev}
    review_checks(dummy, "check", row, req_roles, target_rev)
    return dummy.diagnostics


def review_checks(a_or_reviews, project_or_policy=None, row_or_target=None, roles=None, target_revision=None):
    if not isinstance(a_or_reviews, Audit):
        return review_checks_compat(a_or_reviews, project_or_policy, row_or_target)

    a, project, row = a_or_reviews, project_or_policy, row_or_target
    reviews = row.get("reviews", []) if isinstance(row, dict) else []
    if not isinstance(reviews, list) or any(not isinstance(v, dict) for v in reviews):
        a.add("REVIEWS_INVALID", "reviews must be a list of objects", project, True)
        return

    target_rev = target_revision or (row.get("revision") if isinstance(row, dict) else None)

    identities = []
    for role in roles or []:
        check_single_role_review(a, project, reviews, role, target_rev, identities)
    if len(identities) != len(set(identities)):
        a.add("REVIEW_NOT_INDEPENDENT", "Reviewer identities repeat", project)


def merge_checks(a, project, row, roles):
    head = row.get("revision")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        a.add("REVISION_INVALID", "Merge readiness needs a full commit SHA", project)
    review_checks(a, project, row, roles)
    gates = {}
    for key in ("ci", "branch_protection", "approval"):
        gates[key] = row.get(key) if isinstance(row.get(key), dict) else {}
        if not gates[key]:
            a.add("MERGE_GATE_MISSING", f"{key} evidence object missing", project)
    ci, protection, approval = (gates[k] for k in ("ci", "branch_protection", "approval"))
    if ci.get("revision") != head or ci.get("status") != "success" or not evidence(ci.get("evidence")):
        a.add("CI_NOT_CURRENT", "Successful CI at this revision with evidence required", project)
    if protection.get("verified") is not True or not evidence(protection.get("evidence")):
        a.add("PROTECTION_UNPROVEN", "Effective protection evidence required", project)
    if not text(approval.get("authority")):
        a.add("AUTHORITY_MISSING", "Primary authority reference required; this audit cannot grant approval", project)


def check_worker_permissions(a, project, worker):
    if any(type(worker.get(k)) is not bool for k in ("sandbox", "permission_bypass")):
        a.add("WORKER_PERMISSIONS_INVALID", "sandbox and permission_bypass must be explicit booleans", project, True)
    if worker.get("sandbox") is not True:
        a.add("SANDBOX_UNPROVEN", "Worker sandbox is not recorded as enabled", project)
    if worker.get("permission_bypass") is True and not text(worker.get("authority")):
        a.add("BYPASS_AUTHORITY_MISSING", "Permission bypass lacks primary authority reference", project)


def check_worker_role_model(a, project, worker, policy):
    role, model = worker["role"], worker["model"]
    if isinstance(policy, dict):
        implementer_model = policy.get("implementer_model")
        reviewer_models = policy.get("reviewer_models")
        banned = policy.get("banned_models") or []
    else:
        implementer_model, reviewer_models, banned = policy, None, []

    if role == "review":
        if worker.get("write_paths"):
            a.add("WORKER_INVALID", "Reviewer worker must have empty write_paths (read-only)", project, True)
        if not reviewer_models:
            a.add(
                "REVIEWER_MODEL_INVALID",
                "Review worker present but policy reviewer_models is missing or empty",
                project,
            )
        elif model not in reviewer_models or model in banned:
            a.add("REVIEWER_MODEL_INVALID", f"Reviewer model {model} is not allowed or is banned", project)
    elif role not in ("implementation", "remediation", "implementer") or model != implementer_model:
        a.add("IMPLEMENTER_MODEL", "Implementation/remediation model or role differs from policy", project)


def worker_identity(a, project, worker, policy):
    required = ("id", "model", "role", "user", "worktree")
    if not isinstance(worker, dict) or any(not text(worker.get(k)) for k in required):
        a.add("WORKER_INVALID", "Worker identity, model, role, user, worktree and write_paths required", project, True)
        return False
    has_write_paths = evidence(worker.get("write_paths"))
    has_exports = evidence(worker.get("artifact_exports"))
    if not (
        has_write_paths or (worker["role"] == "review" and has_exports and isinstance(worker.get("write_paths"), list))
    ):
        a.add("WORKER_INVALID", "Worker identity, model, role, user, worktree and write_paths required", project, True)
        return False
    if "artifact_exports" in worker and (
        not isinstance(worker["artifact_exports"], list) or any(not text(v) for v in worker["artifact_exports"])
    ):
        a.add("WORKER_INVALID", "artifact_exports must be a list of nonempty strings", project, True)
        return False
    check_worker_role_model(a, project, worker, policy)
    check_worker_permissions(a, project, worker)
    if not Path(worker["worktree"]).is_absolute():
        a.add("WORKTREE_INVALID", "Worker worktree must be absolute", project, True)
        return False
    return True


def worker_paths(a, project, worker):
    worktree = str(Path(worker["worktree"]).resolve())
    write_paths = [str((Path(p) if p.startswith("/") else Path(worktree) / p).resolve()) for p in worker["write_paths"]]
    raw_exports = worker.get("artifact_exports", [])
    artifact_exports = [str((Path(p) if p.startswith("/") else Path(worktree) / p).resolve()) for p in raw_exports]
    check_paths = [worker["worktree"], worktree] + worker["write_paths"] + write_paths + raw_exports + artifact_exports
    for path in check_paths:
        parts = PurePosixPath(path).parts
        if len(parts) > 2 and parts[1] == "home" and parts[2] != worker["user"]:
            a.add("CROSS_USER_PATH", "Worker scope crosses a different home directory", project)
            break
    if any(p != worktree and not p.startswith(worktree.rstrip("/") + "/") for p in write_paths):
        a.add("WORKTREE_SCOPE_ESCAPE", "Worker write scope escapes the recorded worktree", project)
    return project, worker["id"], worktree, write_paths + artifact_exports


def worker_checks(a, project, row, policy, all_workers):
    workers = row.get("workers", [])
    if not isinstance(workers, list):
        a.add("WORKERS_INVALID", "workers must be a list", project, True)
        return
    for worker in workers:
        if worker_identity(a, project, worker, policy):
            all_workers.append(worker_paths(a, project, worker))


def waiting_check(a, project, row):
    wait = row.get("wait", {})
    if not isinstance(wait, dict) or not text(wait.get("reason")) or not evidence(wait.get("evidence")):
        a.add("WAIT_INVALID", "Waiting needs reason, evidence and deadline", project, True)
        return
    try:
        if timestamp(wait.get("until")) <= a.now:
            a.add("WAIT_DUE", "Waiting deadline reached; inspect outcome", project)
    except (ValueError, TypeError, OverflowError):
        a.add("WAIT_INVALID", "Invalid wait deadline", project, True)


def check_blocker_recheck(a, project, recheck_val, policy, is_stale):
    try:
        recheck_time = timestamp(recheck_val)
    except (ValueError, TypeError, OverflowError):
        a.add("BLOCKER_INVALID", "Invalid blocker recheck deadline", project, True)
        return
    if recheck_time <= a.now:
        a.add("BLOCKER_RECHECK_DUE", "Blocker recheck deadline reached; inspect outcome", project)
        return
    if is_stale:
        horizon_hours = policy.get("max_recheck_horizon_hours", 168)
        if (recheck_time - a.now).total_seconds() > horizon_hours * 3600:
            a.add(
                "BLOCKER_RECHECK_HORIZON_EXCEEDED",
                f"Blocker recheck deadline exceeds maximum policy horizon ({horizon_hours}h)",
                project,
            )


def is_valid_blocker(blocker):
    return (
        isinstance(blocker, dict)
        and text(blocker.get("owner"))
        and text(blocker.get("reason"))
        and evidence(blocker.get("evidence"))
    )


def blocker_since_time(a, project, blocker, row, progress):
    since_val = next(
        (blocker[k] for k in ("since", "blocked_at", "observed_at") if blocker.get(k) is not None),
        None,
    )
    if since_val is not None:
        return since_val, a.time(since_val, "Blocker since", project)
    return row.get("last_progress_at"), progress


def blocker_check(a, project, row, policy, progress=None):
    if row.get("stage") in ("done", "complete"):
        return
    blocker = row.get("blocker")
    if row.get("stage") != "blocked" and (blocker is None or blocker == {}):
        return
    if not is_valid_blocker(blocker):
        a.add("BLOCKER_INVALID", "Blocked needs accountable owner, reason and evidence", project, True)
        return
    since_val, since_time = blocker_since_time(a, project, blocker, row, progress)
    is_stale = (since_val is None) or (
        since_time is not None and (a.now - since_time).total_seconds() > policy["max_progress_age_minutes"] * 60
    )
    recheck_val = next(
        (blocker[k] for k in ("recheck_at", "recheck_by", "until") if blocker.get(k) is not None),
        None,
    )
    if recheck_val is not None:
        check_blocker_recheck(a, project, recheck_val, policy, is_stale)
        return
    if since_val is None:
        a.add("BLOCKER_STALE", "Blocker has no active recheck deadline and no recent progress timestamp", project)
    elif is_stale:
        a.add("BLOCKER_STALE", "No recorded artifact or stage advancement within policy window", project)


def check_claim_expiry(a, project, wi):
    for exp_key in ("claim_expires_at", "expires_at", "until"):
        if exp_key in wi:
            try:
                if timestamp(wi[exp_key]) <= a.now:
                    a.add("STALE_OR_MISSING_CLAIM", f"Claim token expired at {wi[exp_key]}", project)
                    return True
            except (ValueError, TypeError, OverflowError):
                a.add("WORK_ITEM_INVALID", f"Invalid {exp_key} timestamp", project, schema=True)
                return True
    return False


def check_claim_staleness(a, project, wi, policy):
    if wi.get("stale") is True or wi.get("status") == "stale":
        a.add("STALE_OR_MISSING_CLAIM", "Claim is marked stale", project)
        return
    if "claim_observed_at" in wi:
        parsed = a.time(wi["claim_observed_at"], "claim_observed_at", project)
        if parsed and (a.now - parsed).total_seconds() > policy["max_progress_age_minutes"] * 60:
            a.add("STALE_OR_MISSING_CLAIM", "Claim observation exceeds max_progress_age_minutes", project)


def check_claim_token(a, project, wi, policy):
    token = wi.get("claim_token")
    if not text(token):
        a.add("STALE_OR_MISSING_CLAIM", "Active implementation/review row requires valid claim_token", project)
        return
    if not check_claim_expiry(a, project, wi):
        check_claim_staleness(a, project, wi, policy)


def check_active_stage_claim(a, project, row, stage, wi, policy):
    if "work_item" not in row or wi is None:
        a.add("STALE_OR_MISSING_CLAIM", f"Active stage '{stage}' requires work_item with active claim_token", project)
        return False
    if not isinstance(wi, dict):
        a.add("STALE_OR_MISSING_CLAIM", f"Active stage '{stage}' requires work_item with active claim_token", project)
        a.add("WORK_ITEM_INVALID", "work_item must be an object", project, schema=True)
        return False
    check_claim_token(a, project, wi, policy)
    return True


def check_work_item_fields(a, project, wi):
    if not isinstance(wi, dict):
        a.add("WORK_ITEM_INVALID", "work_item must be an object", project, schema=True)
        return
    if "attempt" in wi:
        att = wi["attempt"]
        if not isinstance(att, int) or isinstance(att, bool) or att <= 0:
            a.add("WORK_ITEM_INVALID", "work_item.attempt must be a positive integer", project, schema=True)
    if "admission_key" in wi and not text(wi["admission_key"]):
        a.add("WORK_ITEM_INVALID", "work_item.admission_key must be a non-empty string", project, schema=True)


def resolve_requested_transition_str(row, wi):
    if isinstance(wi, dict):
        trans = wi.get("requested_transition")
        return trans if isinstance(trans, str) else None
    if isinstance(row, dict):
        trans = row.get("requested_transition")
        return trans if isinstance(trans, str) else None
    return None


def validate_work_item(a, project, row, policy):
    if not isinstance(row, dict):
        return
    stage = row.get("stage")
    wi = row.get("work_item")
    trans_str = resolve_requested_transition_str(row, wi)

    if stage in ACTIVE_CLAIM_STAGES:
        if not check_active_stage_claim(a, project, row, stage, wi, policy):
            return
    elif trans_str in TRANSITION_CLAIM_STAGES:
        if "work_item" not in row or wi is None:
            return
        if not isinstance(wi, dict):
            a.add("WORK_ITEM_INVALID", "work_item must be an object", project, schema=True)
            return
        check_claim_token(a, project, wi, policy)

    if "work_item" not in row or wi is None:
        return
    if not isinstance(wi, dict):
        a.add("WORK_ITEM_INVALID", "work_item must be an object", project, schema=True)
        return

    check_work_item_fields(a, project, wi)


def guard_admission(a, project, row, wi):
    task = wi.get("issue_or_task") or row.get("issue_or_task")
    key = wi.get("admission_key")
    if not text(task) or not text(key):
        a.add(
            "TRANSITION_GUARD_FAILED",
            "Admission requires issue_or_task and admission_key",
            project,
            next_permissible_action="Provide issue_or_task and admission_key before requesting admission",
        )


def guard_implementation(a, project, row, wi, policy):
    raw_workers = row.get("workers")
    workers = raw_workers if isinstance(raw_workers, list) else []
    impl_model = policy.get("implementer_model") if isinstance(policy, dict) else None
    has_implementer = any(
        isinstance(w, dict)
        and w.get("role") in ("implementation", "remediation", "implementer")
        and w.get("model") == impl_model
        for w in workers
    )
    token = wi.get("claim_token") if isinstance(wi, dict) else None
    if not has_implementer or not text(token):
        a.add(
            "TRANSITION_GUARD_FAILED",
            "Implementation requires active implementer worker and valid claim_token",
            project,
            next_permissible_action="Assign approved implementer worker and valid claim_token",
        )


def resolve_guard_revision(a, project, row, wi, action_name):
    rev_row = row.get("revision")
    rev_wi = wi.get("revision") if isinstance(wi, dict) else None
    if rev_row and rev_wi and rev_row != rev_wi:
        a.add(
            "REVISION_CHANGED_AFTER_REVIEW",
            f"Work item revision {rev_wi} does not match row revision {rev_row}",
            project,
        )
        a.add(
            "TRANSITION_GUARD_FAILED",
            f"{action_name} requires consistent row and work_item revision",
            project,
            next_permissible_action=f"Align work_item revision with row revision before {action_name.lower()}",
        )
        return None
    rev = rev_row or rev_wi
    if not is_hex_sha(rev):
        a.add(
            "TRANSITION_GUARD_FAILED",
            f"{action_name} requires valid 40-hex revision SHA",
            project,
            next_permissible_action="Provide valid 40-hex commit SHA revision",
        )
        return None
    return rev


def check_reviews_revision_drift(a, project, reviews, rev):
    for r in reviews:
        if isinstance(r, dict) and r.get("revision") and r.get("revision") != rev:
            a.add(
                "REVISION_CHANGED_AFTER_REVIEW",
                f"Review revision {r.get('revision')} does not match target revision {rev}",
                project,
            )
            break


def is_valid_review_entry(r, rev, req_models, banned):
    if not isinstance(r, dict) or r.get("revision") != rev:
        return None
    if not evidence(r.get("evidence")) or r.get("verdict") not in ("SHIP", "REVISE"):
        return None
    model = r.get("model")
    if not text(model) or model in banned:
        return None
    if req_models is not None and model not in req_models:
        return None
    return model


def collect_reviewer_models(reviews, rev, policy):
    req_models = policy.get("reviewer_models") if isinstance(policy, dict) else None
    if not req_models:
        return set(), set()
    banned = set(policy.get("banned_models", [])) if isinstance(policy, dict) else set()
    allowed = set(req_models) - banned
    seen_models = set()
    seen_reviewers = set()
    for r in reviews:
        m = is_valid_review_entry(r, rev, allowed, banned)
        reviewer = r.get("reviewer") if isinstance(r, dict) else None
        if m and isinstance(reviewer, str) and reviewer.strip():
            seen_models.add(m)
            seen_reviewers.add(reviewer.strip())
    return seen_models, seen_reviewers


def guard_review(a, project, row, wi, policy):
    rev = resolve_guard_revision(a, project, row, wi, "Review transition")
    if not rev:
        return
    reviews = row.get("reviews", []) if isinstance(row.get("reviews"), list) else []
    check_reviews_revision_drift(a, project, reviews, rev)
    valid_models, valid_reviewers = collect_reviewer_models(reviews, rev, policy)
    roles = policy.get("required_review_roles", []) if isinstance(policy, dict) else []
    required_count = len(roles) if roles else 3
    if len(valid_models) < required_count or len(valid_reviewers) < required_count:
        a.add(
            "TRANSITION_GUARD_FAILED",
            f"Review transition requires {required_count} distinct reviewer models and sessions on exact revision",
            project,
            next_permissible_action=(
                f"Obtain {required_count} independent reviews with distinct models "
                "and reviewer sessions on exact revision"
            ),
        )


def is_matching_ship(r, role, rev):
    return (
        isinstance(r, dict)
        and r.get("role") == role
        and r.get("revision") == rev
        and r.get("verdict") == "SHIP"
        and evidence(r.get("evidence"))
        and text(r.get("reviewer"))
    )


def resolve_allowed_reviewer_models(models):
    banned = set()
    if isinstance(models, dict):
        banned = set(models.get("banned_models", []))
        models = models.get("reviewer_models")
    if not models or not isinstance(models, (list, set, tuple)):
        return None
    allowed = set(models) - banned
    return allowed or None


def check_role_ship_review(reviews, role, rev, allowed_models, seen_reviewers, seen_models):
    matched = [r for r in reviews if is_matching_ship(r, role, rev)]
    if len(matched) != 1:
        return False
    r = matched[0]
    reviewer = r.get("reviewer")
    model = r.get("model")
    if not isinstance(reviewer, str) or not reviewer.strip():
        return False
    if not isinstance(model, str) or model not in allowed_models:
        return False
    seen_reviewers.add(reviewer.strip())
    seen_models.add(model)
    return True


def is_unanimous_ship(reviews, roles, rev, models=None):
    if not is_hex_sha(rev) or not isinstance(reviews, list) or not reviews or not roles:
        return False
    if any(isinstance(r, dict) and r.get("revision") == rev and r.get("verdict") != "SHIP" for r in reviews):
        return False
    allowed_models = resolve_allowed_reviewer_models(models)
    if not allowed_models:
        return False
    seen_reviewers = set()
    seen_models = set()
    for role in roles:
        if not check_role_ship_review(reviews, role, rev, allowed_models, seen_reviewers, seen_models):
            return False
    return len(seen_reviewers) == len(roles) and len(seen_models) == len(roles)


def check_single_control_gate(c, rev):
    if not isinstance(c, dict):
        return False
    status = c.get("status")
    if status == "enforced":
        return is_hex_sha(rev) and c.get("revision") == rev and evidence(c.get("evidence"))
    if status == "not_applicable":
        return text(c.get("reason")) and evidence(c.get("evidence"))
    return False


def check_required_control_gates(row, policy, rev):
    req_controls = policy.get("required_controls", []) if isinstance(policy, dict) else []
    if not req_controls:
        return []
    controls = row.get("controls")
    if not isinstance(controls, dict):
        return [f"control:{cid}" for cid in req_controls]
    missing = []
    for cid in req_controls:
        if not check_single_control_gate(controls.get(cid), rev):
            missing.append(f"control:{cid}")
    return missing


def check_mandatory_gates(row, policy, rev):
    missing = []
    if not is_hex_sha(rev):
        missing.append("revision:valid_40_hex")
    ci = row.get("ci")
    if not (
        isinstance(ci, dict)
        and is_hex_sha(rev)
        and ci.get("revision") == rev
        and ci.get("status") == "success"
        and evidence(ci.get("evidence"))
    ):
        missing.append("ci")
    prot = row.get("branch_protection")
    if not (isinstance(prot, dict) and prot.get("verified") is True and evidence(prot.get("evidence"))):
        missing.append("branch_protection")
    app = row.get("approval")
    if not (isinstance(app, dict) and text(app.get("authority"))):
        missing.append("approval")
    missing.extend(check_required_control_gates(row, policy, rev))
    return missing


def check_completion_evidence(a, project, row, wi, target):
    ev = row.get("evidence")
    if not ev and isinstance(wi, dict):
        ev = wi.get("evidence")
    if not evidence(ev) or any(is_forged_evidence_item(item) for item in ev):
        a.add(
            "INSUFFICIENT_COMPLETION_EVIDENCE",
            f"{target} completion evidence is missing, placeholder or forged",
            project,
        )
        a.add(
            "TRANSITION_GUARD_FAILED",
            f"{target} requires sufficient non-forged completion evidence",
            project,
            next_permissible_action="Provide concrete verified completion receipts and artifacts",
        )


def guard_merge_or_complete(a, project, row, wi, policy, target):
    rev = resolve_guard_revision(a, project, row, wi, target)
    if not rev:
        return
    reviews = row.get("reviews", []) if isinstance(row.get("reviews"), list) else []
    roles = policy.get("required_review_roles", []) if isinstance(policy, dict) else []
    if not is_unanimous_ship(reviews, roles, rev, policy):
        a.add(
            "TRANSITION_GUARD_FAILED",
            f"{target} requires unanimous SHIP on exact revision",
            project,
            next_permissible_action="Obtain unanimous SHIP reviews from required roles on exact revision",
        )
    check_completion_evidence(a, project, row, wi, target)
    missing = check_mandatory_gates(row, policy, rev)
    if missing:
        for gate in missing:
            a.add("MISSING_MANDATORY_GATE", f"Mandatory gate '{gate}' is missing or unenforced", project)
        a.add(
            "TRANSITION_GUARD_FAILED",
            f"{target} requires all mandatory gates enforced ({', '.join(missing)})",
            project,
            next_permissible_action="Enforce all mandatory gates and controls before transition",
        )


def resolve_requested_transition(a, project, row, wi):
    target = None
    has_target = False
    if "requested_transition" in row:
        has_target = True
        target = row.get("requested_transition")
    if isinstance(wi, dict) and "requested_transition" in wi:
        has_target = True
        wi_target = wi.get("requested_transition")
        if target is not None and wi_target is not None and target != wi_target:
            a.add(
                "STAGE_TRANSITION_REJECTED",
                "Conflicting requested_transition between row and work_item",
                project,
                schema=True,
            )
            return None, False, False
        target = wi_target if wi_target is not None else target

    if not has_target:
        return None, True, False
    if not isinstance(target, str) or not target.strip() or target not in VALID_TRANSITIONS:
        a.add("STAGE_TRANSITION_REJECTED", f"Invalid requested_transition: {target!r}", project, schema=True)
        a.add("WORK_ITEM_INVALID", f"Invalid requested_transition: {target!r}", project, schema=True)
        return None, False, True
    return target, True, True


def check_not_before(a, project, wi, target, stage):
    if not isinstance(wi, dict) or "not_before" not in wi:
        return True
    try:
        if timestamp(wi["not_before"]) > a.now:
            act = target or stage or "stage"
            a.add(
                "TRANSITION_GUARD_FAILED",
                f"{act} transition blocked until {wi['not_before']}",
                project,
                next_permissible_action=f"Wait until not_before time {wi['not_before']} elapses",
            )
            return False
    except (ValueError, TypeError, OverflowError):
        a.add("WORK_ITEM_INVALID", "Invalid not_before timestamp in work_item", project, schema=True)
        return False
    return True


def dispatch_requested_guard(a, project, row, wi, policy, target):
    if target == "admission":
        guard_admission(a, project, row, wi)
    elif target == "implementation":
        guard_implementation(a, project, row, wi, policy)
    elif target == "review":
        guard_review(a, project, row, wi, policy)
    elif target in ("merge", "postmerge", "complete"):
        guard_merge_or_complete(a, project, row, wi, policy, target)


def auto_enforce_stage_guard(a, project, row, wi, policy, guard_executed, has_target):
    stage = row.get("stage")
    has_details = bool(wi) or "reviews" in row or "ci" in row or has_target
    if stage == "review" and guard_executed != "review":
        guard_review(a, project, row, wi, policy)
    elif stage == "merge_ready" and guard_executed != "merge" and has_details:
        guard_merge_or_complete(a, project, row, wi, policy, "merge")
    elif stage == "done" and guard_executed != "complete" and has_details:
        guard_merge_or_complete(a, project, row, wi, policy, "complete")


def check_transition_guard(a, project, row, wi, policy):
    target, valid, has_target = resolve_requested_transition(a, project, row, wi)
    if not valid:
        return
    stage = row.get("stage")
    if not check_not_before(a, project, wi, target, stage):
        return
    if target:
        dispatch_requested_guard(a, project, row, wi, policy, target)
    auto_enforce_stage_guard(a, project, row, wi, policy, target, has_target)


def check_admission_uniqueness(a, project, row, active_admissions):
    if row.get("stage") == "done":
        return
    wi = row.get("work_item")
    if not isinstance(wi, dict) or not text(wi.get("admission_key")):
        return
    key = wi["admission_key"]
    if key in active_admissions:
        other = active_admissions[key]
        a.add("DUPLICATE_ADMISSION", f"Conflicting admission_key '{key}' already active in project {other}", project)
    else:
        active_admissions[key] = project


def project_row(a, project, row, policy):
    stage = row.get("stage")
    if (
        stage not in STAGES
        or not text(row.get("owner"))
        or not text(row.get("next_action"))
        or not evidence(row.get("evidence"))
    ):
        a.add("PROJECT_FIELDS_INVALID", "Valid stage, owner, next_action and evidence required", project, True)
    a.fresh(
        row.get("observed_at"),
        policy["max_snapshot_age_minutes"],
        "PROJECT_STALE",
        "Project observation",
        project,
    )
    progress = a.time(row.get("last_progress_at"), "Project last_progress_at", project)
    if stage == "waiting":
        waiting_check(a, project, row)
    elif stage == "blocked":
        blocker_check(a, project, row, policy, progress)
    elif stage != "done" and progress and (a.now - progress).total_seconds() > policy["max_progress_age_minutes"] * 60:
        a.add("PROGRESS_AGING", "No recorded artifact or stage advancement within policy window", project)
    if stage != "blocked" and "blocker" in row:
        blocker_check(a, project, row, policy, progress)
    if stage == "merge_ready":
        merge_checks(a, project, row, policy["required_review_roles"])

    validate_work_item(a, project, row, policy)
    wi = row.get("work_item")
    wi_dict = wi if isinstance(wi, dict) else {}
    check_transition_guard(a, project, row, wi_dict, policy)

    res = {
        "project": project,
        "coverage": "normalized",
        "stage": stage,
        "evidence_status": "references_unverified",
        "controls": control_checks(a, project, row, policy),
    }
    if isinstance(row.get("work_item"), dict):
        res["work_item"] = {k: v for k, v in row["work_item"].items() if k != "claim_token"}
    return res


def control_status(a, project, identifier, control, revision_id):
    label = f"Control {identifier}"
    status = control.get("status")
    if status == "not_applicable":
        if not text(control.get("reason")) or not evidence(control.get("evidence")):
            a.add(
                "CONTROL_NA_UNJUSTIFIED",
                f"{label}: reason and evidence required",
                project,
                control_id=identifier,
                control_status="not_applicable",
            )
        return
    if status not in ("enforced", "implementing", "gap"):
        a.add(
            "CONTROL_GAP",
            f"{label}: status is unknown",
            project,
            control_id=identifier,
            control_status=status or "unknown",
        )
        return
    if not text(control.get("next_action")):
        a.add(
            "CONTROL_INVALID",
            f"{label}: next_action required",
            project,
            True,
            control_id=identifier,
            control_status="invalid",
        )
    if status != "enforced":
        a.add(
            "CONTROL_GAP",
            f"{label}: {status}; completion remains unproven",
            project,
            control_id=identifier,
            control_status=status,
        )
        return
    full_revision = is_hex_sha(revision_id)
    if not full_revision or control.get("revision") != revision_id or not evidence(control.get("evidence")):
        a.add(
            "CONTROL_ENFORCEMENT_UNPROVEN",
            f"{label}: current full revision and evidence required",
            project,
            control_id=identifier,
            control_status="unenforced",
        )


def control_checks(a, project, row, policy):
    required = policy["required_controls"]
    if not required:
        return []
    controls = row.get("controls", {})
    if not isinstance(controls, dict):
        a.add("CONTROLS_INVALID", "controls must be a mapping", project, True)
        for identifier in required:
            a.add(
                "CONTROL_MISSING",
                f"Control {identifier}: no current record",
                project,
                control_id=identifier,
                control_status="missing",
            )
        return []
    summary = []
    for identifier in required:
        if identifier not in controls:
            a.add(
                "CONTROL_MISSING",
                f"Control {identifier}: no current record",
                project,
                control_id=identifier,
                control_status="missing",
            )
            continue
        control = controls[identifier]
        if (
            not isinstance(control, dict)
            or not text(control.get("owner"))
            or not isinstance(control.get("status"), str)
        ):
            a.add(
                "CONTROL_INVALID",
                f"Control {identifier}: object, owner and status required",
                project,
                True,
                control_id=identifier,
                control_status="invalid",
            )
            continue
        if "evidence" in control and (
            not isinstance(control["evidence"], list) or any(not text(v) for v in control["evidence"])
        ):
            a.add(
                "CONTROL_INVALID",
                f"Control {identifier}: evidence must be a list of references",
                project,
                True,
                control_id=identifier,
                control_status="invalid",
            )
        a.fresh(
            control.get("observed_at"),
            policy["max_snapshot_age_minutes"],
            "CONTROL_STALE",
            f"Control {identifier}: observation",
            project,
            control_id=identifier,
            control_status="stale",
        )
        control_status(a, project, identifier, control, row.get("revision"))
        summary.append({"id": identifier, "status": control["status"]})
    return summary


def writer_conflicts(a, workers):
    for i, (project, identity, _, paths) in enumerate(workers):
        for other, other_id, _, other_paths in workers[i + 1 :]:
            if identity == other_id or any(paths_overlap(x, y) for x in paths for y in other_paths):
                a.add("WRITER_CONFLICT", f"Writer ownership conflicts with project {other}", project)


def legacy_item_match(item, pid):
    if not isinstance(item, dict):
        return False
    return any(item.get(k) == pid for k in ("id", "name", "project", "key"))


def search_legacy_source(source, prefix, pid):
    if isinstance(source, dict) and pid in source:
        escaped_pid = escape_json_pointer(pid)
        return source[pid], f"/{prefix}/{escaped_pid}"
    if isinstance(source, list):
        for idx, item in enumerate(source):
            if legacy_item_match(item, pid):
                return item, f"/{prefix}/{idx}"
    return None, None


def find_legacy_project(doc, pid):
    if not isinstance(doc, dict):
        return None, None
    entry, pointer = search_legacy_source(doc.get("initiatives"), "initiatives", pid)
    if pointer is not None:
        return entry, pointer
    return search_legacy_source(doc.get("projects"), "projects", pid)


def parse_legacy_observed_at(a, entry):
    if not isinstance(entry, dict):
        return None, True, None
    raw = next((entry[k] for k in TIME_KEYS if entry.get(k) is not None and entry.get(k) != ""), None)
    if raw is None:
        return None, True, None
    if not isinstance(raw, str):
        return raw, False, None
    try:
        obs = timestamp(raw)
        age = int((a.now - obs).total_seconds())
        if age < 0:
            return raw, False, age
        return raw, True, age
    except (ValueError, TypeError, OverflowError):
        return raw, False, None


def legacy_project_entry(a, project, legacy_entry, source_pointer):
    observed_at, valid, age_seconds = parse_legacy_observed_at(a, legacy_entry)
    a.add(
        "LEGACY_UNVERIFIED",
        f"Project recorded in legacy source at {source_pointer}",
        project=project,
        schema=False,
    )
    res = {
        "project": project,
        "coverage": "legacy_unverified",
        "source_pointer": source_pointer,
        "source_sha256": canonical_sha256(legacy_entry),
        "observed_at": observed_at,
        "age_seconds": age_seconds,
    }
    if not valid:
        res["observed_at_valid"] = False
    return res


def project_checks(a, contract, policy, doc=None):
    rows = contract.get("projects", {})
    malformed_container = "projects" in contract and not isinstance(contract.get("projects"), dict)
    if malformed_container or not isinstance(rows, dict):
        a.add("PROJECTS_INVALID", "delivery_control.projects must be an object", schema=True)
        rows = {}
    workers, summary = [], []
    active_admissions = {}
    for project in policy["expected_projects"]:
        if malformed_container:
            a.add("PROJECT_INVALID", "Project row must be an object", project, schema=True)
            summary.append({"project": project, "coverage": "invalid"})
            continue
        if project in rows:
            row = rows[project]
            if not isinstance(row, dict):
                a.add("PROJECT_INVALID", "Project row must be an object", project, schema=True)
                summary.append({"project": project, "coverage": "invalid"})
                continue
            summary.append(project_row(a, project, row, policy))
            worker_checks(a, project, row, policy, workers)
            check_admission_uniqueness(a, project, row, active_admissions)
            continue
        legacy_entry, source_pointer = find_legacy_project(doc, project)
        if source_pointer is not None:
            summary.append(legacy_project_entry(a, project, legacy_entry, source_pointer))
            continue
        summary.append({"project": project, "coverage": "absent"})
        a.add(
            "PROJECT_MISSING",
            f"Project {project} is absent from delivery_control and legacy initiatives",
            project=project,
            schema=True,
        )
    writer_conflicts(a, workers)
    return summary


def paths_overlap(left, right):
    if any(c in left + right for c in "*?["):
        left, right = (re.split(r"[?*\[]", p, maxsplit=1)[0] for p in (left, right))
        return left.startswith(right) or right.startswith(left)
    return left == right or left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")


def collection_records(a, doc, keys, kind):
    records = []
    for key in keys:
        if not isinstance(doc[key], list) or any(not isinstance(v, dict) for v in doc[key]):
            a.add(f"{kind}_COLLECTION_INVALID", f"{key} must be a list of objects", schema=True)
        else:
            records.extend(doc[key])
    return records


def completed_results(a, directory):
    path = directory / "local-action-results.json"
    if not path.exists():
        return set(), "not_observed"
    results, _ = a.read(path)
    keys = [key for key in ("results", "items") if key in results]
    if not keys:
        a.add("RESULT_SCHEMA_UNKNOWN", "Results file has no recognized results/items collection")
    complete = set()
    for row in collection_records(a, results, keys, "RESULT"):
        if row.get("status") not in ("completed", "done", "succeeded", "fulfilled"):
            continue
        identity, rev = row.get("id", row.get("action_id")), revision(row)
        if not text(identity) or (rev is not None and not text(rev)):
            a.add("RESULT_ID_INVALID", "Result ID and optional revision must be strings", schema=True)
        else:
            complete.add((identity, rev))
    return complete, "recognized" if keys else "unknown_schema"


def deduplicate_actions(a, records):
    seen, conflicts = {}, set()
    for item in records:
        action_id = item.get("id")
        if not text(action_id):
            a.add("ACTION_ID_MISSING", "Local action lacks stable ID", schema=True)
            continue
        if revision(item) is not None and not text(revision(item)):
            a.add("ACTION_REVISION_INVALID", "Local action revision must be a string", schema=True)
            continue
        if action_id in seen and seen[action_id] != item:
            a.add("ACTION_ID_CONFLICT", "Duplicate local-action ID has conflicting content")
            conflicts.add(action_id)
        seen[action_id] = item
    return {identity: item for identity, item in seen.items() if identity not in conflicts}


def local_actions(a, directory):
    path = directory / "local-actions.json"
    if not path.exists():
        a.add("ACTION_FILE_MISSING", "Local-action state was not observed; queue is unknown")
        return {"state": "not_observed", "ready": []}
    doc, _ = a.read(path)
    present = [key for key in COLLECTIONS if key in doc]
    if not present:
        a.add("ACTION_COLLECTIONS_MISSING", "No recognized local-action collection", schema=True)
    complete, result_state = completed_results(a, directory)
    seen = deduplicate_actions(a, collection_records(a, doc, present, "ACTION"))
    ready = []
    for identity, item in sorted(seen.items()):
        if (
            not str(item.get("status", "")).startswith("ready")
            or item.get("prerequisites_met") is False
            or (identity, revision(item)) in complete
        ):
            continue
        ready.append(
            {
                "id": identity,
                "status": item["status"],
                "revision": revision(item),
                "primary_authority_match_required": True,
                "prerequisites_status": "recorded_met" if item.get("prerequisites_met") is True else "unverified",
            }
        )
    return {"state": "observed", "results_state": result_state, "ready": ready}


def validate_policy_model_lists(a, policy):
    for key in ("reviewer_models", "banned_models"):
        val = policy.get(key)
        if val is not None and (not isinstance(val, list) or any(not text(v) for v in val)):
            a.add("POLICY_INVALID", f"{key} must contain nonempty strings", schema=True)
            policy[key] = []


def load_policy(a, path):
    policy, _ = a.read(Path(path))
    defaults = {
        "max_snapshot_age_minutes": 30,
        "max_progress_age_minutes": 60,
        "max_recheck_horizon_hours": 168,
        "directives": [],
        "required_controls": [],
        "reviewer_models": None,
        "banned_models": [],
    }
    policy = defaults | policy
    for key in ("expected_projects", "required_review_roles"):
        if not evidence(policy.get(key)) or len(policy[key]) != len(set(policy[key])):
            a.add("POLICY_INVALID", f"{key} must contain unique nonempty strings", schema=True)
            policy[key] = []
    if not text(policy.get("implementer_model")):
        a.add("POLICY_INVALID", "implementer_model must be an explicit nonempty string", schema=True)
        policy["implementer_model"] = None
    controls = policy["required_controls"]
    if not isinstance(controls, list) or any(not text(v) for v in controls) or len(controls) != len(set(controls)):
        a.add("POLICY_INVALID", "required_controls must contain unique nonempty strings", schema=True)
        policy["required_controls"] = []
    validate_policy_model_lists(a, policy)
    for key in ("max_snapshot_age_minutes", "max_progress_age_minutes"):
        if not isinstance(policy[key], int | float) or isinstance(policy[key], bool) or not 0 < policy[key] <= 1440:
            a.add("POLICY_INVALID", f"{key} must be positive and at most 1440", schema=True)
            policy[key] = defaults[key]
    horizon = policy.get("max_recheck_horizon_hours", 168)
    if not isinstance(horizon, int | float) or isinstance(horizon, bool) or horizon <= 0:
        a.add("POLICY_INVALID", "max_recheck_horizon_hours must be a positive number", schema=True)
        policy["max_recheck_horizon_hours"] = 168
    return policy


def load_contract(a, doc, snapshot_minutes):
    contract = doc.get("delivery_control", {})
    if not isinstance(contract, dict) or (contract and contract.get("schema_version") != 1):
        a.add("CONTRACT_INVALID", "delivery_control must use schema_version 1", schema=True)
        return {}
    if contract:
        a.fresh(contract.get("observed_at"), snapshot_minutes, "CONTRACT_STALE", "Delivery contract observation")
    return contract


def directive_check(a, directory, directive, applied):
    name, sha = directive.get("path"), directive.get("sha256")
    if not text(name) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        a.add("DIRECTIVE_INVALID", "Directive path and SHA256 required", schema=True)
        return
    path = Path(name) if Path(name).is_absolute() else directory / name
    try:
        if hashlib.sha256(bounded_bytes(path)).hexdigest() != sha:
            a.add("DIRECTIVE_CHANGED", "A governing file digest differs from the policy manifest")
    except (OSError, ValueError):
        a.add("DIRECTIVE_MISSING", "A governing file cannot be read")
    if not any(v.get("path") == name and v.get("sha256") == sha for v in applied):
        a.add("DIRECTIVE_NOT_RECORDED", "A governing revision lacks an applied receipt")


def directives_check(a, directory, policy, contract):
    directives, applied = policy.get("directives"), contract.get("directives", [])
    if (
        not isinstance(directives, list)
        or not isinstance(applied, list)
        or any(not isinstance(v, dict) for v in (directives or []) + (applied or []))
    ):
        a.add("DIRECTIVES_INVALID", "Directive lists must contain objects", schema=True)
        return
    for directive in directives:
        directive_check(a, directory, directive, applied)


def normalize_pane_tokens(pane_text: str) -> str:
    def repl(m):
        inner = m.group(1)
        compact = "".join(inner.split())
        if re.fullmatch(
            r"(dispatch|dispatch-end|dispatch-accepted):(agy-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_.:-]+)",
            compact,
        ):
            return f"[{compact}]"
        return m.group(0)

    return re.sub(r"\[([^\[\]]+)\]", repl, pane_text)


def map_token_positions(pattern, text, pos_attr="start"):
    mapping = {}
    for m in pattern.finditer(text):
        pos = m.start() if pos_attr == "start" else m.end()
        mapping.setdefault(m.group(1), []).append(pos)
    return mapping


def find_prompt_blocks(text: str) -> list[tuple[int, int]]:
    starts = map_token_positions(DISPATCH_TOKEN_PATTERN, text, "start")
    closed_starts = set()
    blocks = []
    for m in DISPATCH_END_PATTERN.finditer(text):
        disp_id = m.group(1)
        matching = starts.get(disp_id, [])
        preceding = [s for s in matching if s < m.start()]
        if preceding:
            closed_start = preceding[-1]
            blocks.append((closed_start, m.end()))
            closed_starts.add((disp_id, closed_start))

    disp_matches = list(DISPATCH_TOKEN_PATTERN.finditer(text))
    for i, m in enumerate(disp_matches):
        disp_id = m.group(1)
        s = m.start()
        if (disp_id, s) not in closed_starts:
            next_start = disp_matches[i + 1].start() if i + 1 < len(disp_matches) else len(text)
            blocks.append((s, next_start))
    return blocks


find_closed_prompt_blocks = find_prompt_blocks


def is_acceptance_valid(disp_id, acc_pos, blocks, disp_starts, disp_ends):
    if any(b_start <= acc_pos <= b_end for b_start, b_end in blocks):
        return False
    preceding_starts = [s for s in disp_starts.get(disp_id, []) if s < acc_pos]
    if not preceding_starts:
        return False
    latest_start = preceding_starts[-1]
    ends_after_latest = [e for e in disp_ends.get(disp_id, []) if latest_start < e <= acc_pos]
    return bool(ends_after_latest)


def collect_accepted_dispatches(text, blocks, disp_starts, disp_ends):
    accepted = set()
    for m in STANDALONE_ACCEPTANCE_PATTERN.finditer(text):
        disp_id = m.group(1)
        if is_acceptance_valid(disp_id, m.start(), blocks, disp_starts, disp_ends):
            accepted.add(disp_id)
    return accepted


def derive_pending_inputs_from_pane(pane_text: str, observed_at: str, evidence_ref: str) -> list[dict[str, Any]]:
    if not isinstance(pane_text, str):
        return []
    normalized = normalize_pane_tokens(pane_text)
    blocks = find_prompt_blocks(normalized)
    disp_starts = map_token_positions(DISPATCH_TOKEN_PATTERN, normalized, "start")
    disp_ends = map_token_positions(DISPATCH_END_PATTERN, normalized, "end")
    accepted = collect_accepted_dispatches(normalized, blocks, disp_starts, disp_ends)

    seen = set()
    result = []
    for match in DISPATCH_TOKEN_PATTERN.finditer(normalized):
        dispatch_id = match.group(1)
        if dispatch_id not in seen:
            seen.add(dispatch_id)
            if dispatch_id not in accepted:
                result.append(
                    {
                        "id": dispatch_id,
                        "status": "queued",
                        "observed_at": observed_at,
                        "evidence": [evidence_ref],
                    }
                )
    return result


def index_pending_items(pending_list):
    existing = {}
    for item in pending_list:
        if isinstance(item, dict):
            eid = item.get("id")
            if isinstance(eid, str) and eid.strip():
                existing[eid] = item
    return existing


def merge_single_derived_input(pending_list, existing_by_id, item):
    if not isinstance(item, dict):
        return
    disp_id = item.get("id")
    if not isinstance(disp_id, str) or not disp_id.strip():
        return
    if disp_id not in existing_by_id:
        pending_list.append(item)
        existing_by_id[disp_id] = item
        return
    existing = existing_by_id[disp_id]
    if existing.get("status") not in TERMINAL_STATUSES:
        existing.update(item)


def merge_derived_pending_inputs(pending_list, derived_entries):
    existing_by_id = index_pending_items(pending_list)
    for item in derived_entries:
        merge_single_derived_input(pending_list, existing_by_id, item)


def apply_pane_capture(a, contract, pane_capture_file):
    if pane_capture_file is None:
        return False
    pane_path = Path(pane_capture_file)
    try:
        raw_pane = bounded_bytes(pane_path)
        pane_text = raw_pane.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        a.add("PENDING_INPUT_UNKNOWN", "Pane capture file is missing or unreadable; defer input")
        return True
    derived = derive_pending_inputs_from_pane(pane_text, a.now.isoformat(), str(pane_path))
    if isinstance(contract.get("pending_inputs"), list):
        merge_derived_pending_inputs(contract["pending_inputs"], derived)
    return False


def pending_input_check(a, contract):
    pending = contract.get("pending_inputs")
    statuses = {"queued", "composer", "submitted_unverified", "read_verified", "consumed", "completed", "failed"}
    invalid = not isinstance(pending, list) or any(
        not isinstance(v, dict)
        or not text(v.get("id"))
        or not isinstance(v.get("status"), str)
        or v["status"] not in statuses
        or not evidence(v.get("evidence"))
        for v in pending
    )
    if invalid:
        a.add(
            "PENDING_INPUTS_INVALID",
            "pending_inputs require stable id, recognized status and evidence; defer input",
            schema=True,
        )
        return True
    return any(v.get("status") in ("queued", "composer", "submitted_unverified") for v in pending)


def ready_project_item(p):
    item = {"project": p["project"], "stage": p["stage"]}
    if "work_item" in p and isinstance(p["work_item"], dict):
        item["work_item"] = {k: v for k, v in p["work_item"].items() if k != "claim_token"}
    return item


def collect_ready_projects(projects):
    res = []
    for p in projects:
        if isinstance(p, dict) and p.get("coverage") == "normalized" and p.get("stage") in READY_ACTIVE_STAGES:
            res.append(ready_project_item(p))
    return res


def project_recheck_deadline(blk):
    for key in ("recheck_at", "recheck_by", "until"):
        if blk.get(key):
            return blk[key]
    return None


def extract_project_blocker(proj, r, stale_projects):
    if not isinstance(r, dict):
        return None
    blk = r.get("blocker") if isinstance(r.get("blocker"), dict) else {}
    wi = r.get("work_item") if isinstance(r.get("work_item"), dict) else {}
    recheck = project_recheck_deadline(blk)
    not_before = blk.get("not_before") or wi.get("not_before")
    is_stale = proj in stale_projects
    if not (recheck or not_before or is_stale):
        return None
    entry = {"project": proj, "stage": r.get("stage")}
    if blk.get("owner"):
        entry["owner"] = blk["owner"]
    if blk.get("reason"):
        entry["reason"] = blk["reason"]
    if recheck:
        entry["recheck_at"] = recheck
    if not_before:
        entry["not_before"] = not_before
    if is_stale:
        entry["stale"] = True
    return entry


def collect_time_critical_blockers(contract, a):
    rows = contract.get("projects", {})
    if not isinstance(rows, dict):
        return []
    stale_codes = ("BLOCKER_STALE", "BLOCKER_RECHECK_DUE", "BLOCKER_RECHECK_HORIZON_EXCEEDED")
    stale_projects = {d["project"] for d in a.diagnostics if d.get("code") in stale_codes and d.get("project")}
    result = [extract_project_blocker(p, r, stale_projects) for p, r in rows.items()]
    return [item for item in result if item is not None]


def parse_gap_diagnostic(d):
    msg = d.get("message", "")
    m = re.match(r"Control (.+?):\s*([^;]+)", msg)
    if m:
        return m.group(1), m.group(2).strip()
    return "unknown", "gap"


def collect_gap_diagnostic_groups(a):
    groups = {}
    for d in a.diagnostics:
        proj = d.get("project")
        if not proj:
            continue
        cid = d.get("control_id")
        if cid:
            st = d.get("control_status") or "gap"
            groups.setdefault((proj, st), set()).add(cid)
        elif d.get("code") == "CONTROL_GAP":
            cid, st = parse_gap_diagnostic(d)
            groups.setdefault((proj, st), set()).add(cid)
    return groups


def append_absent_project_gaps(aggregated, projects):
    if not projects:
        return
    for p in projects:
        if isinstance(p, dict) and p.get("coverage") == "absent":
            proj = p["project"]
            aggregated.append(
                {
                    "project": proj,
                    "status": "absent",
                    "count": 1,
                    "drilldown_pointers": [f"/delivery_control/projects/{escape_json_pointer(proj)}"],
                }
            )


def aggregate_gap_diagnostics(a, projects=None):
    groups = collect_gap_diagnostic_groups(a)
    aggregated = []
    for (proj, st), cids in sorted(groups.items()):
        sorted_cids = sorted(cids)
        aggregated.append(
            {
                "project": proj,
                "status": st,
                "count": len(sorted_cids),
                "drilldown_pointers": [
                    f"/delivery_control/projects/{escape_json_pointer(proj)}/controls/{escape_json_pointer(cid)}"
                    for cid in sorted_cids
                ],
            }
        )
    append_absent_project_gaps(aggregated, projects)
    return aggregated


def extract_transition_failures(a):
    failures = []
    codes = {
        "TRANSITION_GUARD_FAILED",
        "REVISION_CHANGED_AFTER_REVIEW",
        "MISSING_MANDATORY_GATE",
        "INSUFFICIENT_COMPLETION_EVIDENCE",
        "STAGE_TRANSITION_REJECTED",
    }
    for d in a.diagnostics:
        if d.get("code") in codes or d.get("next_permissible_action"):
            failures.append(
                {
                    "project": d.get("project"),
                    "code": d.get("code"),
                    "message": d.get("message"),
                    "next_permissible_action": d.get("next_permissible_action"),
                }
            )
    return failures


def build_compact_projection(projects, a, contract, defer_input, status=None, exit_code=None, summary=None):
    if status is None:
        code = 2 if any(d.get("severity") == "schema_error" for d in a.diagnostics) else int(bool(a.diagnostics))
        status = ("pass", "attention", "schema_error")[code]
        exit_code = code
    if summary is None:
        summary = build_audit_summary(projects, a)
    pending = contract.get("pending_inputs", []) if isinstance(contract.get("pending_inputs"), list) else []
    compact = {
        "status": status,
        "exit_code": exit_code,
        "authorizes_actions": False,
        "summary": summary,
        "changed_or_ready_projects": collect_ready_projects(projects),
        "time_critical_blockers": collect_time_critical_blockers(contract, a),
        "pending_intake": {
            "defer_input": defer_input,
            "pending_inputs": pending,
        },
        "aggregated_gaps": aggregate_gap_diagnostics(a, projects),
        "transition_failures": extract_transition_failures(a),
    }
    return bound_value(compact, max_str_len=240, max_list_len=50)


def build_audit_summary(projects, a):
    norm = sum(1 for p in projects if p.get("coverage") == "normalized")
    leg = sum(1 for p in projects if p.get("coverage") == "legacy_unverified")
    absent = sum(1 for p in projects if p.get("coverage") == "absent")
    inv = sum(1 for p in projects if p.get("coverage") == "invalid")
    cov = {
        "normalized": norm,
        "legacy_unverified": leg,
        "absent": absent,
        "invalid": inv,
    }
    return {
        "normalized": norm,
        "legacy_unverified": leg,
        "absent": absent,
        "invalid": inv,
        "coverage_counts": cov,
        "diagnostic_counts": {
            "schema_error": sum(1 for d in a.diagnostics if d.get("severity") == "schema_error"),
            "attention": sum(1 for d in a.diagnostics if d.get("severity") == "attention"),
        },
    }


def audit(control_dir, policy_path, now=None, pane_capture_file=None):
    a = Audit(timestamp(now) if isinstance(now, str) else now or datetime.now(UTC))
    directory = Path(control_dir)
    policy = load_policy(a, policy_path)
    doc, digest = a.read(directory / "coordinator-status.json")
    view = coordinator_view(a, doc, policy["max_snapshot_age_minutes"])
    contract = load_contract(a, doc, policy["max_snapshot_age_minutes"])
    directives_check(a, directory, policy, contract)
    pane_failed = apply_pane_capture(a, contract, pane_capture_file)
    defer_input = pending_input_check(a, contract) or pane_failed
    projects = project_checks(a, contract, policy, doc)
    actions = local_actions(a, directory)
    code = 2 if any(d["severity"] == "schema_error" for d in a.diagnostics) else int(bool(a.diagnostics))
    status = ("pass", "attention", "schema_error")[code]
    summary = build_audit_summary(projects, a)
    compact = build_compact_projection(projects, a, contract, defer_input, status, code, summary)
    return {
        "status": status,
        "exit_code": code,
        "scope": "structural_evidence_only",
        "authorizes_actions": False,
        "observed_at": a.now.isoformat(),
        "summary": summary,
        "coordinator_sha256": digest,
        "coordinator": view,
        "projects": projects,
        "defer_input": defer_input,
        "local_actions": actions,
        "diagnostics": a.diagnostics,
        "compact_projection": compact,
    }


def run_preflight(control_dir, policy_path, now=None, pane_capture_file=None):
    return audit(control_dir, policy_path, now=now, pane_capture_file=pane_capture_file)


def print_compact_markdown(result):
    print(f"Compact projection: {result['status']}.")
    compact = result.get("compact_projection", {})
    for p in compact.get("changed_or_ready_projects", []):
        print(f"- Ready project: {p['project']} [{p['stage']}]")
    for b in compact.get("time_critical_blockers", []):
        until = b.get("recheck_at") or b.get("not_before") or "stale"
        print(f"- Time-critical blocker: {b['project']} [{until}]")
    for g in compact.get("aggregated_gaps", []):
        print(f"- Gaps: {g['project']} {g['status']}: count={g['count']}")
    for f in compact.get("transition_failures", []):
        print(f"- Transition failure: {f.get('project') or 'portfolio'} [{f.get('code')}]: {f.get('message')}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-dir", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--now")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--compact", action="store_true", help="Emit compact bounded projection")
    parser.add_argument("--full", action="store_true", help="Emit full audit report with complete diagnostics")
    parser.add_argument("--pane-capture-file", help="Path to pane capture file to derive pending inputs")
    args = parser.parse_args(argv)
    try:
        result = audit(args.control_dir, args.policy, args.now, pane_capture_file=args.pane_capture_file)
    except (ValueError, TypeError, OverflowError):
        result = {
            "status": "schema_error",
            "exit_code": 2,
            "authorizes_actions": False,
            "diagnostics": [{"code": "INPUT_INVALID", "message": "Malformed input; no action authorized"}],
            "compact_projection": {
                "status": "schema_error",
                "exit_code": 2,
                "authorizes_actions": False,
                "summary": {"normalized": 0, "legacy_unverified": 0, "absent": 0, "invalid": 0},
                "changed_or_ready_projects": [],
                "time_critical_blockers": [],
                "pending_intake": {"defer_input": True, "pending_inputs": []},
                "aggregated_gaps": [],
                "transition_failures": [],
            },
        }
    if args.compact:
        if args.format == "json":
            print(json.dumps(result["compact_projection"], indent=2, sort_keys=True))
        else:
            print_compact_markdown(result)
    elif args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"Structural preflight: {result['status']}. No actions authorized; evidence references remain unverified."
        )
        s = result.get("summary", {})
        dc = s.get("diagnostic_counts", {})
        norm = s.get("normalized", 0)
        leg = s.get("legacy_unverified", 0)
        absent = s.get("absent", 0)
        se = dc.get("schema_error", 0)
        att = dc.get("attention", 0)
        print(
            f"Summary: normalized={norm}, legacy_unverified={leg}, absent={absent} (schema_error={se}, attention={att})"
        )
        for item in result["diagnostics"]:
            print(f"- {item['code']} [{item.get('project') or 'portfolio'}]: {item['message']}")
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
