---
name: product-loss-descent
description: Use when iterating on a product, app, or codebase across multiple work sessions. Treat development like a training loop: define a measurable product loss, drive it monotonically down each epoch with NO regressions, and stop when loss plateaus.
---

# Product Loss Descent (PLD)

Develop a product the way you train a model: as a **loss-minimizing loop**. Each sweep over the work is an **epoch**; every epoch must **decrease the loss**; you **stop when the loss plateaus**.

## The loss function
Define product loss as a measurable, observable quantity — the sum of what's wrong with the product right now:

```
loss = open_bugs
     + missing_required_features
     + UI/UX_roughness
     + regressions            # ← weighted heavily; see the hard rule
```

Pick a concrete proxy for each term so the number is real, not a vibe (e.g. failing/missing test count, open issues, a rubric score for UI roughness, broken user journeys). You don't need precision — you need a number that moves in the right direction and that you measure the same way each epoch.

## The two hard rules
1. **Monotonic, no-regression.** An epoch may **never introduce a new bug or break a working feature.** A change that fixes 3 things but breaks 1 is a *failed* epoch — the regression term dominates. Working behavior is sacred; protect it with tests before you touch it (this is the regression-ratchet, made continuous).
2. **Stop at the plateau.** When an epoch yields only diminishing returns — loss barely moves — **stop the session.** Plateaued effort is wasted effort and a source of accidental regressions. Resume in a later session with fresh scope.

## The loop
```
1. MEASURE loss (baseline for this epoch — the same metrics each time)
2. Pick the highest-loss term and reduce it
3. VERIFY: re-measure. Did loss go DOWN? Did any term go UP? (no-regression check)
4. If a term went up → revert/fix until it doesn't, before moving on
5. Repeat until loss plateaus → STOP for the session
```

## Why this works / when to use it
- It makes "are we done?" **measurable** instead of a judgment call — done = plateau, not "feels finished".
- It forbids the most common failure: shipping a fix that quietly breaks something else.
- It bounds session length rationally (stop at diminishing returns) instead of grinding.

Use it for any sustained build/improve/polish effort. It composes with: process-aware completion (loss is the evidence), test-driven development (tests define the bug/regression terms), and behavior-evidence audits (the no-regression check). It is **not** for one-shot tasks with a single obvious endpoint.

## Anti-patterns
- A "loss" you don't actually measure → you're guessing, not descending.
- Tolerating a small regression "for now" → the regression term is the one you weight most; never.
- Grinding past the plateau → diminishing returns + rising regression risk.
- Adding new features while bugs are open and loss is high → reduce existing loss first.
