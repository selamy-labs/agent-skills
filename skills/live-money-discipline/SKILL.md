---
name: live-money-discipline
description: Use when building or changing a system whose actions are irreversible — moving money, trading, production deploys, data deletion, external sends. Gate every live action behind dry-run, explicit go-live signoff, a kill switch, and blast-radius limits, and default to the reversible path.
---

# Live-Money Discipline

Some actions can't be taken back: a trade fills, money leaves, a row is deleted,
an email sends, a bad image rolls to the fleet. For these, "we'll fix it after"
doesn't exist — so the engineering bar is different. The discipline: **make the
irreversible action the hardest thing to do by accident, and the reversible path
the default.** Money is the sharpest example; the rules apply to any
irreversible effect.

## The gates (each independently)

- **Dry-run / paper mode first.** The same code path must be able to run without
  committing the effect — log "would have done X" — and that mode is the default
  until explicitly armed. Most live-action bugs are caught here for free.
- **Explicit go-live signoff for irreversible scope.** Crossing into real
  money / production scale is a deliberate, recorded human decision, not a config
  default or a flag that ships on by accident.
- **A kill switch that actually stops it.** One runtime control (no redeploy)
  halts new live actions immediately, and you've tested that it does. A safety
  control you haven't exercised is decoration.
- **Blast-radius limits / capital tiering.** Cap the per-action and cumulative
  exposure (position size, batch size, rate, spend). Separate "can lose it all"
  risk capital from protected capital; never let one bug reach everything.
- **Idempotency + reconciliation.** Make retries safe (idempotency keys) and
  reconcile intended-vs-actual continuously, because at-least-once delivery means
  the action *will* sometimes fire twice.

## Verify the real thing, adversarially

Before claiming a live change safe, verify the actual artifact in the real
environment (verify-real-artifact) and have an independent reviewer try to refute
it (adversarial-review) — the cost of a wrong "it's fine" here is realized, not
hypothetical. Default to REVISE under uncertainty.

## Defaults that bias to safety

- Reversible path is the default; irreversible requires arming.
- Fail closed — on error or ambiguity, *don't* take the live action.
- Make the dangerous command harder to issue than the safe one, not symmetric.
- Treat the runtime risk knobs (limits, kill switch, mode) as config you can
  change live without a deploy, so reacting to trouble is seconds, not a release.

## Smell

If the only thing between a typo and an irreversible effect is remembering to be
careful, the system is wrong — add a gate. Care is not a control.
