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

Run the checks that fit the artifact; do not stop at the report.

- **Re-run the reported checks yourself.** Execute the tests, lint, and build
  on the actual tree, do not trust pasted output. Confirm they succeed in the
  place that matters, not wherever the worker happened to run them.
- **Read the real diff.** For anything security- or safety-sensitive — shell or
  process execution, file writes, credential or secret handling, network
  egress, deletes — read the code and confirm the claimed guard actually exists
  in the diff, not just in the worker's summary.
- **Launch the produced artifact.** Install it, start the server, hit the
  endpoint, open the page, run the binary. A green unit suite routinely coexists
  with a packaging, dependency, or wiring defect that only a real launch
  reveals.
- **Confirm the file list is exactly the intended change.** No stray edits, no
  unrelated files, no scope creep beyond what was delegated.
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
yourself — re-ran the checks, read the safety-sensitive diff, launched what
should launch, and confirmed the change is exactly what was intended — not when
the worker reported it complete.
