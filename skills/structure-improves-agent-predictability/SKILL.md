---
name: structure-improves-agent-predictability
description: Use when writing code an AI agent will later read and edit. Structure it so the next edit is obvious — typed boundaries, exhaustive matches, named dispatch, small surfaces — because well-structured code makes an agent predictable and ad-hoc code makes it guess.
---

# Structure Improves Agent Predictability

An AI agent edits code by pattern-matching against what it can see. The more the
code's structure *encodes the rules*, the less the agent has to infer — and
inference is where it guesses wrong. So structure isn't only for human
maintainers anymore: **a well-structured codebase makes agent behavior
predictable; an ad-hoc one makes every agent edit a gamble.** Treat the agent as
a first-class reader and design for it.

## Why structure changes the odds

A human carries hidden context across a sprawling function; an agent only has
what's in front of it plus its priors. When the rules live in the *shape* of the
code, a correct edit is the obvious edit:

- **Typed boundaries** (parse-don't-validate) mean the agent literally cannot
  thread an invalid value deep into the core — the type won't let it.
- **Exhaustive matches / tagged unions** mean adding a case forces handling it
  everywhere; the compiler points the agent at every site instead of it having
  to remember them.
- **Named dispatch (table/map) over long if/switch chains** means "add a
  behavior" is "add a row," not "find and correctly weave a branch into five
  places."
- **Small, single-responsibility surfaces** mean the blast radius of an edit is
  visible and local — the agent can't accidentally reach what it can't see.
- **Explicit contracts at the edges** (signatures that are misuse-resistant)
  mean the wrong call doesn't type-check, so the agent's mistake fails loudly at
  authoring time, not silently at runtime.

## The tell

If you find yourself writing a comment like "remember to also update X when you
change Y," that coupling is invisible to an agent and it *will* miss it. The fix
isn't a louder comment — it's structure that makes X and Y change together
(one source, exhaustive switch, shared type). Every "remember to…" is a latent
agent bug; convert it into something the type system or a single dispatch point
enforces.

## How to apply

When writing or reviewing code that agents will maintain, prefer the option that
*removes a thing the next editor has to remember*:

1. Make illegal states unrepresentable (types) over validating-and-hoping.
2. Make the set of cases explicit and exhaustive over open-coded branching.
3. Make "add a variant" a local, mechanical edit over a cross-cutting one.
4. Keep surfaces small so the visible context is the whole context.

This compounds with the rest of the design skills (parse-don't-validate,
map-dispatch-over-conditionals, enums-codify-behavior, small-focused-changes):
each one is also a bet that the next editor — increasingly an agent — will do
the predictable thing because the structure left no other obvious move.
