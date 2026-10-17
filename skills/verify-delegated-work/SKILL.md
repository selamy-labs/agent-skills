---
name: verify-delegated-work
description: Use when accepting, merging, or building on work produced by a sub-agent, delegated worker, or any process you did not run yourself. A worker's done, idle, or green-CI signal is a claim, not proof — independently verify the real artifact first.
---

# Verify Delegated Work

Delegation multiplies output, and it multiplies unverified claims with it. The
worker's report is the cheapest, most plausible thing to accept and the most
expensive thing to accept wrongly. A "done", "idle", "available", or "CI is
green" signal from a delegated worker is a claim about the work, not the work.
Before you accept it, build on it, or merge it, verify the real artifact
yourself.

This is the multi-worker complement to verifying your own output: the gap here
is not that you skipped verification, but that you trusted *someone else's*
verification report instead of producing your own.

## The Rule

Treat every completion signal from a delegated worker as untrusted input.
Re-derive the result from the real artifact before it counts as done. The
cheapest place to catch a wrong-but-plausible result is before you merge it or
chain more work onto it.

## Why Worker Signals Mislead

- **"Done" is self-graded.** The worker is reporting its own belief about its
  own output, often optimistically and without an independent check.
- **"CI is green" is a partial exercise.** A unit suite can go green even when
  the artifact fails to launch, fails to install, or breaks a path CI never
  exercised.
- **"Idle" / "available" is not a completion report.** An async worker's
  availability often fires at dispatch or mid-run, before the result exists.
- **Pasted output is editable and stale.** A pasted test log is not the same as
  the test running now, on the merged tree.
- **Plausible is not correct.** Delegated output tends to look finished; that
  surface polish is exactly what makes an unverified wrong result dangerous.

## Verification Checklist

Independently inspect the checks and artifact that fit the task; do not stop at a worker summary. Verification does not require repeating identical expensive proof when a trusted system of record already binds it to the exact source tree, tool/configuration versions, relevant inputs, and environment, and governing repository policy permits reuse. Live or time-sensitive behavior still needs current evidence at its real boundary.

If a reviewer cannot continue, fill the required role through the existing
review mechanism with available, authorized, qualified capacity. Preserve the
required review count, independence from authors and each other, and explicit
model, provider or named-reviewer gates; a session handle alone need not bind
the role. Carry forward findings, original verdicts and actual reviewer/model
provenance. Distinguish unfinished review from completed old-head review; retain
the latter as historical evidence and refresh affected review for the current
head under repository policy. Bind the successor's review to its actual
identity/model and the head reviewed, preserving prior review history.

- **Verify authoritative check evidence yourself.** Read actual run IDs, exact
  heads/trees, commands, and terminal results from the system of record. Rerun
  affected checks when evidence is missing, stale, opaque, or bound to different
  code, tools, configuration, inputs, or environment, or when policy requires it.
  A worker's pasted output alone cannot establish any of these facts.
- **Read the real diff.** For anything security- or safety-sensitive — shell or
  process execution, file writes, credential or secret handling, network
  egress, deletes — read the code and confirm the claimed guard actually exists
  in the diff, not just in the worker's summary.
- **Launch the produced artifact.** Install it, start the server, hit the
  endpoint, open the page, run the binary. A green unit suite routinely coexists
  with a packaging, dependency, or wiring defect that only a real launch
  reveals.
- **Check acceptance against current authority.** Compare the artifact and
  tests with the governing spec or user requirement. Tests that restate the
  implementation can agree with each other while missing the intended behavior.
- **Confirm the file list is exactly the intended change.** No stray edits, no
  unrelated files, no scope creep beyond what was delegated.
- **Adopt sound existing work without recreating it.** Verify the existing PR's
  exact head, diff, authorship, and remaining gaps. Preserve attribution and the
  PR's review history; a preferred branch name is not a reason to replace it.
  Head qualification does not by itself qualify the combined or resulting main
  tree; apply the repository's integration and post-merge policy.
- **For infrastructure, confirm the plan is non-destructive.** Read the plan or
  diff for replacements, deletes, and data loss before any apply.
- **Get the actual result from async workers.** When a worker reports idle or
  available, fetch its concrete output and verify that, rather than treating the
  signal itself as completion.

## When To Use

- Accepting or reviewing output from a sub-agent or delegated worker.
- Merging a PR you did not author, especially one a worker reports as ready.
- Chaining further work onto a delegated result.
- Reacting to an async worker's idle, available, or completion notification.

## When Not To Use

- Verifying your own direct output — verify the artifact, but the
  trust-the-report failure mode does not apply.
- A throwaway result with no downstream consumer and no safety surface, where a
  wrong result costs nothing.

## Anti-Patterns

- Merging because the worker said done, the status was idle, or CI was green.
- Copying the worker's pasted test output into your own report as if you ran it.
- Reviewing the worker's summary of the diff instead of the diff.
- Treating an async availability ping as a completion report.
- Accepting an infrastructure change without reading the plan for destructive
  operations.
- Chaining a second delegated task onto an unverified first result, so one
  wrong claim compounds.

## Done Standard

Delegated work counts as accepted only when you have checked the real artifact
yourself — independently verified applicable checks, read the safety-sensitive
diff, exercised the required real behavior, and confirmed the change is exactly
what was intended — not when
the worker reported it complete.
