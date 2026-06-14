---
name: early-return-over-else
description: Use when control flow is getting nested or hard to scan. Prefer guard clauses and early returns while preserving behavior with tests.
---

# Early Return Over Else

Use this skill when a function grows nested `if`/`else` blocks, mixes validation
with the main path, or forces readers to keep too much state in mind.

## Workflow

1. Identify the main success path and the exceptional, invalid, empty, or
   already-complete cases.
2. Move each edge case into a guard clause that returns, raises, continues, or
   breaks before the main path.
3. Remove `else` blocks that only exist because the earlier branch did not
   return.
4. Keep cleanup, transactions, and resource lifetimes explicit. Do not return
   early past required finalization unless the language construct guarantees it.
5. Run the focused tests that cover both the guard path and the main path.

## Review Checks

- The main path is less indented than before.
- Every early exit has a clear reason and a behavior test or existing coverage.
- No guard silently swallows an error that should be visible to callers.
- No broad refactor was mixed into a behavior fix.

## Anti-Patterns

- Converting simple two-branch code into scattered exits with worse locality.
- Returning a default value when the previous code raised or logged an error.
- Using early returns to avoid modeling states that should be explicit.
