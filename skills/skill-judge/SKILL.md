---
name: skill-judge
description: Use when reviewing or scoring a SKILL.md for design quality. Judge it by knowledge delta — what it teaches beyond what the model already knows — plus trigger clarity, scope, and progressive disclosure, and return specific fixes rather than a verdict.
---

# Skill Judge

A rubric for evaluating a single skill's *design quality*. This is narrower than
deciding whether a skill should exist (`skill-curation`) and than reviewing an
arbitrary artifact (`adversarial-review`): it scores how well a given SKILL.md
is built, and says exactly what to change.

## The core test: knowledge delta

> **A skill's value = expert-only knowledge − what the model already knows.**

A skill is not a tutorial. Restating what the model can already do is noise that
costs context and dilutes the catalog. The valuable content is the **delta**:
decision trees, trade-offs, non-obvious gotchas, anti-patterns, the "we learned
this the hard way" judgment that isn't in the base model. Before scoring
anything else, ask: *what does this teach that a capable model didn't already
know?* If the answer is thin, no amount of polish saves it.

## Dimensions to score

1. **Knowledge delta** (the gate) — is there real expert signal, or is it a
   tutorial? Cut restated basics.
2. **Trigger clarity** — does the `description` say *when* to use it in concrete,
   matchable terms (the situations, the verbs), so it actually fires at the
   right moment? Vague triggers = a skill that never activates.
3. **Scope** — one coherent capability. "And"-heavy skills should split.
4. **Progressive disclosure** — is the SKILL.md lean, pushing exhaustive detail
   into references loaded on demand, rather than front-loading everything?
5. **Actionability** — decisions and steps a reader can apply, not prose to
   admire.
6. **Self-containment** — no dependence on the author's private context; no
   secrets; renders/loads cleanly.

## Output: fixes, not a grade

A bare score teaches nothing. For each weak dimension, name the specific defect
and the concrete change — "the description lists capabilities but no trigger
situations; add the verbs/scenarios that should invoke it" beats "trigger
clarity: 2/5." Default to REVISE when the knowledge delta is thin; that's the
failure that quietly fills a catalog with filler.

---

_Adapted from the MIT-licensed [softaworks/agent-toolkit](https://github.com/softaworks/agent-toolkit) `skill-judge` skill._
