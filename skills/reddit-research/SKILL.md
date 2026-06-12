---
name: reddit-research
description: Use when collecting Reddit discussion signals for research, trend discovery, or hypothesis generation. Requires terms-aware access, source lineage, rate-limit discipline, and clear uncertainty.
---

# Reddit Research

Reddit is a noisy public-discussion source, not a ground-truth database. Use it
to generate hypotheses, locate themes, and identify examples that can be
checked against independent evidence.

## Access Order

- Check the current platform terms, developer documentation, and allowed use
  before automating collection.
- Prefer official API, OAuth, or an approved provider for reliable or scaled
  ingestion.
- Use public JSON or RSS endpoints only for low-volume discovery when the
  endpoint is available, permitted, and non-critical.
- Make no-credential polling bounded, cache-aware, and easy to disable.
- Use a descriptive user agent or app identity where the access path supports
  it.
- Respect rate limits and back off on `429`, `401`, `403`, and repeated `5xx`
  responses.

## Collection

- Define the research question, communities, time window, sort order, and
  filters before pulling data.
- Capture post URL, post ID, comment ID, author display handle when available,
  authored timestamp, retrieved timestamp, score fields, and permalink.
- Preserve deleted, removed, locked, quarantined, mature, and unavailable
  states instead of silently dropping them.
- Avoid bulk republication of user content. Quote only the minimum needed for
  verification and prefer short paraphrases with links.
- Treat vote scores, awards, and comments as engagement signals with caveats,
  not as popularity truth.

## Analysis

- Separate observation from interpretation.
- Label Reddit-derived outputs as sentiment, themes, objections, language, or
  candidate hypotheses.
- Do not infer demographics, identity, or intent beyond what the source
  directly supports.
- Watch for bots, coordinated activity, deleted context, sarcasm, stale posts,
  and community-specific norms.
- Check high-value claims against independent sources before using them for
  decisions.

## Build Requirements

- Store raw fixtures from sanitized examples and test parsers against them.
- Make pagination, retry, rate-limit, unavailable-content, and schema-drift
  behavior explicit.
- Emit structured logs and metrics for request count, status code, latency,
  backoff, parse failures, and records produced.
- Add tracing spans around fetch, parse, normalize, and publish steps when
  Reddit data feeds a product or automated decision.
- Keep credentials in the repository's established secret-management path.

## Output Contract

Every research output should include:

- question and collection window
- communities and filters queried
- access path used
- source links and retrieval time
- caveats and known missing coverage
- confidence level and recommended next verification step
