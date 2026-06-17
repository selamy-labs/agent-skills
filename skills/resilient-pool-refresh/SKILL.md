---
name: resilient-pool-refresh
description: Use when a job refreshes or validates a pool of credentials, tokens, sessions, or connections. Isolate per-item failures, classify terminal (needs human re-auth) versus transient (auto-retry), and alert clearly instead of failing the whole batch silently.
---

# Resilient Pool Refresh

A job that periodically refreshes a **pool** of items — OAuth tokens, API
sessions, certificates, DB connections — must not treat the pool as
all-or-nothing. The naive version loops over the pool, lets the first error
propagate, and exits non-zero. The scheduler retries the whole batch, exhausts
its backoff/retry limit, and ends as one silent failed run — even though most
items were fine and only one needed attention. You lose the good refreshes and
the signal about the one bad item.

Three properties make a refresh job resilient.

## 1. Isolate per-item failures

Wrap each item in its own try/except and **continue** on failure. One item's
terminal error must never abort the rest of the pool. Keep the **prior good
value** for an item that fails to refresh — a still-valid token is better than
no token — and record which items succeeded.

```python
results = {"ok": [], "needs_human": [], "transient": []}
for item in pool:
    try:
        new = refresh(item)
        store(item, new)
        results["ok"].append(item.id)
    except TerminalAuthError as e:      # do not retry; a human must act
        keep_prior_value(item)
        results["needs_human"].append((item.id, str(e)))
    except TransientError as e:         # safe to retry next run / now
        keep_prior_value(item)
        results["transient"].append((item.id, str(e)))
```

## 2. Classify terminal vs transient

Not all failures are equal. Decide per error class:

- **Terminal — needs human re-auth.** No amount of retrying fixes it; a person
  must re-authenticate. Signals: `401 Unauthorized`, `invalid_grant`,
  `refresh_token` revoked/reused/expired, account disabled. Do **not** retry;
  surface a clear, actionable alert naming the item and the human action.
- **Transient — auto-retry.** A blip that the next attempt likely clears.
  Signals: `5xx`, timeouts, connection resets, rate limits (honor `Retry-After`).
  Retry with bounded backoff; only escalate if it stays transient too long.

Misclassifying terminal as transient burns retries and delays the human;
misclassifying transient as terminal pages a human for a blip. Map known error
codes explicitly; default unknowns to transient-with-cap so they still surface.

## 3. Alert clearly, never fail silently

A `backoffLimit`-exhausted red job with no message is the worst outcome — it
hides *which* item failed and *why*. Instead, finish the run and emit a
structured summary: counts of ok / needs-human / transient, and for each
needs-human item the identity and the exact remediation ("re-auth account X").
Make the alert the product of the job, not a stack trace someone has to decode.

Real incident: a credential-refresh CronJob failed silently for ~2.5 days. One
provider's OAuth had hit a terminal `invalid_grant` (needed human re-auth), but
because the job aborted on first error and surfaced only a red run, nobody knew
which credential or what to do — and the other credentials' refreshes were lost
with it.

## Acceptance bar

A refresh job is done only if: a single item's terminal failure leaves the rest
refreshed; terminal vs transient is classified and acted on differently; and a
failure produces a human-readable alert naming the item and the fix — not just a
non-zero exit.
