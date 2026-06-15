---
name: merge-conflict-discipline
description: Resolve merge conflicts by intent (never blind accept-ours/theirs) AND prevent them structurally — integrate, never clobber.
---

# merge-conflict-discipline

The core rule: **respect every change up to this point WHILE contributing your own — integrate, never clobber.** Blind `accept-ours`/`accept-theirs` is almost always wrong; it silently discards someone's intent. Deep thought is required only on LEGIT overlap (both sides touched the same thing for incompatible reasons).

This skill has two halves: **resolution** (a conflict is here now) and **prevention** (stop them recurring).

## PART A — RESOLUTION (semantic, intent-first)

1. **Understand the intent of BOTH sides first.** Hardest when you authored neither. Use the 3-way view — *ours / theirs / common ANCESTOR* (`git config merge.conflictstyle zdiff3`). Get the *why*: read the commit message, PR, issue, and author for the other side. "Get more context into why they deleted it" before you re-add it.

2. **Clean text merge ≠ correct merge.** Git cannot detect a SEMANTIC conflict — when one branch's intent isn't applied to lines the other branch added. A textually clean merge can be logically broken. **Resolution always ends by re-running build + the full test suite.** "It merged cleanly" is not done.

3. **Decision frameworks for legit overlap:**
   - **They deleted what you meant to change** (it's gone in latest): find out WHY it was deleted; confirm retaining it isn't at odds with the platform/north-star direction; then re-add, adapt, or drop your change. **Default to the platform direction over local intent.**
   - **They updated it differently from you:** ask the same *why*; reconcile by INTENT — combine both where compatible; if truly mutually exclusive, pick the one serving the north-star. Then ask the prevention question: *why did we collide here at all?*
   - **Both added independent edits** (the classic third case): standard 3-way integrate-both.

4. **Feed conflicts back into prevention.** A conflict CLASS that recurs is a signal, not bad luck — add a prevention control (below) so it can't recur (regression-ratchet discipline).

## PART B — PREVENTION (legit overlap is usually a design/hygiene smell)

If you keep hitting real overlap, ask: are we not using best practices — a consistent formatter? canonical sorting? leaning on open source enough?

- **Deterministic formatters, fleet-wide** (prettier / gofmt / ruff / black / buildifier / …): canonical layout → minimal diffs → fewer collisions. Enforce as a REQUIRED CI check so every PR arrives pre-canonicalized.
- **Canonical / alphabetical sorting** of imports, deps, enums, lists, config keys. Combine with **one-item-per-line + a magic trailing comma** so additions are *appends*, not line-edits two people fight over.
- **Custom merge drivers** via `.gitattributes` for trivial/generated classes (lockfiles: pnpm/poetry/composer, generated code) → auto-resolve the busywork.
- **Modular design** (SRP, small focused files, "things that change together live together") → fewer same-section edits. Repeated overlap in one file is a structural signal to split it.
- **Short-lived branches:** small frequent PRs + frequent rebase off main → conflicts stay small and incremental.

## DONE means
Build + tests green after resolution (not just "merged clean"); the resolution preserved both intents (or chose the north-star one deliberately, with the why recorded); and if the conflict class recurred, a prevention control was added.

Sources: git-scm gitattributes; semantic-conflict awareness (Fowler); standard merge-prevention practice.
