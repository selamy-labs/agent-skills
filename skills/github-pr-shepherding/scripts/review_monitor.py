"""Repository-scoped review evidence helper (schema version 1). No credentials, posting, verdicts, or model calls.

Input is a snapshot from connected GitHub tools: number -> {pr, reviews, checks}.
The scheduler still invokes a model before this script can inspect that snapshot.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    """Replace atomically; never leave a partial durable ledger."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        temporary = f.name
    os.replace(temporary, path)


@contextmanager
def locked_ledger(path):
    with Path(str(path) + ".monitor.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        yield read(path)


def fingerprint(pr):
    return hashlib.sha256((pr["title"] + "\n" + (pr.get("body") or "")).encode()).hexdigest()


def evaluations(ledger, number):
    return [
        e
        for e in ledger.get("head_evaluations", [])
        if e["number"] == number and e.get("completed", False) and e.get("completed_code_review", True)
    ]


def classify(pr, previous):
    if pr["state"] != "open" or pr["draft"]:
        return "skip_not_ready"
    if not previous:
        return "first_review"
    return classify_followup(pr, previous)


def classify_followup(pr, previous):
    same_head = [e for e in previous if e["head_sha"] == pr["head"]["sha"]]
    same_base = [e for e in same_head if e.get("base_sha") == pr["base"]["sha"]]
    if any(e.get("metadata_fingerprint") == fingerprint(pr) for e in same_base):
        return "skip_unchanged"
    if same_base:
        return "metadata_only"
    if same_head:
        return "base_changed_expand_review"
    if previous[-1].get("base_sha") != pr["base"]["sha"]:
        return "delta_review_expand_base"
    return "delta_review"


def prior_findings(previous):
    # A new SHA/green CI never clears a finding. Only a completed semantic
    # assessment explicitly clearing source does. Preserve the full evidence.
    if not previous or previous[-1].get("source_verdict") == "SHIP":
        return []
    return previous[-1].get("findings", [])


def exact_checks(observation, head):
    checks = observation.get("checks", {}).get("check_runs", [])
    return {
        "fresh": bool(checks) and all(c.get("head_sha") == head for c in checks),
        "checks": [{k: c.get(k) for k in ("id", "name", "head_sha", "status", "conclusion")} for c in checks],
        "limits": "An empty list is no validation evidence; green checks are not source clearance.",
    }


def native_reviews(observation, head):
    return [
        r
        for r in observation.get("reviews", []) or []
        if r.get("user", {}).get("login") == observation["review_actor"]
        and r.get("commit_id") == head
        and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
    ]


def matching_assessments(pr, previous):
    """An assessment applies to this exact code, base and title/body."""
    return [
        e
        for e in previous
        if e["head_sha"] == pr["head"]["sha"]
        and e.get("base_sha") == pr["base"]["sha"]
        and e.get("metadata_fingerprint") == fingerprint(pr)
    ]


def receipt_matches(assessment, native):
    if not assessment.get("submitted"):
        return False
    review_id = assessment.get("review_id")
    url = assessment.get("review_url")
    if review_id is not None:
        return any(str(review_id) == str(r["id"]) for r in native)
    return any(url is not None and url == r.get("html_url") for r in native)


def own_pending_review(observation, head):
    return any(
        r.get("user", {}).get("login") == observation["review_actor"]
        and r.get("commit_id") == head
        and r.get("state") == "PENDING"
        for r in observation.get("reviews", []) or []
    )


def published_receipts(pr, previous, observation):
    assessments = matching_assessments(pr, previous)
    return [
        r for r in native_reviews(observation, pr["head"]["sha"]) if any(receipt_matches(e, [r]) for e in assessments)
    ]


def publication_state(pr, previous, observation):
    """Only native author/commit receipts establish publication, never local completion."""
    history = observation.get("reviews")
    if not isinstance(history, list):
        return "unknown"
    native = native_reviews(observation, pr["head"]["sha"])
    if any(receipt_matches(e, native) for e in matching_assessments(pr, previous)):
        return "published"
    if observation.get("reviews_complete") is False:
        return "unknown"
    if len(history) >= 100 and not observation.get("reviews_complete"):
        return "unknown"
    if native or own_pending_review(observation, pr["head"]["sha"]):
        return "reconcile"
    return "pending"


def classify_with_history(pr, previous, native, publication):
    decision = classify(pr, previous)
    if decision == "skip_unchanged":
        return {
            "published": "skip_unchanged",
            "pending": "publication_pending",
            "unknown": "refresh_native_history",
            "reconcile": "reconcile_native_history_before_publication",
        }[publication]
    if decision == "first_review" and (native or publication == "reconcile"):
        return "reconcile_native_history_before_review"
    return decision


def detect(snapshot, ledger):
    out = []
    active = {r["number"]: r for r in ledger.get("in_progress", [])}
    for supplied in snapshot.values():
        observation = dict(supplied, review_actor=ledger["review_actor"])
        pr = observation["pr"]
        if pr.get("base", {}).get("repo", {}).get("full_name", ledger["repository"]) != ledger["repository"]:
            raise ValueError("Snapshot repository differs from ledger")
        number = pr["number"]
        previous = evaluations(ledger, number)
        latest = previous[-1] if previous else {}
        native = native_reviews(observation, pr["head"]["sha"])
        publication = publication_state(pr, previous, observation)
        out.append(
            {
                "number": number,
                "title": pr["title"],
                "head_sha": pr["head"]["sha"],
                "base_sha": pr["base"]["sha"],
                "metadata_fingerprint": fingerprint(pr),
                "decision": classify_with_history(pr, previous, native, publication),
                "assessment_complete": bool(matching_assessments(pr, previous)),
                "publication_state": publication,
                "published_review_state": next(
                    (r["state"] for r in reversed(published_receipts(pr, previous, observation))),
                    None,
                ),
                "active_claim": active.get(number),
                "unresolved_findings": prior_findings(previous),
                "prior_evidence": [e.get("source_review_evidence") for e in previous[-1:]],
                "prior_metadata_feedback": latest.get("metadata_feedback"),
                "previous_head_sha": latest.get("head_sha"),
                "previous_base_sha": latest.get("base_sha"),
                "previous_review_url": latest.get("review_url"),
                "ci": exact_checks(observation, pr["head"]["sha"]),
                "native_review_ids": [r["id"] for r in native],
            }
        )
    return sorted(out, key=lambda r: r["number"])


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def risk_tags(paths):
    tags = set()
    if len(paths) > 15:
        tags.add("broad_change")
    for path in paths:
        if path.endswith(".proto") or "/contracts/" in path:
            tags.add("public_contract")
        if any(word in path.lower() for word in ("auth", "credential", "migration", "revocation", "concurren")):
            tags.add("security_migration_or_concurrency")
    return sorted(tags)


def evidence_receipts(evidence):
    receipts = []
    for supplied in evidence.get("artifacts", []):
        path = Path(supplied).resolve()
        if not path.is_file():
            receipts.append({"path": str(path), "missing": True})
            continue
        data = path.read_bytes()
        receipts.append(
            {
                "path": str(path),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return receipts


def verify_commit(repo, ref):
    """Accept immutable object identities, never command options or moving refs."""
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", ref):
        raise ValueError("Packet requires full commit object IDs")
    git(repo, "rev-parse", "--verify", ref + "^{commit}")


def make_packet(row, observation, repo, destination, evidence):
    """Collect actual base diff and explicit evidence; never guess callers/specs."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    head, base = row["head_sha"], row["base_sha"]
    for ref in (head, base):
        verify_commit(repo, ref)
    paths = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-only", base + "..." + head).splitlines()
    diff = destination / "current.diff"
    diff.write_text(git(repo, "diff", "--no-ext-diff", "--no-textconv", base + "..." + head))
    previous_head = evidence.get("previous_head_sha", row.get("previous_head_sha"))
    delta = None
    if previous_head:
        verify_commit(repo, previous_head)
        delta = destination / "followup.diff"
        delta.write_text(git(repo, "diff", "--no-ext-diff", "--no-textconv", previous_head, head))
    packet = dict(
        **row,
        generated_at=now(),
        changed_files=paths,
        diff_stat=git(repo, "diff", "--no-ext-diff", "--no-textconv", "--shortstat", base + "..." + head).strip(),
        full_diff=str(diff.resolve()),
        followup_diff=str(delta.resolve()) if delta else None,
        body=observation["pr"].get("body"),
        mergeable=observation["pr"].get("mergeable"),
        mergeable_state=observation["pr"].get("mergeable_state"),
        risk_tags=risk_tags(paths),
        evidence=evidence,
        evidence_receipts=evidence_receipts(evidence),
        reviewer_instruction="Primary assesses risk. Use one independent specialist for risky scope; "
        "inspect CodeGraph callers/contracts before selecting context. Missing evidence is a gap. "
        "Do not infer correctness, resolution, or approval from this packet.",
    )
    write(destination / "packet.json", packet)
    return {
        "number": row["number"],
        "decision": row["decision"],
        "files": len(paths),
        "risk_tags": packet["risk_tags"],
        "packet": str((destination / "packet.json").resolve()),
    }


def claim(path, number, head, owner):
    with locked_ledger(path) as ledger:
        if any(r["number"] == number for r in ledger.get("in_progress", [])):
            raise ValueError("PR already claimed; inspect/resume its recorded work; do not overlap")
        token = str(uuid.uuid4())
        ledger.setdefault("in_progress", []).append(
            {
                "number": number,
                "head_sha": head,
                "owner": owner,
                "claim_token": token,
                "claimed_at": now(),
                "status": "claimed",
            }
        )
        write(path, ledger)
    return token


def release(path, token):
    with locked_ledger(path) as ledger:
        records = ledger.get("in_progress", [])
        if not any(r.get("claim_token") == token for r in records):
            raise ValueError("Unknown claim token; cannot release another review")
        ledger["in_progress"] = [r for r in records if r.get("claim_token") != token]
        write(path, ledger)


def observe(path, rows, snapshot_path):
    with locked_ledger(path) as ledger:
        ledger["monitor_pilot"] = {
            "observed_at": now(),
            "snapshot": str(Path(snapshot_path).resolve()),
            "queue": [
                {
                    k: r[k]
                    for k in (
                        "number",
                        "head_sha",
                        "base_sha",
                        "metadata_fingerprint",
                        "decision",
                        "assessment_complete",
                        "publication_state",
                    )
                }
                for r in rows
                if not r["decision"].startswith("skip_")
            ],
            "skipped": [r["number"] for r in rows if r["decision"].startswith("skip_")],
        }
        write(path, ledger)


def queue_command(args, parser):
    if not all((args.snapshot, args.out)):
        parser.error("detect/packet require --snapshot and --out")
    snapshot = read(args.snapshot)
    rows = detect(snapshot, read(args.ledger))
    if args.command == "detect":
        write(args.out, rows)
        if args.record_observation:
            observe(args.ledger, rows, args.snapshot)
        print(
            json.dumps(
                [
                    {
                        "number": r["number"],
                        "decision": r["decision"],
                        "publication": r["publication_state"],
                        "findings": len(r["unresolved_findings"]),
                        "ci_fresh": r["ci"]["fresh"],
                        "claimed": bool(r["active_claim"]),
                    }
                    for r in rows
                ]
            )
        )
        return
    if not all((args.number, args.repository)):
        parser.error("packet requires --number and --repository")
    row = next(r for r in rows if r["number"] == args.number)
    print(
        json.dumps(
            make_packet(
                row,
                snapshot[str(args.number)],
                args.repository,
                args.out,
                read(args.evidence) if args.evidence else {},
            )
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "detect", "packet", "claim", "release"))
    parser.add_argument("--repo-id", required=True, help="Repository owner/name; one ledger per repository and actor")
    parser.add_argument("--actor", required=True, help="GitHub reviewer login")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--snapshot")
    parser.add_argument("--out")
    parser.add_argument("--number", type=int)
    parser.add_argument("--repository")
    parser.add_argument("--evidence")
    parser.add_argument("--head")
    parser.add_argument("--owner")
    parser.add_argument("--token")
    parser.add_argument("--record-observation", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        # Exclusive creation prevents accidental replacement of existing history.
        with Path(args.ledger).open("x") as target:
            json.dump(
                {
                    "schema_version": 1,
                    "repository": args.repo_id,
                    "review_actor": args.actor,
                    "head_evaluations": [],
                    "in_progress": [],
                },
                target,
            )
        return
    ledger = read(args.ledger)
    if (ledger.get("schema_version"), ledger.get("repository"), ledger.get("review_actor")) != (
        1,
        args.repo_id,
        args.actor,
    ):
        parser.error("Ledger schema, repository or actor mismatch; explicitly migrate a copy of legacy state")
    if args.command == "claim":
        if not all((args.number, args.head, args.owner)):
            parser.error("claim requires --number, --head, --owner")
        print(json.dumps({"claim_token": claim(args.ledger, args.number, args.head, args.owner)}))
        return
    if args.command == "release":
        if not args.token:
            parser.error("release requires --token")
        release(args.ledger, args.token)
        return
    queue_command(args, parser)


if __name__ == "__main__":
    main()
