---
name: wiki-building
description: Use when building or maintaining a durable LLM-compiled knowledge base from documents, transcripts, code, decisions, or other evidence. Preserves immutable sources, normative authority, claim provenance, contradiction state, staleness, and generated projections while the wiki compounds over time.
---

# Wiki Building

Treat knowledge like a build: compile immutable sources into legible,
interlinked pages once, then incrementally rebuild affected knowledge when a
source changes. The wiki is maintained synthesis, not a concatenated archive or
an opaque retrieval index.

## Layers and authority

```text
raw sources -> compiled wiki -> audience projections
      |              |
      +---- normative authority, when explicitly designated
```

- **Raw sources** are immutable evidence. Register a stable source ID, URI,
  revision or digest, observed time, owner, and privacy class. Correct a source
  by adding a new revision, never by rewriting history.
- **Normative sources** are the explicitly designated contracts, policies, or
  specifications within the raw set. State their precedence. A wiki summary
  cannot promote evidence into authority.
- **The wiki** is canonical for synthesis, retrieval, cross-links, and current
  understanding. It remains derived unless a project explicitly makes a wiki
  page normative.
- **Projections** are audience-specific Docs, Sheets, dashboards, or briefs.
  They link back to stable wiki claims and sources and never become a competing
  authority by accident.

If there is no normative source, say so. Do not manufacture one from the newest
document or the most confident prose.

## Minimal structure

```text
raw/                         optional approved source snapshots
wiki/
  sources/                   one page per registered source
  entities/                  people, systems, organizations, artifacts
  concepts/                  one concept per page
  decisions/                 proposed, accepted, superseded
  journeys/                  user or operational flows
  synthesis/                 cross-cutting maintained understanding
  projections/               generated audience views
index.md                     content-oriented catalog
log.md                       append-only operation history
AGENTS.md                    schema, authority, ingest, query, lint rules
```

Store structured source/page events in an append-only ledger when automation
is available. Keep search indexes and embeddings rebuildable and non-authority.
Long-context reading is the simplest query path; add exact-text or hybrid
search only when corpus scale justifies it.

## Compile workflow

1. **Register the source.** Record identity, revision/digest, classification,
   authority class, and access boundary before extracting claims.
2. **Extract candidates.** Separate facts, preferences, proposals, accepted
   decisions, actions, questions, and contradictions. Preserve uncertain
   speaker or entity identity as uncertain.
3. **Integrate, do not append.** Update affected entity, concept, decision,
   journey, and synthesis pages. Add backlinks and supersession links.
4. **Cite material claims.** Give claims stable IDs and adjacent source or
   normative references. Classify the strongest evidence each citation proves.
5. **Handle conflict explicitly.** Mark claims `disputed`, `provisional`,
   `stale`, or `superseded`. Never resolve contradictions with "latest wins"
   unless the authority contract says that.
6. **Close the operation.** Refresh the deterministic index and append a log
   entry derived from recorded mutations. Re-ingesting the same digest must be
   a no-op.

## Authority synchronization

Synchronize normative material one way into generated reference blocks. Pin
requirements or policies by stable ID and digest. When authority changes:

- mark dependent claims and projections stale;
- fail validation until affected synthesis is reconciled;
- preserve claim identity across renames or archival moves;
- route a discovery that changes intended behavior back through the authority's
  review process before treating it as a requirement.

Never let the wiki silently edit, accept, or supersede a normative contract.

## Query workflow

Read `index.md` first, then the smallest relevant pages. Group the answer by
authority and evidence strength, cite stable claim/source IDs, and name gaps or
conflicts. When a query produces durable new synthesis, propose a wiki update
through the normal reviewed write path; do not let valuable conclusions die in
chat history.

## Lint and verification

Periodically and before publishing a projection, check:

- source digests and access classifications;
- broken or one-way links, orphan pages, duplicate IDs, and missing concepts;
- claims without citations or with inflated evidence classes;
- contradictions, unresolved gaps, supersession, and stale freshness markers;
- drift from normative IDs/digests;
- deterministic index/log output and idempotent re-ingest;
- projections that are older than their dependencies.

Auto-fix deterministic metadata only. Human or model judgment must review
semantic contradictions and changed decisions.

## Anti-patterns

- Treating compiled prose as more authoritative than its cited contract.
- Copying private evidence into a public wiki or search index.
- Dumping sources without synthesis and cross-linking.
- Hand-counting pages or hand-editing a structured ledger.
- Letting projections accept decisions, assign people, or send themselves.
- Maintaining duplicate wikis or dashboards that drift independently.
