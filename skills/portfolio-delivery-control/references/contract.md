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

A valid wait has `wait: {until, reason, evidence}`. A blocker has `blocker: {owner, reason, evidence}`. Evidence is a nonempty list of references. Use a real recheck time for an authorized wait; do not roll it forward to conceal inactivity. A blocked row is visible work, not a claim that the whole project must stop: admit independent slices where appropriate.

`merge_ready` additionally requires:

- `revision`: full 40-character commit SHA.
- `reviews`: one independent `{role, reviewer, revision, verdict: "SHIP", evidence}` for each required role. Distinct reviewer identities mean actual independent agent sessions, not invented GitHub users.
- `ci: {revision, status: "success", evidence}`.
- `branch_protection: {verified: true, evidence}`.
- `approval: {authority}` referencing the exact applicable primary approval scope.

These fields are necessary structural checks, never sufficient authorization. The maintained landing path still verifies live head/base, check-run identity, review contents, effective branch rules and approval applicability. A reference string does not make a provider fact true. Source-merge approval is not infrastructure/deployment approval.

`pending_inputs` is required; record `[]` only after observing that no input is pending. Entries require `id`, `status` and nonempty evidence references. Statuses `queued`, `composer`, or `submitted_unverified` defer new input; other recognized states are `read_verified`, `consumed`, `completed` and `failed`. Missing or malformed queue state remains unknown and defers new input. The maintained transport lock/verifier remains authoritative at dispatch time.

The policy file contains `expected_projects`, age thresholds, the required reviewer roles, primary implementer model and directive `{path,sha256}` records. Policy documents point to primary instructions and the maintained quality baseline; this script does not try to infer policy semantics from prose or replace repository-specific baseline tests.

`required_review_roles` and `implementer_model` are explicit required policy fields; the reusable script does not choose providers or models. Optional `required_controls` lists the control IDs from the governing baseline. Each project supplies `controls[id]` with `status` (`enforced`, `implementing`, `gap`, or `not_applicable`), `owner`, `next_action`, `observed_at`, and evidence references. An enforced control requires a full revision equal to the project row's revision. Non-applicability requires a concrete reason and evidence. Missing/unfinished controls remain visible gaps; independent productive work continues. These records index canonical definitions, not a second specification or a substitute for checking actual rejection and successful execution.

The report is bounded structural evidence. Store its source digest and observation time with the checkpoint. Resolve contradictory legacy aliases instead of periodically refreshing them with old values. Refresh each project's evidence only after checking that project; fresh root timestamps never imply fresh project facts.
