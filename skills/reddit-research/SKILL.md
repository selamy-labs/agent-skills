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
- Default to no-credential public endpoints for low-volume discovery: JSON
  listing and thread URLs such as `/r/<community>/top.json?t=day&limit=25` or
  `/r/<community>/comments/<post_id>.json`, and RSS feeds such as
  `/r/<community>/.rss`.
- Keep no-credential polling bounded, cache-aware, and easy to disable.
- Use a descriptive user agent or app identity where the access path supports
  it.
- Respect rate limits and back off on `429`, `401`, `403`, and repeated `5xx`
  responses.
- Escalate to official API, OAuth, or an approved provider when public
  endpoints are blocked, comments are unavailable, volume grows beyond light
  polling, or the research becomes product-critical.

## Verified Envelope

Public Reddit endpoints are not a stable contract. Validate current behavior
from the actual runtime network before treating the path as available.

A useful light-polling envelope is:

- a small allowlist of communities
- single-digit requests per run
- conservative cadence such as minutes or hours, not tight loops
- local caching keyed by URL and retrieval time
- graceful partial output when listing, thread, or RSS variants differ

If JSON listing or comment-tree endpoints return `403` from a datacenter or CI
network, record that as an access-path finding and fall back to RSS only for
feed-level discovery. RSS does not replace comment-tree ingestion. Escalate
when comments or reliable pagination are required.

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
