---
name: data-connector-building
description: Use when building integrations or replacing synthetic demos with real data sources. Requires source contracts, idempotent ingestion, validation, observability, and fixture-backed tests.
---

# Data Connector Building

The connector is the product when outputs depend on external systems.

The failure this guards: a synthetic demo, fixture, or stub mistaken for a real
source in production. Green tests and well-formed output prove the shape, not
that any byte came from the live system. Confirm the wire before trusting the
result.

## Workflow

- Define source ownership, auth path, rate limits, freshness, and allowed use.
- Ingest once and fan out to consumers; avoid duplicate polling and drift.
- Preserve raw source references and normalized records so each output traces
  back to the byte it came from.
- Validate schema and semantic invariants at the boundary.
- Add fixtures from sanitized examples and tests for missing or partial data.
- Emit structured logs, spans, and metrics for fetch, parse, normalize, and
  publish.
- Keep credentials in the repository's established secret-management path.

## Verify

Confirm the connector hit the real source: a live fetch span with the source's
own identifiers/timestamps in the lineage, not a fixture path or seeded value.
A connector that passes its tests against fixtures alone is unverified.

## Anti-patterns

- Shipping the fixture/stub path as the production path; the demo becomes the
  product no one noticed.
- Output that can't name which source byte produced it (no lineage to verify).
- Calling it done on green tests without one live fetch against the real source.

Until the connector independently sources the data, label outputs as demos or
format validation, not independent results.
