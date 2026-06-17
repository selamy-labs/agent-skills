---
name: small-focused-changes
description: Use when planning or breaking down any change: a PR, a refactor, a feature, or a delegated unit of work. Bias toward a series of small, single-concern, independently-revertible changes over one large one.
---

# Small, Focused Changes

Prefer a **series of small, single-concern changes** over one large one. Small diffs are statistically more correct, faster to review, and easy to revert if something goes wrong. This applies to PRs **and** to delegated work — chop dispatches into small units too.

## The rule
**One concern per change.** Each unit should be independently shippable and independently revertible. If reverting your change would also undo an unrelated improvement, it's two changes wearing one hat.

## Why
- **Correctness**: smaller diffs have fewer places for bugs to hide and get genuinely reviewed (large PRs get rubber-stamped).
- **Reversibility**: a one-concern change reverts cleanly; a mega-change forces you to choose between keeping a bug and losing good work.
- **Throughput**: small changes merge sooner and unblock dependents instead of sitting in review.
- **Bisectability**: when something breaks, a history of small commits points at the cause; a few giant commits don't.

## How to decompose
1. **Find the seams**: separate refactor-then-change (first a behavior-preserving refactor, then the behavior change), interface-then-impl, scaffolding-then-feature.
2. **Make each unit stand alone**: it should build, keep tests green, and be mergeable on its own — not "part 3 of 5 that only works with the others."
3. **Sequence by dependency**, ship in order; later units build on merged earlier ones.
4. **Keep mechanical churn separate** from logic: a rename or formatting sweep is its own change so the logic diff stays readable.
5. **For delegated work**: hand out one small, reversible unit per dispatch — not a big multi-part dispatch. The same correctness/reversibility math applies one level down.

## When to use / not
- **Use** whenever a change is more than one concern, or big enough that review would skim it.
- **Not** an excuse to split one atomic concern into nonsense fragments (a change and the deletion it requires ship together — see adopt-and-delete), or to ship a half-feature that breaks the build. Smallest *coherent* unit, not smallest possible.

## Anti-patterns
- The mega-PR that mixes a refactor, a feature, and a formatting sweep — unreviewable, unrevertible.
- "I'll split it later" — the split never happens once it's written as one blob.
- A stack of units where unit N silently depends on unit N+1 (none stands alone).
- A dispatch that asks for five loosely-related things at once.
- Splitting so aggressively that no single unit builds or makes sense on its own.
