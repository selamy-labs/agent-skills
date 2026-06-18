---
name: stacked-diff-discipline
description: Use when dependent multi-step changes tempt one big PR or a pile of conflicting branches. Stack small dependency-ordered PRs, keep stacks short, and draft the upper members so N stacked PRs don't multiply CI cost.
---

# stacked-diff-discipline

The core rule: **small PRs by default; stack only genuinely-dependent work, keep the stack short, and draft the upper members so the stack costs ~1 CI run at a time, not N.** Stacking reconciles "smaller PRs always" with sequential work where PR2 needs PR1's code — without the two bad escapes (one big PR, or sibling branches that conflict and merge-order-couple).

## When to stack (default stays flat)

1. **Independent change → flat small PR.** Unchanged default: one small, self-contained PR to the trunk. Do **not** stack things that aren't actually dependent — coupling them just serializes unrelated work.
2. **Genuinely dependent multi-step → stack.** Use a stack only when PR_n requires PR_{n-1}'s code and splitting flat would force either one big PR or merge-order coupling. Each member still obeys the size bar (one concern, small).
3. **Short stacks only (≤ ~4).** A long stack is a smell — usually the work wasn't decomposed at the right seam, or independent pieces got chained needlessly. Long chain → re-plan the decomposition.
4. **Bottom-first merge.** Merge the base first; resync so the children retarget to the trunk; repeat upward.

## The CI-cost discipline (the part teams skip)

Each stacked member runs its **own** CI. When runner/build capacity is a constraint, a deep stack multiplies that cost.

- **Draft the upper members.** Open the bottom PR ready-for-review/auto-merge; open the rest as **draft** (configure drafts not to trigger required CI). Un-draft each only when its parent is green/merged. This serializes CI to ~1 active run instead of N — trading a little latency for a large capacity saving. When runners are scarce, latency is cheaper than runner-hours.
- **Cap stack depth** so even the worst case is bounded.
- Relax the draft-upper rule when capacity is ample; it is a discipline for the constrained case, not dogma.

## Tooling

Prefer a tool whose model maps onto ordinary branches + your existing auto-merge and PR-shepherding, with no extra hosted dependency:

- **Branch-chain tools that auto-retarget children on parent-merge and need no SaaS** are the safest default for automated/agent workflows — the branches are normal PRs your existing tooling already understands.
- **Avoid tools that push to special non-standard base branches** or rewrite history on every amend — they collide with auto-merge-at-creation and PR-shepherding.
- A richer hosted stack UI can stay available as a *human* convenience for viewing/curating a stack; it need not be the automation's transport.

## DONE means

A dependent change ships as a short stack of small PRs that each meet the size bar, merge bottom-first with children auto-retargeting, are shepherded to merged, and consume **at most ~1 concurrent CI run** when capacity is tight — i.e. smaller-PRs and dependent-work are reconciled without multiplying build load.

Sources: stacked-diff / patch-stack workflow practice (Sapling/Graphite/`git-town`/`ghstack`/`spr`); short-lived-branch + trunk-based development guidance.
