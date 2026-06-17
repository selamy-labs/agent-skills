---
name: test-design
description: Use when writing or reviewing tests: how to STRUCTURE and NAME them, what one test should cover, and when to split versus parameterize versus use soft assertions. Covers test structure and design, not the red-green-refactor workflow.
---

# Test Design

How to design tests that **localize failures and read like a spec.** This complements — does not duplicate — the upstream TDD-workflow skills (red→green→refactor) and pairs with the code-style foundation: those cover *when/how you drive code with tests*; this covers *what a good test looks like*.

Principles are **language-agnostic**; per-language bindings are at the end.

## 1. Visible AAA / Given-When-Then
Every test visibly separates **Arrange → Act → Assert** (Given/When/Then), by blank line or comment. The three phases should be findable at a glance. A test where setup, action, and verification are tangled is a test you can't read at failure time.

## 2. One concept per test (NOT literally one assertion)
Each test verifies **ONE behavior**, so a failure localizes precisely and the test name describes exactly one thing.
- This *usually* means one or a few assertions — but **multiple physical assertions are correct when they jointly verify ONE concept** (e.g. asserting every field of one returned object = "constructed correctly" = one concept).
- Use **soft assertions** (`assertSoftly` / `assertAll` / `expect.soft`) to check several properties of one result and report **all** failures at once — strictly better than splitting into N tests that each re-run the same arrange/act.
- **Split into separate tests only when the BEHAVIORS differ, never when the PROPERTIES of one result differ.**
- Honest framing: **"single concept, not literally single assert."** Do not ship one-assert-per-test dogma.

## 3. Small, focused cases with descriptive, sentence-like names
The **name is the spec.** `returns_404_when_run_id_is_unknown`, not `test3`. A reader should know what broke from the failed test's name alone, without opening the body.

## 4. Parameterize data variation; separate methods for different behaviors
- **Parameterize (table-driven)** across DATA variation of ONE behavior — many inputs, same rule. Give **each case a descriptive label** so a failure names the offending case.
- Use **separate test methods for DIFFERENT behaviors** — don't cram two rules into one parameterized table.

## 5. DAMP over DRY
Optimize tests for **readability-at-failure**, not DRY elegance. Inline repetition in tests is fine and often better. **DAMP ≠ no helpers** — *do* use test-data builders/factories where they aid clarity — just **never hide the AAA behind abstraction layers.** A test you must trace through three helpers to understand is a bad test even if it's DRY.

## 6. Comments: name + structure carry the WHAT; comment the WHY
The test name and AAA structure tell the story. Reserve comments for the **WHY** — business-rule rationale, non-obvious edge cases — not a play-by-play of the WHAT. **Density is language-dependent:** expressive languages (e.g. Kotlin) need very few; if the code reads like English, comment only business logic / non-obvious implementation. Don't go heavy.

## 7. Honest cost
Single-concept tests **multiply the test count** → more suite time + maintenance. State the tradeoff; don't pretend it's free. **Mitigate** with: parameterization (one method, many cases), **cheap arrange** (builders, fixtures), and **soft assertions** (one arrange/act, many checks). The goal is precise failure localization at acceptable cost — not maximum test count.

## When to use / not
- **Use** for designing or reviewing test *structure/quality*.
- **Not** the place for the red-green-refactor loop, or test-double strategy (fakes-vs-mocks) — see the upstream TDD skills and [[prefer-fakes-over-mocks]].

## Per-language bindings
- **Python (pytest):** AAA with blank lines; `@pytest.mark.parametrize` with `ids=[...]` for labels; soft checks via `pytest.raises`/`pytest-check` or grouping asserts on one object; descriptive `test_<behavior>_when_<condition>` names.
- **Kotlin/JVM (JUnit5 / Kotest):** backtick sentence names (`` `returns 404 when run id unknown` ``); `assertAll {}` (JUnit) / `assertSoftly {}` (Kotest) for one-concept multi-property; `@ParameterizedTest` + `@MethodSource` with named args; comments rare.
- **TypeScript (vitest/jest):** `describe`/`it('returns 404 when ...')`; `expect.soft(...)` for multi-property one-concept; `it.each([...])` (table) with a label template.
- **Go:** table-driven with `t.Run(tc.name, ...)`; each case named; `t.Run` subtests localize failures; `require` for fatal preconditions, `assert` for soft checks.
