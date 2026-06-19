---
name: adversarial-review
description: Use before claiming a high-stakes artifact done, verified, or merged. Have an independent grader adversarially review it — prompted to refute and find reasons not to ship, not to bless — and iterate to an explicit ship-or-revise verdict.
---

# Adversarial Review

The author is the worst judge of their own work — they review it with the same
assumptions that produced it. Before you call a high-stakes artifact done,
verified, or merged, hand it to an **independent grader whose job is to refute
it**: find the reason it should not ship. What survives a genuine attempt to
break it is far more trustworthy than what you blessed yourself.

## Refute, don't bless

The grader's task is adversarial: *"Here is a diff/artifact claimed ready. Find
the strongest reasons NOT to ship it — bugs, missed cases, unsafe assumptions,
unmet requirements. Default to REVISE if uncertain."* A prompt that asks "does
this look good?" gets a rubber stamp; a prompt that asks "why is this wrong?"
gets the defects. Uncertainty is a REVISE, not a SHIP — a weak "seems fine"
guards nothing.

## Independence is the whole point

A grader that shares the author's context inherits the author's blind spots.
Maximize independence:

- **Different context/model** than the one that produced the artifact — not the
  author re-reading their own work.
- **Clean-room where possible**: review the diff against an *untouched* checkout
  of the base, so the grader judges what actually changed, not the author's
  narrative about it.
- **Anti-reward-hack**: the grader is graded on finding real defects, not on
  agreeing. It does not get to clear the artifact by restating its intent.

## Explicit verdict, then iterate

The output is a decision, not a vibe: **SHIP** or **REVISE**, with the specific
defects that drove it. REVISE → fix the named defects → re-review. Loop until a
clean SHIP. Record the verdict with the artifact so "it was reviewed" is a fact,
not a memory.

## Scale to the stakes

Adversarial review costs an extra cycle; spend it where being wrong is expensive:
live-money or financial changes, security/auth/permission edits, fleet-wide or
irreversible infrastructure, data migrations, anything you cannot easily roll
back. A typo fix does not need a tribunal. Match the rigor to the blast radius.

## Make it standing, not ad-hoc

A review you *remember* to run is a review you will skip under pressure. Wire it
to the boundary where the claim is made — before a done-mark, before opening the
PR — with a hook or checklist gate, so the adversarial review is part of the
definition of done rather than an optional courtesy. For higher assurance, use
several independent graders with distinct lenses (correctness, security, does it
actually reproduce) and require a majority to clear.

## Where it fits

Adversarial review answers *"is this actually right and safe to ship?"* It pairs
with two siblings: **verify the real artifact** (does the thing exist and behave
in the closest-to-production form?) and **process-aware done** (never mark done
on a proxy). Run the adversarial review, verify the real artifact it points at,
then — and only then — claim done.
