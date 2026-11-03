# Current delivery view

This is an additive normalized view in the existing `coordinator-status.json`, maintained by its existing single writer. It replaces ambiguous current-state aliases, not the historical evidence. Populate facts from current receipts; do not copy the example as live state.

```json
{
  "delivery_control": {
    "schema_version": 1,
    "observed_at": "2026-11-03T00:30:00Z",
    "directives": [
      {"path": "primary-directive.md", "sha256": "full SHA256", "applied_at": "2026-11-03T00:30:00Z"}
    ],
    "pending_inputs": [],
    "projects": {
      "example": {
        "owner": "existing coordinator / admitted slice owner",
        "stage": "implementing",
        "next_action": "Produce the bounded implementation receipt",
        "observed_at": "2026-11-03T00:30:00Z",
        "last_progress_at": "2026-11-03T00:29:00Z",
        "evidence": ["accepted-dispatch-and-runtime-model-receipt.json"],
        "workers": [
          {"id": "unique admitted worker", "role": "implementation", "model": "approved-implementation-model", "user": "worker-owner", "worktree": "/isolated/project/worktree", "write_paths": ["src/component/"], "sandbox": true, "permission_bypass": false, "authority": "primary-directive.md"}
        ]
      }
    }
  }
}
```

Stages: `discovery`, `ready`, `implementing`, `review`, `ci`, `merge_ready`, `waiting`, `blocked`, `done`.

## Waits and Blockers

- A valid wait has `wait: {until, reason, evidence}`.
- A blocker has `blocker: {owner, reason, evidence, [recheck_at | recheck_by | until], [since | blocked_at | observed_at]}`.
- Evidence is a nonempty list of references.
- Recheck deadlines: `recheck_at` (or aliases `recheck_by`, `until`) defers stale blocker attention until the specified deadline. Any `null` values are skipped in alias lookup.
- Origin timestamps: `since` (or aliases `blocked_at`, `observed_at`) specifies when the blocker was observed; if omitted, it falls back to the row's `last_progress_at`.
- Policy horizon: `max_recheck_horizon_hours` (default 168 hours = 7 days) defines the maximum allowable future recheck horizon on stale blockers. Fresh blockers within active progress window are not bounded by recheck horizon.
- Blocker diagnostic codes:
  - `BLOCKER_STALE`: Blocker progress exceeds policy `max_progress_age_minutes` without an active recheck deadline.
  - `BLOCKER_RECHECK_DUE`: Recheck deadline has elapsed (`<= now`).
  - `BLOCKER_RECHECK_HORIZON_EXCEEDED`: Stale blocker has a recheck deadline exceeding `max_recheck_horizon_hours` in the future.
  - `BLOCKER_INVALID`: Blocker object missing required fields or has an unparseable timestamp.
- Inactive blockers:
  - Rows with stage `done` or `complete` skip blocker enforcement (treating historical blockers as resolved).
  - Non-blocked rows (`stage != "blocked"`) with `blocker: null` or `blocker: {}` treat the blocker as absent with 0 diagnostics.
  - Blocked rows (`stage == "blocked"`) with missing, null, or empty blockers emit `BLOCKER_INVALID`.

## Coverage and Provenance

Each project listed in policy `expected_projects` is evaluated for coverage:

- `normalized`: Project has a valid row object in `delivery_control.projects`.
- `legacy_unverified`: Project is absent from `delivery_control.projects` but located in legacy `initiatives` or `projects`. The projection includes:
  - `source_pointer`: RFC 6901 JSON pointer (e.g. `/initiatives/owner~1repo`, `/projects/0`), escaping `~` as `~0` and `/` as `~1` for object key tokens while retaining numeric list indices.
  - `source_sha256`: Canonical SHA-256 digest of the source object (`sort_keys=True, separators=(",", ":")`).
  - `observed_at`: Extracted timestamp from legacy record, skipping null/empty candidates to find the earliest valid timestamp key.
  - `age_seconds`: Elapsed integer seconds between observation and audit `now`. If unparseable, non-string, or in the future (`age_seconds < 0`), `observed_at_valid: false` is set.
  - Legacy dict entries with `null` or non-dict values are reported as `legacy_unverified` (never `absent` or `PROJECT_MISSING`).
- `invalid`: Project has a malformed project row (not an object) or `delivery_control.projects` container is malformed.
- `absent`: Project is missing from both normalized and legacy records (`PROJECT_MISSING`).

## Summary and Compact Projection

- `summary` provides aggregate counts:
  - `normalized`: Count of valid normalized dict project rows.
  - `legacy_unverified`: Count of legacy projects.
  - `absent`: Count of absent projects.
  - `invalid`: Count of malformed project rows.
  - `coverage_counts`: Mapping of all four coverage status counts (`normalized`, `legacy_unverified`, `absent`, `invalid`).
  - `diagnostic_counts`: Counts of `schema_error` and `attention` diagnostics.
  - Included in both `--format json` and `--format markdown` outputs.
- `compact_projection` (available via `--compact` CLI flag; `--full` emits full diagnostics):
  - `status`: Overall audit status (`pass`, `attention`, `schema_error`).
  - `exit_code`: Numeric exit code (0, 1, 2).
  - `authorizes_actions`: Always `false`.
  - `summary`: Full summary count dictionary.
  - `changed_or_ready_projects`: Active or ready project rows (with `claim_token` redacted).
  - `time_critical_blockers`: Blockers with active recheck deadlines, `not_before` constraints, or stale status.
  - `pending_intake`: Queue state and `defer_input` flag.
  - `aggregated_gaps`: Control gaps grouped by `(project, status)` with RFC 6901 drilldown pointers; absent projects are explicitly included with `status: "absent"`.
  - `transition_failures`: Guard failure diagnostics with `next_permissible_action`.
  - All string fields are bounded to at most 240 characters (with `...` truncation) and lists bounded to at most 50 items.

## Work Item Metadata and Stage Transition Guards

Projects may include a `work_item` object:
```json
{
  "admission_key": "unique-task-key",
  "issue_or_task": "task-reference",
  "attempt": 1,
  "claim_token": "fenced-claim-token",
  "requested_transition": "admission | implementation | review | merge | postmerge | complete",
  "claim_expires_at": "2026-11-03T02:00:00Z",
  "not_before": "2026-11-03T01:15:00Z",
  "revision": "40-char-sha",
  "evidence": ["artifact.json"]
}
```

- `admission_key`: Required non-empty string identifier for admitted tasks; conflicting admission keys across active non-done projects raise `DUPLICATE_ADMISSION`.
- `attempt`: Optional positive integer tracking task execution attempts; non-integer or `<= 0` values raise `WORK_ITEM_INVALID`.
- `claim_token`: Required for active implementation and review stages; verified against expiration and staleness (`STALE_OR_MISSING_CLAIM`). Omitted/redacted from reported projections.
- Evidence format validation: Evidence lists are format-checked as non-empty lists of non-blank strings; placeholder, todo, unverified, dummy, none, and leetspeak variations (e.g. `pl4ceholder`) are rejected as forged (`INSUFFICIENT_COMPLETION_EVIDENCE`).

### Transition Guards and Stage Auto-enforcement

- `requested_transition`: Optional field on `row` or `work_item` (`admission`, `implementation`, `review`, `merge`, `postmerge`, `complete`). Any falsy or invalid transition value (`""`, `0`, `[]`, unknown) emits `STAGE_TRANSITION_REJECTED`. Conflicting values between `row` and `work_item` are rejected.
- Stage-based auto-enforcement:
  - `stage == "review"`: Runs `guard_review`.
  - `stage == "merge_ready"`: Runs `guard_merge_or_complete(target="merge")` when `work_item`, `reviews`, `ci`, or `requested_transition` is present.
  - `stage == "done"`: Runs `guard_merge_or_complete(target="complete")` when `work_item`, `reviews`, `ci`, or `requested_transition` is present.
- Revisions: Valid 40-hex commit SHA (`is_hex_sha`) required for `review`, `merge`, and `complete`. Disagreement between `work_item.revision` and `row.revision` raises `REVISION_CHANGED_AFTER_REVIEW` and `TRANSITION_GUARD_FAILED`. `None == None` never passes revision matching.
- Preconditions by transition:
  - `admission`: Requires `issue_or_task` and non-empty `admission_key`.
  - `implementation`: Requires assigned approved implementer worker (`model == policy.implementer_model`) and valid `claim_token`.
  - `review`: Requires `len(roles)` distinct reviewer models from `policy.reviewer_models` on exact 40-hex revision, with valid evidence and verdicts in `{"SHIP", "REVISE"}`.
  - `merge` / `postmerge` / `complete`: Requires unanimous SHIP reviews from required roles, non-forged completion evidence, and all mandatory gates satisfied.
- Unanimous SHIP: Every required role in `required_review_roles` must have exactly one matching review with `verdict == "SHIP"`, `revision` equal to the 40-hex target SHA, valid format-checked evidence, distinct `reviewer` identities, and `model` matching `policy.reviewer_models` (and not in `banned_models`). Any review on target revision with verdict other than `"SHIP"` (e.g. `"REVISE"`) rejects unanimity.
- Mandatory Gates: If `policy.required_controls` is non-empty and `controls` is missing or not a dict, fails closed by reporting all required controls as missing (`MISSING_MANDATORY_GATE`). For `enforced` controls, requires target 40-hex SHA match and valid evidence. For `not_applicable` controls, requires non-empty reason and valid evidence.

## Reviewer Workers and Artifact Exports

Workers may specify `role: "review"`:
- Reviewers are checked against policy `reviewer_models` and `banned_models`. Policy `reviewer_models` must be declared and non-empty when review workers are present (`REVIEWER_MODEL_INVALID`).
- Reviewer workers must declare `write_paths: []` (strictly read-only on repository paths) and non-empty `artifact_exports` (`WORKER_INVALID`).
- Cross-user home directory escapes are forbidden (`CROSS_USER_PATH`).

## Merge Gates

`merge_ready` additionally requires:

- `revision`: full 40-character commit SHA.
- `reviews`: one independent `{role, reviewer, revision, verdict: "SHIP", evidence}` for each required role. Distinct reviewer identities mean actual independent agent sessions, not invented GitHub users.
- `ci: {revision, status: "success", evidence}`.
- `branch_protection: {verified: true, evidence}`.
- `approval: {authority}` referencing the exact applicable primary approval scope.

These fields are necessary structural checks, never sufficient authorization. The maintained landing path still verifies live head/base, check-run identity, review contents, effective branch rules and approval applicability. A reference string does not make a provider fact true. Source-merge approval is not infrastructure/deployment approval.

## Directives and Queue Controls

`pending_inputs` is required; record `[]` only after observing that no input is pending. Entries require `id`, `status` and nonempty evidence references. Statuses `queued`, `composer`, or `submitted_unverified` defer new input; other recognized states are `read_verified`, `consumed`, `completed` and `failed`. Missing or malformed queue state remains unknown and defers new input. The maintained transport lock/verifier remains authoritative at dispatch time.

### Deterministic Pane Queue Derivation

Preflight supports pure one-shot pane queue-derivation via `derive_pending_inputs_from_pane(pane_text, observed_at, evidence_ref)` and the `--pane-capture-file <path>` CLI option:

- **Pure one-shot derivation**: Evaluates pane text without polling loops, sleep delays, background daemons, or modifying `coordinator-status.json` on disk. Capture panes using `tmux capture-pane -p -J` to join wrapped lines, though preflight also normalizes soft-wrapped `[dispatch:...]`, `[dispatch-end:...]`, and `[dispatch-accepted:...]` tokens split across lines without `-J`.
- **Dispatch token matching**: Matches timestamped dispatch tokens using regex pattern `\[dispatch:(agy-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_.:-]+)\]`, ignoring prose placeholders like `[dispatch:agy-...]`.
- **Standalone acceptance lines**: Matches standalone acceptance lines using multiline regex `^\s*\[dispatch-accepted:(agy-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_.:-]+)\]\s*$`. Any acceptance token appearing inside any prompt block (closed or unclosed) is treated as prompt text, not an assistant acceptance; acceptance is only recognized when it appears on a standalone line after the corresponding `[dispatch-end:<id>]` closing marker (and outside any prompt block). An unclosed prompt block lacking its closing marker never self-accepts and remains queued.
- **Deduplicated ordered intake**: Dispatches seen in pane text are deduplicated in order of appearance; any dispatch ID not present in the standalone accepted set is returned with `status: "queued"`, `observed_at`, and `evidence: [evidence_ref]`.
- **Terminal state preservation**: Merging derived entries never moves a recorded terminal entry (`consumed`, `completed`, `read_verified`, `failed`) back to `queued`.
- **CLI integration (`--pane-capture-file`)**: When `--pane-capture-file <path>` is passed to `preflight.py` or `run_preflight`, it reads the pane capture file (bounded to 2 MiB) and merges derived queued entries (by `id`) into the evaluated `pending_inputs` list before checking queue deferral.
- **Fail-closed on unreadable pane file**: If `--pane-capture-file` is explicitly specified but the file is missing, unreadable, or exceeds 2 MiB, preflight emits a `PENDING_INPUT_UNKNOWN` diagnostic and sets `defer_input: true`.

The policy file contains `expected_projects`, age thresholds, `max_recheck_horizon_hours`, the required reviewer roles, primary implementer model, and directive `{path,sha256}` records. Policy documents point to primary instructions and the maintained quality baseline; this script does not try to infer policy semantics from prose or replace repository-specific baseline tests.

`required_review_roles` and `implementer_model` are explicit required policy fields; the reusable script does not choose providers or models. Optional `required_controls` lists the control IDs from the governing baseline. Each project supplies `controls[id]` with `status` (`enforced`, `implementing`, `gap`, or `not_applicable`), `owner`, `next_action`, `observed_at`, and evidence references. An enforced control requires a full revision equal to the project row's revision. Non-applicability requires a concrete reason and evidence. Missing/unfinished controls remain visible gaps; independent productive work continues. These records index canonical definitions, not a second specification or a substitute for checking actual rejection and successful execution.

The report is bounded structural evidence. Store its source digest and observation time with the checkpoint. Resolve contradictory legacy aliases instead of periodically refreshing them with old values. Refresh each project's evidence only after checking that project; fresh root timestamps never imply fresh project facts.

## Workflow Management Layer and Receipts (`workflow.py`)

The executable layer maintains `coordinator-status.json.delivery_control.work_items` and projects worker state into `delivery_control.projects`:

```json
{
  "delivery_control": {
    "work_items": {
      "W-001": {
        "work_id": "W-001",
        "project": "example-project",
        "repository": "owner/repo",
        "task_id": "TASK-001",
        "spec_path": "/path/to/tasks.md",
        "base_sha": "0123456789abcdef0123456789abcdef01234567",
        "stage": "review",
        "prerequisites": [],
        "approval_ref": "/path/to/directive.md:sha256",
        "admitted_at": "2026-11-03T01:00:00Z",
        "owner": "lane-session-1",
        "claim_token": "cryptographic-hex-token",
        "worktree": "/path/to/worktree",
        "write_paths": ["src/module/"],
        "claimed_at": "2026-11-03T01:05:00Z",
        "claim_expires_at": "2026-11-03T03:05:00Z",
        "candidate": {
          "round": 1,
          "commit_sha": "1111111111111111111111111111111111111111",
          "tree_sha": "2222222222222222222222222222222222222222",
          "parent_sha": "0123456789abcdef0123456789abcdef01234567",
          "patch_path": "/path/to/patch.patch",
          "bundle_path": "/path/to/bundle.bundle",
          "receipt_path": "/path/to/receipt.json",
          "gate_results": {"test_exit_code": 0},
          "recorded_at": "2026-11-03T01:30:00Z"
        },
        "reviews": [
          {
            "reviewer_id": "rev-opus",
            "model": "claude-3-7-sonnet",
            "family": "opus",
            "session_id": "sess-rev-1",
            "verdict": "SHIP",
            "report_path": "/path/to/report.json",
            "report_sha256": "sha256-hex",
            "recorded_at": "2026-11-03T01:35:00Z"
          }
        ],
        "qualified": true,
        "remediation_required": false,
        "defects": [],
        "transitions": [
          {
            "action": "push",
            "reconciled_existing_effect": false,
            "transitioned_at": "2026-11-03T01:40:00Z"
          }
        ]
      }
    }
  }
}
```

### Durable Receipts

All workflow actions and pre-effect refusals record durable JSON receipts in `<control_dir>/receipts/`:

- `admission_<work_id>.json`: Recorded on successful work admission.
- `refusal_<work_id>_<reason_code>.json`: Recorded when admission, claim, implementation, review, or transition is refused.
- `claim_<work_id>.json`: Recorded on successful atomic claim with token and expiry.
- `implementation_<work_id>_r<round>.json`: Recorded when implementation candidate and artifacts satisfy all verification checks.
- `review_<work_id>_<family>_<verdict>.json`: Recorded when an independent review report is verified and attached.
- `transition_<work_id>_<action>.json`: Recorded when an external transition (`push`, `publish_pr`, `merge_pr`, `complete_spec_task`) is executed or reconciled with provider state.

### Review Report Provenance & Unsigned Local Report Trust Boundary

Review reports are read from local filesystem JSON receipts without public-key or GPG cryptographic signatures. To protect against malicious, unverified, or fabricated reviews within this boundary, the workflow layer enforces strict structural and provenance constraints:

1. **Canonical Reviewer Model Allowlist**: Reviewer model identifiers must strictly match a known canonical model per family (`opus`, `argon`, `sol`):
   - `opus`: `claude-opus-5-5`, `claude-opus-5.5`, `claude-opus-5`, `claude-opus-4-6`, `claude-opus-4.6`, `claude-opus-4-5`, `claude-opus-4.5`, `claude-opus-4`, `claude-3-opus`, `claude-3-opus-20240229`.
   - `argon`: `gemini-4.0-argon-high`, `gemini-4.0-argon`, `gemini-4-argon-high`, `gemini-4-argon`, `gemini-4.5-argon-high`, `gemini-4.5-argon`, `gemini-4-argon-thought`.
   - `sol`: `gpt-6.1-sol-high`, `gpt-6.1-sol`, `gpt-6-sol-high`, `gpt-6-sol`, `gpt-5.4`, `gpt-5.4-sol`, `gpt-5.3-codex`, `gpt-5-sol`.
   Arbitrary model substrings (e.g. `argon-by-anyone`) are rejected with `REPORT_IDENTITY_MISMATCH`.
2. **Provenance Metadata**: Every review report JSON must contain a non-empty provenance field: `dispatch` (or `dispatch_id`), `session_id` (or `session`), or `attestation` / `sha256`. Reports lacking provenance are refused with `REPORT_IDENTITY_MISMATCH`.
3. **Anti-Self-Review and Session Isolation**: Embedded report tokens are inspected against claim owners, implementer sessions, candidate models, and flash models to forbid self-review (`SELF_REVIEW_FORBIDDEN`) and detect conflicting session or account IDs (`REPORT_IDENTITY_MISMATCH`).
4. **Local Trust Boundary Contract**: Until reviews carry verifiable cryptographic signatures (e.g. Sigstore or repository-trusted GPG keys), trust is scoped to the authenticated coordinator process, POSIX filesystem permissions (0644/0755), and coordinator-level atomic file locking.
