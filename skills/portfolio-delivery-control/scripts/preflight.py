#!/usr/bin/env python3
"""Read-only structural portfolio checks; this program grants no authority."""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

STAGES = {"discovery", "ready", "implementing", "review", "ci", "merge_ready", "waiting", "blocked", "done"}
TIME_KEYS = ("observedAt", "updatedAt", "updated_at", "observed_at")
COLLECTIONS = ("items", "prioritized_operator_unblock_decisions", "pending_actions")
MAX_INPUT_BYTES = 2 * 1024 * 1024


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
    return result.astimezone(timezone.utc)


def evidence(value):
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def revision(row):
    return next((row[k] for k in ("revision", "reviewed_head_sha", "head_sha", "headCommit") if row.get(k)), None)


class Audit:
    def __init__(self, now):
        self.now = now
        self.diagnostics = []

    def add(self, code, message, project=None, schema=False):
        self.diagnostics.append(
            {
                "code": code,
                "project": project,
                "message": message,
                "severity": "schema_error" if schema else "attention",
            }
        )

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

    def fresh(self, value, minutes, code, label, project=None):
        parsed = self.time(value, label, project)
        if parsed and (self.now - parsed).total_seconds() > minutes * 60:
            self.add(code, label, project)


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
        sources.append((max(valid) if valid else datetime.min.replace(tzinfo=timezone.utc), name, row))
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


def review_checks(a, project, row, roles):
    reviews = row.get("reviews", [])
    if not isinstance(reviews, list) or any(not isinstance(v, dict) for v in reviews):
        a.add("REVIEWS_INVALID", "reviews must be a list of objects", project, True)
        reviews = []
    identities = []
    for role in roles:
        matches = [r for r in reviews if r.get("role") == role]
        if len(matches) != 1:
            a.add("REVIEW_MISSING_OR_DUPLICATE", f"Expected one {role} review", project)
            continue
        review = matches[0]
        if (
            review.get("revision") != row.get("revision")
            or review.get("verdict") != "SHIP"
            or not evidence(review.get("evidence"))
        ):
            a.add("REVIEW_NOT_CURRENT", f"{role} review is not SHIP at this revision with evidence", project)
        if not text(review.get("reviewer")):
            a.add("REVIEW_IDENTITY_MISSING", f"{role} reviewer identity missing", project)
        else:
            identities.append(review["reviewer"])
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


def worker_identity(a, project, worker, implementer_model):
    required = ("id", "model", "role", "user", "worktree")
    if (
        not isinstance(worker, dict)
        or any(not text(worker.get(k)) for k in required)
        or not evidence(worker.get("write_paths"))
    ):
        a.add("WORKER_INVALID", "Worker identity, model, role, user, worktree and write_paths required", project, True)
        return False
    if worker["role"] not in ("implementation", "remediation") or worker["model"] != implementer_model:
        a.add("IMPLEMENTER_MODEL", "Implementation/remediation model or role differs from policy", project)
    if any(type(worker.get(k)) is not bool for k in ("sandbox", "permission_bypass")):
        a.add("WORKER_PERMISSIONS_INVALID", "sandbox and permission_bypass must be explicit booleans", project, True)
    if worker.get("sandbox") is not True:
        a.add("SANDBOX_UNPROVEN", "Worker sandbox is not recorded as enabled", project)
    if worker.get("permission_bypass") is True and not text(worker.get("authority")):
        a.add("BYPASS_AUTHORITY_MISSING", "Permission bypass lacks primary authority reference", project)
    if not Path(worker["worktree"]).is_absolute():
        a.add("WORKTREE_INVALID", "Worker worktree must be absolute", project, True)
        return False
    return True


def worker_paths(a, project, worker):
    worktree = str(Path(worker["worktree"]).resolve())
    paths = [str((Path(p) if p.startswith("/") else Path(worktree) / p).resolve()) for p in worker["write_paths"]]
    for path in [worker["worktree"], worktree] + worker["write_paths"] + paths:
        parts = PurePosixPath(path).parts
        if len(parts) > 2 and parts[1] == "home" and parts[2] != worker["user"]:
            a.add("CROSS_USER_PATH", "Worker scope crosses a different home directory", project)
            break
    if any(p != worktree and not p.startswith(worktree.rstrip("/") + "/") for p in paths):
        a.add("WORKTREE_SCOPE_ESCAPE", "Worker write scope escapes the recorded worktree", project)
    return project, worker["id"], worktree, paths


def worker_checks(a, project, row, implementer_model, all_workers):
    workers = row.get("workers", [])
    if not isinstance(workers, list):
        a.add("WORKERS_INVALID", "workers must be a list", project, True)
        return
    for worker in workers:
        if worker_identity(a, project, worker, implementer_model):
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


def project_row(a, project, row, policy):
    stage = row.get("stage")
    if (
        stage not in STAGES
        or not text(row.get("owner"))
        or not text(row.get("next_action"))
        or not evidence(row.get("evidence"))
    ):
        a.add("PROJECT_FIELDS_INVALID", "Valid stage, owner, next_action and evidence required", project, True)
    a.fresh(row.get("observed_at"), policy["max_snapshot_age_minutes"], "PROJECT_STALE", "Project observation", project)
    progress = a.time(row.get("last_progress_at"), "Project last_progress_at", project)
    if stage == "waiting":
        waiting_check(a, project, row)
    elif stage == "blocked":
        blocker = row.get("blocker", {})
        if (
            not isinstance(blocker, dict)
            or not all(text(blocker.get(k)) for k in ("owner", "reason"))
            or not evidence(blocker.get("evidence"))
        ):
            a.add("BLOCKER_INVALID", "Blocked needs accountable owner, reason and evidence", project, True)
    elif stage != "done" and progress and (a.now - progress).total_seconds() > policy["max_progress_age_minutes"] * 60:
        a.add("PROGRESS_AGING", "No recorded artifact or stage advancement within policy window", project)
    if stage == "merge_ready":
        merge_checks(a, project, row, policy["required_review_roles"])
    return {
        "project": project,
        "stage": stage,
        "evidence_status": "references_unverified",
        "controls": control_checks(a, project, row, policy),
    }


def control_status(a, project, identifier, control, revision_id):
    label = f"Control {identifier}"
    status = control.get("status")
    if status == "not_applicable":
        if not text(control.get("reason")) or not evidence(control.get("evidence")):
            a.add("CONTROL_NA_UNJUSTIFIED", f"{label}: reason and evidence required", project)
        return
    if status not in ("enforced", "implementing", "gap"):
        a.add("CONTROL_GAP", f"{label}: status is unknown", project)
        return
    if not text(control.get("next_action")):
        a.add("CONTROL_INVALID", f"{label}: next_action required", project, True)
    if status != "enforced":
        a.add("CONTROL_GAP", f"{label}: {status}; completion remains unproven", project)
        return
    full_revision = isinstance(revision_id, str) and re.fullmatch(r"[0-9a-f]{40}", revision_id)
    if not full_revision or control.get("revision") != revision_id or not evidence(control.get("evidence")):
        a.add("CONTROL_ENFORCEMENT_UNPROVEN", f"{label}: current full revision and evidence required", project)


def control_checks(a, project, row, policy):
    required = policy["required_controls"]
    if not required:
        return []
    controls = row.get("controls", {})
    if not isinstance(controls, dict):
        a.add("CONTROLS_INVALID", "controls must be a mapping", project, True)
        return []
    summary = []
    for identifier in required:
        if identifier not in controls:
            a.add("CONTROL_MISSING", f"Control {identifier}: no current record", project)
            continue
        control = controls[identifier]
        if (
            not isinstance(control, dict)
            or not text(control.get("owner"))
            or not isinstance(control.get("status"), str)
        ):
            a.add("CONTROL_INVALID", f"Control {identifier}: object, owner and status required", project, True)
            continue
        if "evidence" in control and (
            not isinstance(control["evidence"], list) or any(not text(v) for v in control["evidence"])
        ):
            a.add("CONTROL_INVALID", f"Control {identifier}: evidence must be a list of references", project, True)
        a.fresh(
            control.get("observed_at"),
            policy["max_snapshot_age_minutes"],
            "CONTROL_STALE",
            f"Control {identifier}: observation",
            project,
        )
        control_status(a, project, identifier, control, row.get("revision"))
        summary.append({"id": identifier, "status": control["status"]})
    return summary


def writer_conflicts(a, workers):
    for i, (project, identity, _, paths) in enumerate(workers):
        for other, other_id, _, other_paths in workers[i + 1 :]:
            if identity == other_id or any(paths_overlap(x, y) for x in paths for y in other_paths):
                a.add("WRITER_CONFLICT", f"Writer ownership conflicts with project {other}", project)


def project_checks(a, contract, policy):
    rows = contract.get("projects", {})
    if not isinstance(rows, dict):
        a.add("PROJECTS_INVALID", "delivery_control.projects must be an object", schema=True)
        rows = {}
    workers, summary = [], []
    for project in policy["expected_projects"]:
        row = rows.get(project)
        if row is None:
            a.add("COVERAGE_MISSING", "No current canonical project row", project)
            continue
        if not isinstance(row, dict):
            a.add("PROJECT_INVALID", "Project row must be an object", project, True)
            continue
        summary.append(project_row(a, project, row, policy))
        worker_checks(a, project, row, policy["implementer_model"], workers)
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


def load_policy(a, path):
    policy, _ = a.read(Path(path))
    defaults = {
        "max_snapshot_age_minutes": 30,
        "max_progress_age_minutes": 60,
        "directives": [],
        "required_controls": [],
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
    for key in ("max_snapshot_age_minutes", "max_progress_age_minutes"):
        if not isinstance(policy[key], (int, float)) or isinstance(policy[key], bool) or not 0 < policy[key] <= 1440:
            a.add("POLICY_INVALID", f"{key} must be positive and at most 1440", schema=True)
            policy[key] = defaults[key]
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


def audit(control_dir, policy_path, now=None):
    a = Audit(timestamp(now) if isinstance(now, str) else now or datetime.now(timezone.utc))
    directory = Path(control_dir)
    policy = load_policy(a, policy_path)
    doc, digest = a.read(directory / "coordinator-status.json")
    view = coordinator_view(a, doc, policy["max_snapshot_age_minutes"])
    contract = load_contract(a, doc, policy["max_snapshot_age_minutes"])
    directives_check(a, directory, policy, contract)
    defer_input = pending_input_check(a, contract)
    projects = project_checks(a, contract, policy)
    actions = local_actions(a, directory)
    code = 2 if any(d["severity"] == "schema_error" for d in a.diagnostics) else int(bool(a.diagnostics))
    return {
        "status": ("pass", "attention", "schema_error")[code],
        "exit_code": code,
        "scope": "structural_evidence_only",
        "authorizes_actions": False,
        "observed_at": a.now.isoformat(),
        "coordinator_sha256": digest,
        "coordinator": view,
        "projects": projects,
        "defer_input": defer_input,
        "local_actions": actions,
        "diagnostics": a.diagnostics,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-dir", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--now")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    try:
        result = audit(args.control_dir, args.policy, args.now)
    except (ValueError, TypeError, OverflowError):
        result = {
            "status": "schema_error",
            "exit_code": 2,
            "authorizes_actions": False,
            "diagnostics": [{"code": "INPUT_INVALID", "message": "Malformed input; no action authorized"}],
        }
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"Structural preflight: {result['status']}. No actions authorized; evidence references remain unverified."
        )
        for item in result["diagnostics"]:
            print(f"- {item['code']} [{item.get('project') or 'portfolio'}]: {item['message']}")
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
