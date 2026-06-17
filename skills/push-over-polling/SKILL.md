---
name: push-over-polling
description: Use when designing how one component learns about another's state changes — observability, status propagation, readiness, or event reaction. Prefer server push over client polling; if polling is unavoidable, bound it.
---

# Push Over Polling

When component A needs to know about state changes in component B, default to
**B pushing the change to A**, not **A repeatedly asking B**. A busy poll loop
both wastes cycles when nothing changed and lags reality between intervals. Push
delivers the change once, when it happens, and goes quiet otherwise.

## Choose push first

Prefer a push mechanism the platform already gives you:

- server-sent events or a long-lived stream for one-way server-to-client updates
- webhooks or callbacks for cross-service notifications
- a message bus, queue, or pub/sub topic for fan-out to many consumers
- change-data-capture, watch APIs, or a notify/listen channel from a datastore
- a blocking/long-poll endpoint that holds the request open until something
  changes, instead of a tight retry loop

Push wins on two axes at once: it is cheaper (no traffic while idle) and fresher
(no interval-sized lag). Reach for it before writing any retry loop.

## When polling is unavoidable

Some sources offer no push and you must poll. That is acceptable only if the
poll is **bounded**, never a tight loop:

- **Backoff**: grow the interval when nothing changes; reset on a real change.
- **Jitter**: randomize intervals so many clients do not align into thundering
  herds.
- **Conditional requests**: send `If-Modified-Since` / `ETag` (or an equivalent
  cursor/version) so an unchanged source returns cheaply with no payload.
- **Cursors over rescans**: poll for "what changed since X", not "give me
  everything so I can diff it".
- **A ceiling and a floor**: cap the slowest interval so freshness stays
  bounded; floor the fastest so you never hammer.

## When polling is actually fine

Do not over-engineer. A simple bounded poll is the right call when the source
genuinely has no push, the change rate is low, and a few seconds of lag is
acceptable — for example a one-shot readiness check with backoff. The target is
"bounded and quiet when idle", not "push at any cost".

## Anti-patterns

- a `while true` loop that re-requests with no sleep, no backoff, and no
  conditional check — burning cycles to mostly re-read unchanged state
- fixed-interval polling fast enough to be expensive but still too slow to be
  fresh — the worst of both
- many clients polling the same source on the same fixed interval (synchronized
  herd) with no jitter
- re-fetching and diffing a whole collection every tick instead of asking for a
  delta
- adding a poller when the source already exposes a stream, webhook, or watch

## Done means

State changes reach the consumer via push where the source supports it; any
remaining poll is bounded (backoff + jitter + conditional/cursored requests with
a capped interval) and is silent when nothing has changed.
