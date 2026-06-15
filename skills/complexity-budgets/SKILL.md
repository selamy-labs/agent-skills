---
name: complexity-budgets
description: Use when code review needs enforceable limits on branching, nesting, function size, or parameter count. Ratchet budgets through standard linters.
---

# Complexity Budgets

Use this skill when functions are hard to understand because they branch too
much, nest too deeply, take too many parameters, or own too many responsibilities.

## Workflow

1. Use the repository's existing formatter and linter first.
2. Add the smallest complexity rule that catches the problem: cyclomatic
   complexity, cognitive complexity, max nesting depth, max function length, or
   max parameter count.
3. Set the first threshold to stop new outliers without requiring a large
   cleanup PR. A threshold near 10 is a practical starting point for many
   cyclomatic-complexity tools.
4. Add focused tests around a complex function before reducing complexity.
5. Ratchet thresholds down only after code is already below the new threshold.

## Tool Bindings

- Python: Ruff with McCabe complexity rules enabled.
- TypeScript and JavaScript: ESLint complexity and max-depth rules; use
  typescript-eslint where type information matters.
- Go: golangci-lint with gocyclo or gocognit.
- Java: Checkstyle complexity checks plus SpotBugs for bug patterns.
- Kotlin: detekt complexity rules.
- Shell: ShellCheck plus shfmt; extract shell functions once branches grow.

## Review Checks

- The rule runs in CI and blocks only changed or newly violating code when the
  repository has existing debt.
- Suppressions are rare, local, and include a reason.
- Complexity reduction does not remove behavior coverage.
