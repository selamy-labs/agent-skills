---
name: feature-coverage-not-just-line-coverage
description: Use when coverage percentage is treated as proof. Require named feature behavior tests, source fixtures, and anti-overfit checks alongside line coverage.
---

# Feature Coverage Not Just Line Coverage

Use this skill when a change claims safety from line coverage alone or when a
feature has no test that names the behavior users depend on.

## Workflow

1. Name the feature promise in user or system terms.
2. Add at least one test, fixture, or acceptance check that proves that promise
   directly.
3. Keep line, branch, or mutation coverage as a floor for untested code paths.
4. For generated, ranked, or data-derived behavior, record source lineage and
   check for leakage, lookahead, copied answers, and overfit fixtures.
5. Make the CI gate fail when either the coverage threshold or the named feature
   check fails.

## Review Checks

- The test name or fixture clearly maps to the feature promise.
- The assertion checks the real output or contract, not only an implementation
  detail.
- Fixtures are realistic enough to catch regressions and small enough to review.
- A high line-coverage number is not used to excuse missing behavior coverage.

## Ratchet Pattern

When fixing a bug, add the cheapest durable guard that would have caught it:
unit test for code logic, fixture for parser or data behavior, integration test
for cross-module contracts, or monitor for an operational failure that tests
cannot reproduce.
