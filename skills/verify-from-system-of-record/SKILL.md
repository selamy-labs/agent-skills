---
name: verify-from-system-of-record
description: Absence of visibility is not absence of the thing — verify state, identity, and existence against the system of record, not a partial or cached view.
---

# verify-from-system-of-record

**The trap:** you don't see X in the view you happened to check, so you conclude X doesn't exist — or is broken, missing, or unauthorized. But your view is partial. The thing may be fine; your *vantage point* is wrong. "I can't see it" is a statement about your view, not about reality.

**The rule:** before claiming something is missing, broken, stale, or unauthorized, verify against its **system of record** — the authoritative source for that fact — not a cache, a mirror, a sidecar, or whatever was nearest to hand.

## Identify the system of record FIRST
For each kind of fact, know what's authoritative:
- **Identity / auth:** what the live service reports (e.g. the in-process/in-pod authenticated session), NOT a credentials vault's apparent contents. A missing vault entry can be a custody-model choice, not an auth gap.
- **Deployed state:** the cluster/runtime, not a local manifest you assume is applied.
- **Existence of a record:** the canonical store queried directly, not a partial list, a stale dashboard, or a search index that may lag.
- **"Which remote is canonical":** what the team treats as truth, not whichever endpoint happens to accept your push (a mirror will accept writes and silently diverge).

## Then verify by USE, not by inference
The strongest verification exercises the real path: authenticate and read back the actor; query the record by key; apply and re-read. "It should be there" / "the absence implies X" is inference, not verification.

## Distrust your OWN prior diagnosis
The most expensive version of this trap is believing your earlier conclusion. If you decided "this path is blocked / this is missing," and you're now building on that, re-test it cheaply before you build further — prior diagnoses are partial views too, and the supposedly-blocked path is often open.

## DONE means
You named the authoritative source for the fact, read it directly (ideally by exercising the real path), and your claim of present/absent/valid/canonical is grounded in that read — not in the silence of a partial view.

Cross-link: verify-real-artifact (the artifact exists and works) · process-aware-done (how it was produced).
