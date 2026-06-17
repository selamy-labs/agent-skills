---
name: criticality-tiered-reporting
description: Use when deciding what a service or worker should report and how loudly — alerts, progress updates, logs, notifications. Tier output by criticality so the important signal is not buried in routine chatter.
---

# Criticality Tiered Reporting

A reporter that emits at one volume is wrong at every volume. Page on
everything and the operator mutes you; report nothing and a real failure passes
unnoticed. The fix is to **tier the signal by how much the consumer needs to act
on it**, then route each tier to a channel matched to that urgency.

## The tiers

Classify every reportable event into one of three tiers and route accordingly:

- **Down or blocking** — the work has stopped, is stuck, or needs a human
  decision to proceed. This is the only tier that may **interrupt** a person:
  page, alert, or message a channel someone watches. It must be actionable —
  what broke, what is blocked, and the first step to unblock.
- **Routine progress** — the work is proceeding normally: started, milestone
  reached, finished. **Summarize**, do not interrupt. A periodic digest, a
  status surface someone can pull, or a single completion message — not a ping
  per step.
- **Noise** — heartbeats, retried-and-recovered transients, per-iteration
  detail. **Stay silent** on the active channels; write it to logs or metrics
  for later inspection, not to anyone's attention.

## Route to the consumer's need, not your chattiness

The tier is defined by **what the consumer must do**, not by how much is
happening internally. A thousand successful iterations are still tier "routine"
(one summary). One stuck queue is tier "blocking" (one page). Map each tier to a
channel whose interruption level matches: interrupting channel for blocking,
pull/digest surface for progress, log/metric sink for noise.

## Design rules

- **Escalate, don't duplicate**: a blocking event pages once; it does not also
  spray progress updates. Lower tiers never borrow the higher tier's channel.
- **Actionable at the top**: every interrupting message names the problem and a
  next step. "Something went wrong" is not a tier-one message.
- **Recovered transients are noise**: a retry that succeeded is not a page;
  count it as a metric. Only a transient that exhausts retries escalates.
- **Make routine pullable**: progress should be queryable on demand so the
  consumer checks when they want, instead of being pushed each step.
- **Silence is a valid output**: when nothing crosses a tier threshold, emit
  nothing to attention channels. No-news is good news, by design.

## Anti-patterns

- alerting on every event so real failures drown in green noise (alert fatigue)
- emitting a notification per step of a long routine job
- a "failure" page that contains no cause and no next action
- paging on a transient that already recovered on retry
- silence by omission: a worker that simply never reports, so a real stall is
  invisible until someone notices the absence of output

## Done means

Events are classified into down/blocking, routine progress, and noise; each tier
routes to a channel matched to its urgency; interrupting messages are actionable;
routine is summarized or made pullable; and noise stays out of attention
channels while remaining inspectable in logs or metrics.
