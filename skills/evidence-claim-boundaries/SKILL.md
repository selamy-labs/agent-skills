---
name: evidence-claim-boundaries
description: Use when verifier output, schemas, fixtures, templates, CI, or readiness artifacts are being used to support live, human-gated, hardware, deployment, or completion claims.
---

# Evidence Claim Boundaries

Use this skill when a task produces or reviews evidence artifacts that could be
mistaken for proof of the real-world outcome. Verifiers, schemas, fixtures,
templates, summaries, and CI runs can make a future check stronger, but they do
not prove that the underlying live run, user-visible behavior, hardware path, or
deployment outcome happened.

## Classify the Evidence

Name the evidence category before writing the claim:

- `readiness`: tooling, verifier, schema, template, fixture, or docs hardening.
- `proxy`: logs, summaries, screenshots, traces, or generated reports that
  describe another artifact.
- `primary`: the real artifact or behavior in the environment where it matters.
- `human-gated`: a primary artifact that requires a person to perform or observe
  the live run.

If evidence is readiness or proxy, say what it prepares or checks. Keep the
completion claim open until primary evidence exists.

## Write Bounded Claims

When reporting progress, include:

- The artifact changed or checked.
- The verifier or review path used.
- The exact outcome that is supported.
- The outcome that is not supported yet.

Prefer this compact form:

`Readiness landed: <artifact/check>. Supports: <future claim it can verify>. Not claimed: <live/human/hardware/deployment outcome still missing>.`

## Preserve Completed Packages

Keep a completed execution's frozen package and manifest inputs unchanged.
Prepare follow-on edits and outputs in a separate package outside the recorded
scope, including any directory or prefix entries; reuse immutable inputs.

If later preparation changes a recorded input, preserve both versions and the
accepted result. When existing evidence establishes that the accepted execution
used verified original bytes and the drift happened afterward, restore those
bytes and verify their recorded identity. Record the drift and restoration
identities and observed timing; restoring bytes must not erase the intervening
event. Retain that acceptance instead of replaying accepted effects to repair
file drift. If the originals cannot be
verified, the executed inputs differed, or the effects are uncertain, use the
existing recovery or affected requalification path.

Carry out routine reversible artifact repairs covered by standing authority
without repeated per-file escalation. Preserve explicit holds, source custody,
and human gates; changed executed artifacts still need their affected checks.

## Human-Gated Outcomes

For manual or human-gated acceptance, stop at "ready for human attempt" until
the person-produced sanitized artifact exists. CI, fixtures, dry runs, templates,
or simulated inputs can prepare the path, but they cannot stand in for the
manual run.

When the human-gated artifact exists, report the required bundle:

- Same-session or same-environment identifier, if the claim depends on it.
- Sanitized primary artifact location or attachment.
- Verifier result or reviewer-safe summary.
- Residual missing rows, if any.

## Review Checklist

Before marking an issue, PR, or task complete, ask:

- Is this primary evidence, or only readiness/proxy evidence?
- Does the wording name the real artifact still required?
- Could a reader mistake this for live, hardware, deployment, or human
  acceptance?
- Are synthetic, fixture, dry-run, or CI-only artifacts clearly labeled as such?
- If the required artifact is unavailable, is the task reported as waiting,
  blocked, or ready for human attempt rather than complete?
