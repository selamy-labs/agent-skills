---
name: knowledge-routing
description: Use when you capture a learning and must decide where it lives. Route by type — shared construct to a skill, fact to memory, reference to a doc, behavior to a thin pointer — so it propagates once and is never duplicated or re-derived.
---

# Knowledge Routing

Knowledge gets re-explained and re-derived for one of two reasons: it is
**trapped** in a place only one actor can read, or it is **duplicated** across
several places that drift apart. The fix is a single rule: **capture once, route
to the one right home by type, and point — never copy.**

## The routing rule

| What you learned | Where it lives | Why there |
|---|---|---|
| A **construct / technique** many actors will reuse | a **skill** (the single source of truth) | versioned, audited, distributed once to everyone |
| A **fact / state / decision** | a **memory / shared store** that others can read | durable and queryable; it is knowledge, not behavior |
| A **reference / long-form explanation** | a **wiki / doc** | linkable, survives in depth without bloating prompts |
| A **behavior an actor must follow** | a **thin pointer** to the skill from the config file (CLAUDE.md / AGENTS.md) | one source; the config references, never restates |

If a thing is two types, split it: the reusable part becomes a skill, the
specific fact becomes a memory, and each points at the other.

## Read from the same home you write to

Before asking a human or re-solving a problem, **check the system of record
first** — the home a piece of knowledge would route *to* is the home you read it
*from*. "Ask the person" is the last resort, not the first; most of what feels
like a missing answer is already captured somewhere readable.

## Capture once, propagate by tooling

The artifact is the source. Tooling (a bundle, a sync, a build) distributes it to
every consumer. You never hand-copy the content into N files — the copy is how
drift starts. Changing the behavior means changing the **one** source, then
letting propagation carry it.

## Anti-patterns

- **Pasting skill content into a config file.** The config should hold a pointer
  ("follow the `x` skill"), not a second copy that will rot.
- **Filing a reusable construct as a one-off note.** If others will reuse it, it
  is a skill, not a buried memory.
- **A new parallel doc** for something already documented. Extend the canonical
  one; do not fork it.
- **Asking for what is already recorded.** That is a read miss, not a knowledge
  gap — fix your lookup, not the source.

## The test

For anything worth keeping, answer: **"Where is the ONE place this lives, and how
does everyone else read it without me telling them?"** If the answer is "in my
head" or "in three places," route it.
