---
name: stakeholder-knowledge-projection
description: Use when translating authoritative project knowledge into stakeholder-facing Google Docs, Sheets, dashboards, journey evidence, or human task lists. Preserves source authority, audience-specific detail, native collaboration structures, protected manual decisions, assignment consent, freshness, and sync auditability.
---

# Stakeholder Knowledge Projection

Project one maintained body of knowledge into a collaborative workspace without
creating another source of truth. Make the first view understandable to a
non-engineer, while keeping exact claims, evidence, and decisions available by
drill-down.

## 1. Establish the contract

Before authoring, identify:

- the normative source for intended behavior;
- the canonical synthesis or project index;
- immutable evidence and its revisions;
- the audiences, their decisions, and their preferred detail level;
- the send, sharing, assignment, and approval gates.

Label the workspace artifact as a generated projection when that is what it is.
Every material status or decision must link to a stable claim, source, or
authority reference.

## 2. Use two reading depths

Build a concise landing document and a sortable evidence ledger.

**Landing document**

- current state and last verified revision/environment;
- user or operational journey with current proof;
- accepted decisions versus clearly marked proposals;
- audience-specific digest;
- recent material changes and next review boundary;
- links to detailed evidence and primary sources.

**Ledger or spreadsheet**

- Sources: identity, revision, owner, freshness, classification, disposition.
- Claims: stable ID, text, authority/evidence class, citations, conflict state.
- Decisions: proposer, decider, status, rationale, source, accepted time.
- Human work: outcome, owner, due date, status, source claim, notification state.
- Journey evidence: step, environment, viewport, result, artifact, revision, time.
- Sync audit: input revisions, generator version, proposed/applied changes,
  protected-field skips, conflicts, operator, time.

Use separate capability/detail tabs only when they materially improve scanning.
Do not create one tab per tiny requirement by default.

## 3. Preserve field ownership

Classify every field before synchronization:

- **Generated:** summaries, topic tags, candidate claims/tasks, deltas,
  freshness, and likely duplicates.
- **Manual:** accepted decision, decider, assignee, due date, authority override,
  retirement, launch/cutover claim, and send approval.
- **Hybrid:** generated candidate plus a separate reviewed value. Never overwrite
  the reviewed value.

Use revision guards and a dry-run diff. Fail closed on missing sources,
ambiguous identity, protected-field collisions, authority conflicts, or stale
evidence presented as current.

## 4. Use native collaboration primitives

Prefer native people, file, date, event, status, checklist, task, comment, and
project-management structures over plain-text imitations. Start from a verified
template when an API cannot create the exact native building block.

After a write, read back the artifact and verify that chips, links, dropdowns,
tasks, formulas, permissions, and revisions survived. A person chip does not
grant file access.

## 5. Assign human work deliberately

Create a native task only when all are true:

1. The action genuinely requires a human; engineering execution belongs in the
   engineering work tracker.
2. The action is explicit and accepted, not an unreviewed model inference.
3. The assignee identity, access, organization/domain support, and notification
   consequence are verified.
4. The outcome and due date are specific enough to complete.
5. Assignment is within the user's authority.

Keep cross-domain or unsupported assignments as an unassigned action row with
an internal accountable owner, or prepare a gated communication. Never assign
stakeholders merely because their names appear near a request. Audit for
orphaned or divergent tasks after checklist edits.

## 6. Ingest meeting and transcript material

Keep the raw transcript immutable. Segment on semantic topic changes while
preserving timestamps or stable source offsets and speaker uncertainty. Extract
factual claims, preferences, proposals, accepted decisions, actions, and open
questions separately. One utterance belongs to one primary segment; related
tags may cross-link it without copying the text.

Exclude personal or sensitive digressions from stakeholder projections while
retaining a bounded exclusion record. Every digest cites its source segment and
records generation/review state.

## 7. Synchronize and verify

1. Snapshot source and target revisions.
2. Recompute only affected claims, journeys, and projections.
3. Produce a dry-run diff and surface conflicts.
4. Obtain review for decisions, assignments, and stakeholder-facing prose.
5. Apply generated fields only, preserving native structures and manual fields.
6. Read back the document, ledger, and task state.
7. Append a sync-audit record.
8. Notify or send only through the explicit gate.

Freshness is dependency-driven: runtime claims stale when deployment identity
changes; code claims stale when affected paths change; stakeholder preferences
stale only when newer direct evidence supersedes them.

## Verification checklist

- [ ] The executive view and drill-down tell the same story.
- [ ] Every material claim has provenance and an honest evidence class.
- [ ] Proposals cannot render as accepted decisions.
- [ ] Old observations cannot repopulate current status silently.
- [ ] Manual fields survived synchronization.
- [ ] Native tasks were assigned only with verified identity and authority.
- [ ] Journey visuals identify environment, viewport, revision, and observed time.
- [ ] The final artifact was read back and the sync was audited.
