---
name: exhaustive-match
description: Use when every known variant must be handled. Prefer compiler, type-checker, linter, or test-enforced exhaustive matches.
---

# Exhaustive Match

Use this skill when a status, enum, union, event type, command, or error code has
a finite set of known variants and missing one would create incorrect behavior.

## Workflow

1. Model the variants as an enum, tagged union, sealed class, literal union, or
   another construct your toolchain can reason about.
2. Replace loose `if` chains with a match, switch, dispatch table, or strategy
   selection that enumerates every known case.
3. Make the default branch unreachable, absent, or fail closed. Do not use a
   broad default that hides newly added variants.
4. Enable the strongest available enforcement: compiler exhaustiveness,
   type-checker assertions, lint rules, or a test that compares handled cases to
   the declared variant set.
5. Add or update one test per behavior group, plus a test that proves unknown
   external values are rejected at the boundary.

## Review Checks

- Adding a new variant creates a visible failure until behavior is implemented.
- Unknown external input is parsed separately from internal exhaustive handling.
- The fallback path is explicit and safe, not a catch-all for forgotten cases.
- The match body stays small; complex behavior is delegated to named handlers.

## Anti-Patterns

- Keeping a `default` branch that returns a plausible value for all unknowns.
- Exhaustively matching raw strings from outside the system instead of parsing
  them into a known type first.
- Adding a variant without updating tests, documentation, and caller behavior.
