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
