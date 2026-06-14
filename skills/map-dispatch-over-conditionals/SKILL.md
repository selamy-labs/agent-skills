---
name: map-dispatch-over-conditionals
description: Use when stable named cases are implemented as long if or switch chains. Prefer table, map, or strategy dispatch with explicit fallback behavior.
---

# Map Dispatch Over Conditionals

Use this skill when a conditional chain selects behavior by command, event type,
status, provider, file kind, or another stable key.

## Workflow

1. Confirm the cases are named and stable. If each branch has unrelated
   conditions, simplify the branches first instead of forcing a table.
2. Define a dispatch table from key to function, object, class, or value.
3. Put validation and fallback behavior next to the dispatch lookup.
4. Keep each handler small and independently testable.
5. Add or update tests for one normal case, the fallback case, and any case with
   special authorization, side effects, or error handling.

## Review Checks

- Adding a new case changes one table entry and one focused test.
- Unknown keys fail closed or use a documented default.
- Handler signatures are consistent enough that the dispatch table is readable.
- The table does not hide important ordering rules. If order matters, use an
  ordered structure and test it.

## Anti-Patterns

- Building a dispatch map for two tiny cases that were already clear.
- Capturing too much outer state in anonymous handlers.
- Replacing an obvious conditional with reflection, dynamic imports, or string
  evaluation.
