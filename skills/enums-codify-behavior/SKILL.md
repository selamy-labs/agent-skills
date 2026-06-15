---
name: enums-codify-behavior
description: Use when behavior modes are represented by booleans, strings, or repeated conditionals. Prefer enums or tagged unions with behavior per case.
---

# Enums Codify Behavior

Use this skill when a mode, command, status, or option controls behavior in
multiple places.

## Workflow

1. Name the behavior cases as an enum, literal union, sealed class, or equivalent
   native construct.
2. Replace boolean flags when the name does not fully explain the behavior or
   future cases are plausible.
3. Move behavior close to the enum case through methods, pattern matching,
   dispatch tables, or exhaustive switches.
4. Make unknown or unsupported values fail with a clear error at the boundary.
5. Test every behavior case and at least one invalid input.

## CLI Rule

Prefer an enum option such as `--mode=check|fix|report` over boolean flags like
`--fix` and `--report` when options can conflict or grow. Boolean flags are fine
for a single independent yes/no setting.

## Review Checks

- The enum name describes the behavior, not just the transport value.
- Exhaustiveness is checked by the compiler, type checker, linter, or tests.
- String parsing is limited to input boundaries.
- Adding a case creates a visible compiler, linter, or test failure until the
  behavior is implemented.
