---
name: data-connector-building
description: Use when building integrations or replacing synthetic demos with real data sources. Requires source contracts, idempotent ingestion, validation, observability, and fixture-backed tests.
---

# Data Connector Building

The connector is the product when outputs depend on external systems.

## Checklist

- Define source ownership, auth path, rate limits, freshness, and allowed use.
- Ingest once and fan out to consumers; avoid duplicate polling and drift.
- Preserve raw source references and normalized records.
- Validate schema and semantic invariants at the boundary.
- Add fixtures from sanitized examples and tests for missing or partial data.
- Emit structured logs, spans, and metrics for fetch, parse, normalize, and
  publish.
- Keep credentials in the repository's established secret-management path.

Until the connector independently sources the data, label outputs as demos or
format validation, not independent results.
