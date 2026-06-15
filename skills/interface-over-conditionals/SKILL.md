---
name: interface-over-conditionals
description: Model behavioral variation as an interface with N implementations and select the right one for the context — instead of branching on a type/flag at every call site.
---

# interface-over-conditionals

When behavior varies by some type/mode/flag, the reflex is `if/elif/else` (or `switch`) at each call site. That scatters the variation across the codebase, and every new variant means editing every branch. Instead: **define one interface, write one implementation per variant, and inject/select the right implementation for the context.** Having the right implementation IS the answer — the conditional disappears.

## The move
1. Name the varying behavior as an **interface** (one method, or a small cohesive set).
2. One **implementation per variant** — each self-contained, independently testable.
3. **Select once** at the boundary (a factory / DI container / a map from key→impl) and hand the chosen impl down. Call sites just call `impl.do(x)` — no branching.

```
# instead of, at every call site:
if kind == "kalshi": ...elif kind == "coinbase": ...elif kind == "onchain": ...
# do:
strategy = REGISTRY[kind]   # select once
strategy.execute(ctx)       # call sites are branch-free
```

## When this beats conditionals
- The same `if kind == …` ladder appears in MORE THAN ONE place → the variation is a missing abstraction.
- Adding a variant currently means editing several branches (open/closed violation).
- The branches are BEHAVIORAL (different algorithms), not a one-off data lookup.

## When NOT to (keep it simple)
- A single, local, two-way branch that won't grow → just use the `if`. Don't manufacture an interface for one call site (that's over-engineering — respect the complexity budget).
- Pure DATA-driven selection (key → value/handler, no behavior) → a **dispatch table/map** is lighter; see `map-dispatch-over-conditionals`. This skill is its sibling for *behavioral* polymorphism.

## DONE means
The varying behavior lives behind one interface with one impl per variant; selection happens once at a boundary; call sites are branch-free; adding a variant is adding a class + a registry entry, touching nothing else.

Cross-link: `map-dispatch-over-conditionals` (data-driven dispatch) · pairs with the code-taste cluster (no-nullable-params, fakes-over-mocks, early-return-over-else, enums-codify-behavior, complexity-budgets).
