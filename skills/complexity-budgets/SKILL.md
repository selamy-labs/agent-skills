---
name: complexity-budgets
description: Use when code review needs enforceable limits on branching, nesting, function size, or parameter count. Ratchet budgets through standard linters.
---

# Complexity Budgets

Use this skill when functions are hard to understand because they branch too
much, nest too deeply, take too many parameters, or own too many responsibilities.

## Workflow

1. Run the repository's existing formatter and linter first.
2. Add the smallest rule that catches the problem — cyclomatic complexity,
   cognitive complexity, max nesting depth, max function length, or max
   parameter count — not all of them.
3. Set the first threshold to the current worst offender, so it stops *new*
   outliers without forcing a cleanup PR. For cyclomatic complexity, a starting
   threshold of 10 is the conventional default across the linters below; raise
   it to fit existing debt, then ratchet down.
4. Add focused tests around a complex function before reducing its complexity.
5. Lower a threshold only after the code is already below the new value.

## Tool Bindings

Each tool's cyclomatic-complexity check defaults to a limit near 10 — a sound
starting point before ratcheting:

- Python: Ruff with McCabe (`C901`) complexity rules enabled.
- TypeScript and JavaScript: ESLint `complexity` and `max-depth`; use
  typescript-eslint where type information matters.
- Go: golangci-lint with `gocyclo` or `gocognit`.
- Java: Checkstyle complexity checks plus SpotBugs for bug patterns.
- Kotlin: detekt complexity rules.
- Shell: ShellCheck plus shfmt; extract shell functions once branches grow.

## Review Checks

- The rule runs in CI and blocks only changed or newly violating code when the
  repository has existing debt.
- Suppressions are rare, local, and include a reason.
- Complexity reduction does not remove behavior coverage.
