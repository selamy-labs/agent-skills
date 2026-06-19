---
name: regression-ratchet
description: Use when fixing a bug, incident, or regression. In the same change, add the guard that would have caught it — matched to the failure class — so the problem can recur at most once. A fix without its ratchet is half-done.
---

# Regression Ratchet

A bug that can silently recur is a bug you will fix twice. The ratchet makes the
fix monotonic: the **same change** that fixes the problem also adds the guard
that would have caught it. Quality only goes up — the class you just closed
cannot reopen unnoticed.

## Same change, not "later"

A follow-up "add tests" task rots while the bug is now invisible (it's fixed
today). The guard ships **in the fix's own PR**, or the fix isn't done. If you're
tempted to defer it, you're choosing to debug this again.

## Match the guard to the failure class

The guard is whatever would have caught *this* failure — not always a unit test:

| Failure class | Ratchet |
|---|---|
| Logic bug in code | a unit/behavior test reproducing it |
| Wrong artifact / container / file shape | a structure or build test asserting the shape |
| Config / policy / permission gap | a policy or lint check that fails on the bad config |
| Integration / runtime edge | an integration test or a runtime assertion/precondition |
| Genuinely expensive to test | an alert/monitor that fires on recurrence |

## Prove it actually ratchets

The guard must **fail on the pre-fix state and go green after** — run it
red→green. A test that stays green with the bug still present guards nothing;
it's theater. If you
can't make it fail without the fix, you haven't reproduced the failure yet.

## One incident, one ratchet

Add the minimal guard for *this* class — don't bolt on a speculative suite. The
discipline is coverage of what broke, not maximalism. Breadth comes from many
incidents each leaving one guard behind, over time.

## The test

For the bug you just fixed, answer: **"If this exact thing happened again, what
would catch it automatically — and is that guard in this PR?"** If the answer is
"nothing" or "a separate task," the ratchet is missing.
