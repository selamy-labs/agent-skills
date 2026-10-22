# Review evidence helper, version 1

Run `python3 scripts/review_monitor.py --help` from this skill directory.
Every invocation requires `--ledger PATH --repo-id OWNER/REPO --actor LOGIN`.
Use `init` once to create an empty ledger exclusively. Each repository/actor pair
has its own ledger. Existing ledger schema/identity mismatches are rejected.
For migration, back up the old ledger and explicitly add `schema_version: 1`,
`repository` and `review_actor` to a copy after verifying its history and ownership.
Do not discard historical findings or copy another repository's observations.

`detect --snapshot SNAPSHOT --out REPORT` accepts the existing pilot shape:
a JSON object keyed by PR number, each value containing GitHub `pr`, optional
`reviews`, `reviews_complete`, and `checks.check_runs` observations. Enumerate all
PR/review pages through the existing connector; missing/paginated history is not
proof of absence. GitHub PR fields include number, title, body, state, draft,
head.sha and base.sha. Include base.repo.full_name to validate source identity.
Snapshots without repository metadata must be independently verified by the caller.
The ledger uses `head_evaluations` and `in_progress` arrays, preserving unknown fields.
Evaluations bind number, head_sha, base_sha, metadata_fingerprint and completion;
submitted publication additionally needs a native review id/url, author and commit receipt.

`claim --number N --head SHA --owner ID` returns an exclusive claim token.
`release --token TOKEN` releases only that claim; it does not mark completion.
Claims do not expire automatically. Inspect/resume interrupted work.
`packet --snapshot SNAPSHOT --out DIRECTORY --number N --repository CHECKOUT`
collects actual base and prior-head diffs. Optional `--evidence MANIFEST` takes
an `artifacts` array of paths and explicit scope/limits. Detailed output remains
in files. `--record-observation` records routing without overwriting assessments.

Before publication refresh actor, head/base, draft/open state, metadata, review
history and required checks. A skipped source review still requires inspection
of current CI and integration dependencies. A packet is not an approval, and
unchanged code does not establish current requirements or deployment validity.
This helper neither posts reviews nor calls a model. A model heartbeat still
incurs its initial model invocation. POSIX file locking is required.
