---
name: portfolio-delivery-control
description: Check a multi-project delivery ledger deterministically at coordinator wakeups, directive intake, worker admission, review readiness, and witness follow-through. Use when projects disappear, ready work ages, evidence conflicts, or parallel work becomes serial. Composes maintained transport and review mechanisms without adding supervisors.
---

# Portfolio delivery control

Requires Python 3.10+, local JSON control records, and installed transport/review helpers. No third-party Python dependencies.

Use one current work ledger and a short deterministic report to make omissions visible. The coordinator still decides decomposition and priority. A report is an inspection of recorded evidence, not proof that the evidence is true or permission to act.

## Run before deciding

Run the bundled script with the absolute control directory and policy selected by primary authority:

```sh
python3 /absolute/portfolio-delivery-control/scripts/preflight.py \
  --control-dir /absolute/control --policy /absolute/control/delivery-policy.json \
  --format json
```

Use `--compact` to emit a bounded, agent-friendly projection (>70% byte reduction) containing ready projects, time-critical blockers, pending intake, and aggregated control gaps. Use `--pane-capture-file <path>` for deterministic one-shot derivation of queued dispatches from a captured tmux pane (captured with `tmux capture-pane -p -J`). Use `--format markdown` for human-readable reports including audit summary counts.

Exit 0 means the recorded structural checks pass, 1 means attention is needed, and 2 means input/schema errors. Preserve the output and its source digest with the coordinator's checkpoint. Never manufacture missing records to obtain green output. Run once per existing coordinator/witness tick and after a material state transition, not after every tool call.

The script reads `coordinator-status.json`, `local-actions.json`, and an optional results file. The normalized current view lives in `coordinator-status.json.delivery_control`; see [the contract](references/contract.md). Existing historical records remain historical. Do not create an independently maintained CSV or spreadsheet. A human-readable export can be generated from the same report.

## Executable workflow management layer (`workflow.py`)

A focused, deterministic, zero-daemon executable workflow-management module `skills/portfolio-delivery-control/scripts/workflow.py` (callable via subcommands `admit`, `claim`, `record-implementation`, `record-review`, `execute-transition` or direct Python API) mutates and transitions work items under an exclusive `flock` on the control directory lockfile (`.workflow.lock`).

### Operations and Subcommands

1. **`admit` (`admit_work`)**:
   - Admit and deduplicate real work from GitHub issues or SpecKit `tasks.md` entries under exclusive lock.
   - Binds a stable `work_id` to `project`, `repository`, immutable `task_id`, `spec_path` (verifying task exists in spec when present), `base_sha` (40-hex SHA), initial stage `admitted`, `prerequisites`, `required_completion_action` (`push`, `publish_pr`, or `merge_pr`; default `"merge_pr"`), and `approval_ref` (`path:sha256`).
   - Validates live approval file on disk: Resolves directive file path, calculates live SHA-256 digest, checks for revocation/withdrawal markers (`WITHDRAWN`, `REVOKED`, `DO NOT PUBLISH`, `"status": "withdrawn"`), and verifies hash match before admitting.
   - Refuses before mutation on duplicate work ID (`DUPLICATE_WORK_ID`), malformed base SHA (`INVALID_BASE_SHA`), missing spec task (`MISSING_SPEC_TASK`), absent/invalid approval (`ABSENT_APPROVAL`, `MISSING_APPROVAL`), stale approval hash (`STALE_APPROVAL`), or withdrawn approval (`APPROVAL_WITHDRAWN`), writing a durable refusal receipt to `<control-dir>/receipts/refusal_<work_id>_<reason>.json`.
   - CLI:
     ```sh
     python3 /absolute/portfolio-delivery-control/scripts/workflow.py admit \
       --control-dir /absolute/control \
       --work-id W-001 \
       --project project-name \
       --repository owner/repo \
       --task-id TASK-001 \
       --base-sha 0123456789abcdef0123456789abcdef01234567 \
       --approval-ref /path/to/directive.md \
       --approval-sha256 <sha256> \
       --required-completion-action merge_pr \
       --spec-path /path/to/tasks.md
     ```

2. **`claim` (`claim_work`)**:
   - Atomically claims admitted work with `owner` (session/lane ID), verified `worktree`, path scope (`write_paths`), generated cryptographic `claim_token`, and TTL-based expiration `expires_at`.
   - Pre-effect refusals:
     - `WRITER_CONFLICT`: Reused active session holding a claim on another active work item ("a reused session cannot count as two simultaneous writers").
     - `COMPETING_ACTIVE_CLAIM`: Overlapping claim on work ID or intersecting write paths.
     - `SCOPE_ESCAPE`: Worktree or write paths outside allowed roots or intersecting protected directories (`skills/portfolio-delivery-control`).
     - `UNMET_PREREQUISITES`: Incomplete prerequisite work items.
   - CLI:
     ```sh
     python3 /absolute/portfolio-delivery-control/scripts/workflow.py claim \
       --control-dir /absolute/control \
       --work-id W-001 \
       --owner lane-session-1 \
       --worktree /path/to/worktree \
       --write-paths src/module/ \
       --allowed-roots /path/to/workspace \
       --ttl-seconds 7200
     ```

3. **`record-implementation` (`record_implementation`)**:
   - Binds implementation round, commit SHA, tree SHA, parent SHA, patch/bundle/receipt artifacts, and gate results (`test_exit_code == 0`) to an active claim.
   - Verifies the live git repository at `worktree` (`git rev-parse HEAD`, `git rev-parse HEAD^{tree}`, `git rev-parse HEAD^`, `git rev-list --count base_sha..HEAD`, and commit identity `selamy-labs-bot`).
   - Refuses on git mismatches (`HEAD_MISMATCH`, `TREE_MISMATCH`, `BASE_SHA_DRIFT`, `NON_SINGLE_COMMIT_RANGE`, `INVALID_COMMIT_IDENTITY`), artifact hash mismatches (`ARTIFACT_HASH_MISMATCH`), or missing mandatory gates (`MISSING_MANDATORY_GATE`).
   - Automatically invalidates prior reviews and transitions if `commit_sha` or `tree_sha` differs from previous candidate revision.
   - Advances stage to `review` and records durable implementation receipt.
   - CLI:
     ```sh
     python3 /absolute/portfolio-delivery-control/scripts/workflow.py record-implementation \
       --control-dir /absolute/control \
       --work-id W-001 \
       --owner lane-session-1 \
       --claim-token <token> \
       --round 1 \
       --commit-sha <40-hex> \
       --tree-sha <40-hex> \
       --parent-sha <40-hex> \
       --gate-results '{"test_exit_code": 0}'
     ```

4. **`record-review` (`record_review`)**:
   - Binds an independent review report (`reviewer_id`, `model`, `family` in `argon`, `opus`, `sol`, `session_id`, `verdict` in `SHIP`, `REVISE`, `report_path`, `report_sha256`) to the candidate.
   - Enforces strict reviewer provenance: Refuses unattributed reports lacking non-empty model or reviewer attestation (`REPORT_IDENTITY_MISMATCH`).
   - Refuses duplicate report reuse across reviewers on the current revision (`REPORT_IDENTITY_MISMATCH`).
   - Enforces embedded self-review refusal: Inspects report tokens (`session`, `session_id`, `conversationId`, `authorModel`, `reviewer`, `model`, `dispatch`, `attestation`) against implementer identity, claim owner, candidate model, and flash models (`SELF_REVIEW_FORBIDDEN`).
   - Verifies embedded reviewer consistency against caller session, reviewer, account, and dispatch (`REPORT_IDENTITY_MISMATCH`).
   - Validates report file SHA-256 and verified commit/tree match candidate.
   - Refuses forbidden models (`claude-fable-5-1`), duplicate reviewer family/session per round, or revision drift.
   - `REVISE` verdict reverts stage to `implementing` with `remediation_required: true` and records actionable defect IDs.
   - 3 distinct reviewer families (`argon`, `opus`, `sol`) and 3 distinct sessions with `SHIP` marks candidate `qualified: true`.
   - CLI:
     ```sh
     python3 /absolute/portfolio-delivery-control/scripts/workflow.py record-review \
       --control-dir /absolute/control \
       --work-id W-001 \
       --reviewer-id rev-opus \
       --model claude-3-7-sonnet \
       --family opus \
       --session-id sess-rev-1 \
       --verdict SHIP \
       --report-path /path/to/report.json \
       --report-sha256 <sha256>
     ```

5. **`execute-transition` (`execute_transition`)**:
   - Executes guarded side effects (`push`, `publish_pr`, `merge_pr`, `complete_spec_task`) through maintained helpers with provider reconciliation.
   - Pre-effect guards: Validates claim freshness, live git revision matching qualified candidate, 3 unanimous `SHIP` reviews, mandatory gates, live primary operator approval verification on disk (checking hash match and absence of withdrawal markers), exact-head CI, and approved bot identity (for `merge_pr`).
   - Provider-authoritative merge CI: Evaluates `provider_checks` (`statusCheckRollup` from `gh pr view`). Requires non-empty, green rollup (`MISSING_EXACT_HEAD_CI`). Caller `ci_checks` cannot override provider CI and must explicitly match `commit_sha`.
   - Provider actor identity verification: Calls `gh api user` and enforces actor identity matching `ALLOWED_MERGE_IDENTITIES` (`selamy-labs-bot`), refusing unauthorized human/bot actors with `DISALLOWED_MERGE_IDENTITY`.
   - Already-merged reconciliation verification: Verifies `headRefOid == commit_sha` and valid provider CI before returning reconciled success, recording `merge_commit_sha`.
   - Revision-bound transitions: Requires prior transitions matching `required_completion_action` bound to the current candidate commit and tree (`MISSING_TRANSITION_EVIDENCE`).
   - Updates `coordinator-status.json` with truthful CI/branch protection projection and writes durable transition receipts.
   - CLI:
     ```sh
     python3 /absolute/portfolio-delivery-control/scripts/workflow.py execute-transition \
       --control-dir /absolute/control \
       --work-id W-001 \
       --action push \
       --claim-token <token> \
       --branch feat/my-branch
     ```

## Resolve the report

1. **Coverage:** Every primary-authorized project has an owner, next action, current stage, dated progress evidence, or an explicit blocker/wait. Priority for one foundation does not remove other projects. Unresolved product scope can advance through a bounded discovery artifact; it does not authorize invented implementations or deployments. Legacy entries are projected with RFC 6901 source pointers, canonical SHA-256 digests, and verified fact age.
2. **Intake:** Read new primary directives, reconcile superseded instructions, record their exact file hashes, and verify actual application. A hash proves which file was read, not compliance. Keep a compact current checkpoint with the next consequential decisions.
3. **Parallelism:** Admit independent ready slices using available authorized implementation and review capacity. Give each writer a verified worktree and path scope. Stage transition guards enforce strict admission keys, claim fencing (`claim_token`), and distinct reviewer model roles. Keep review/integration capacity available. Count advancing artifacts and stage transitions, not session names or idle processes. Preserve valid long waits and advancing work.
4. **Evidence:** Distinguish implementation, review, push, protected merge, green post-merge CI, artifact publication, and hosted validation. A fresh root timestamp does not refresh old nested facts. Bind review and CI receipts to the exact revision; verify live state before an irreversible action.
5. **Follow-through:** For each nudge, record the cause, expected next observable result, owner and recheck time. Recheck action/result, not just acknowledgement. Stop repeating a nudge already queued or being handled.

6. **Quality coverage:** Reconcile every `required_controls` entry to each project's `controls` record. Read the canonical definition, check applicable repository enforcement and exact-revision evidence, and retain an owned next task for gaps. A checked box, hook configuration or self-reported status is insufficient proof. Preserve both successful execution and meaningful rejection evidence where the baseline requires them. Use narrow justified non-applicability, never blanket exemptions.

Treat unrelated deficits independently. A missing project row should prompt admission/reconciliation, not halt an unrelated productive worker. Missing authorization or failed prerequisites block the specific action they govern. A passing preflight never grants cloud apply, paid execution, permission bypass, model access, merges outside primary approval, or secret/custody changes.

## Compose maintained mechanisms

Use the installed versions of these skills rather than reimplementing their mechanisms:

- `low-level-executor-task-spec`: immutable task scope, allowed paths, acceptance criteria and evidence contract.
- `orchestrate-agy`, `orchestrate-claude`, `orchestrate-codex`: installed-capability checks, safe execution, unique dispatch and verified read.
- `pr-ci-shepherding` / `github-pr-shepherding`: exact-revision review, required checks and protected landing.
- `verify-from-system-of-record` / `verify-real-artifact`: verify claims against repository/provider evidence.
- `loop-governance-and-learning`: turn repeated failures into regression cases for this preflight or the owning repository gate.

Keep composition shallow: preflight → bounded decision → maintained action helper → independent verification → checkpoint. No recursive supervisors, self-spawning skills, extra schedules, or general command-string security parser.

## Primary roles and safeguards

Read the current policy rather than assuming roles from a historical status field. The coordinator owns admission and integration, the witness observes the coordinator, and admitted workers implement or remediate. Current primary policy selects the implementation model and independent reviewer roles for each exact revision. A coauthor trailer, audit or model nickname alone does not establish implementation provenance.

Use the existing coordinator input lock and a single-line directive-file pointer. If any relevant input is queued, the lock is busy, or a helper result is ambiguous, inspect the same dispatch and defer new input. Do not re-paste. Preserve the same conversation when recovering.

No script here dispatches workers, starts timers, changes permissions, merges, deploys or mutates the ledger. Required server-side branch checks, restricted credentials, sandboxing and explicit live-apply approval remain independent boundaries.

## Adoption and limits

Verify four separate steps: tests pass; the installed harness discovers this skill; the current conversation actually invokes the script; observed project outcomes improve. Merely installing or reading it is not adoption.

Antigravity documentation describes hooks, but the installed version is the contract. Before any hook enforcement, test discovery, payload, deny, malformed output, crash and timeout behavior in a harmless isolated probe. Do not install an untested hook on productive sessions or claim this read-only script prevents arbitrary bypasses. If hooks are unsupported, invoke explicitly from the existing wakeup contract and let the independent witness verify freshness.

## Verify changes

```sh
python3 -m unittest discover -s /absolute/portfolio-delivery-control/tests -v
```

Keep incident-based tests for missing collections, stale revisions, omitted projects, valid versus expired waits, conflicting writers, unsafe custody/permissions, duplicate input and already-fulfilled actions. Test decisions and outputs, not source spelling or handwritten inventory counts.
