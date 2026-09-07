---
name: complexity-budgets
description: Use when code review or implementation needs enforceable limits on branching, nesting, cognitive complexity, function size, or parameter count. Compose language-specific complexity analyzers with repo-native lint gates.
---

# Complexity Budgets

Use this skill when functions are hard to understand because they branch too
much, nest too deeply, take too many parameters, or own too many responsibilities.

## Workflow

1. Run the repository's existing formatter and linter first.
2. If a repository is indexed by CodeGraph, use `codegraph-worktree-startup`
   for navigation and blast radius before selecting files to measure. CodeGraph
   finds the code; complexity analyzers score it.
3. Run the appropriate language analyzer to find hotspots before choosing a
   threshold:

   ```bash
   # Python
   complexipy . --top 20 --sort desc
   complexipy . --max-complexity-allowed 10 --failed --suggest-refactors

   # TypeScript / JavaScript, when @genese/complexity is installed
   complexity src
   genese cpx src --language ts --console
   ```

   For changed-code checks, prefer the repository's base reference or staged
   diff when the tool supports it:

   ```bash
   complexipy . --diff main
   complexipy . --staged=true
   ```

4. Add the smallest rule that catches the problem — cyclomatic complexity,
   cognitive complexity, max nesting depth, max function length, or max
   parameter count — not all of them.
5. Set the first threshold to the current worst offender, so it stops *new*
   outliers without forcing a cleanup PR. For cyclomatic complexity, a starting
   threshold of 10 is the conventional default across the linters below; raise
   it to fit existing debt, then ratchet down.
6. Add focused tests around a complex function before reducing its complexity.
7. Lower a threshold only after the code is already below the new value.

## Tool Bindings

Each tool's cyclomatic-complexity check defaults to a limit near 10 — a sound
starting point before ratcheting:

- Python: Ruff with McCabe (`C901`) for cyclomatic complexity; `complexipy` for
  cognitive complexity, diff reports, and refactor suggestions.
- TypeScript and JavaScript: ESLint `complexity` and `max-depth`; use
  typescript-eslint where type information matters. When the `complexity` CLI
  from `@genese/complexity` is available, use it for cognitive and cyclomatic
  reports over `ts`, `js`, `tsx`, and `jsx` code.
- TypeScript hotspot scans: `fta-cli` is a good fast file-level triage tool
  when the repository already uses it, but do not substitute a file-level score
  for function-level thresholds when a function budget is needed.
- Go: golangci-lint with `gocyclo` or `gocognit`.
- Java: Checkstyle complexity checks plus SpotBugs for bug patterns.
- Kotlin: detekt complexity rules.
- Shell: ShellCheck plus shfmt; extract shell functions once branches grow.

## Review Checks

- The rule runs in CI and blocks only changed or newly violating code when the
  repository has existing debt.
- CI should invoke the same language analyzer used during review. Python CI may
  use `complexipy` JSON, GitLab, or SARIF output; TypeScript/JavaScript CI may
  use `complexity`/`genese cpx` reports or ESLint complexity rules, depending
  on whether the budget is cognitive, cyclomatic, or both.
- Suppressions are rare, local, and include a reason.
- Complexity reduction does not remove behavior coverage.
