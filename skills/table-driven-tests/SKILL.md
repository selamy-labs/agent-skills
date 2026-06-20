---
name: table-driven-tests
description: Use when one behavior should be checked against many input and output cases. Put cases in a named table and run the same assertion path.
---

# Table Driven Tests

Use this skill when many inputs should exercise the same rule, parser, formatter,
permission check, calculation, or state transition.

## Workflow

1. Confirm every case exercises the same behavior. Split separate behaviors into
   separate tests before building a table.
2. Define a case structure with a descriptive name, inputs, expected result, and
   expected error when relevant.
3. Run each case through the same arrange, act, and assert path.
4. Give each case a stable failure label using subtests, parameter IDs, or the
   framework's named-case mechanism.
5. Keep per-case branching out of the assertion loop. If a case needs unique
   assertions, it probably deserves its own test.
6. Include representative edge cases: empty input, boundary values, invalid
   input, and at least one ordinary success case.

## Review Checks

- A failing case name explains what broke without opening the table.
- The table is small enough to scan, or split by behavior group.
- Expected values are explicit; they are not recomputed by duplicating the code
  under test.
- Setup remains visible. Helpers may build data, but they do not hide the test's
  purpose.

## Anti-Patterns

- Mixing unrelated behaviors into one large table.
- Adding `if case.name == ...` logic inside the assertion loop.
- Using tables to avoid writing a clear standalone test for a unique workflow.
