---
name: prefer-fakes-over-mocks
description: Use when choosing or reviewing test doubles — deciding between a fake, stub, or mock for a collaborator. Default to fakes (working in-memory implementations); reserve interaction mocks for the narrow cases where the interaction itself is the contract.
---

# Prefer Fakes Over Mocks

When a test needs a stand-in for a collaborator (a database, clock, queue, HTTP
client), **reach for a fake before a mock in almost every case.** A fake tests
*what the code does* (observable state and behavior); an interaction mock tests
*how the code does it* (the exact calls made) — and "how" is the part you most
want to be free to change. This complements [[test-design]] (which covers test
*structure*, and deliberately defers fakes-vs-mocks here) and pairs with
[[sim-zero-mode]] for deterministic test environments.

Principles are **language-agnostic**; per-language bindings are at the end.

## The taxonomy (so the terms are precise)

- **Dummy** — passed but never used (fills a parameter).
- **Stub** — returns canned answers to the calls a test makes; no logic.
- **Fake** — a real, lightweight *working* implementation: an in-memory
  repository, a fake clock, an in-memory message bus. Behaves like the real
  thing within the test.
- **Mock** — pre-programmed with expectations and **verifies the interactions**
  (which methods were called, with what, in what order).

Grounded in Martin Fowler, *Mocks Aren't Stubs* / *Fake, Don't Mock*, and
*Software Engineering at Google* (Test Doubles).

## Rules

1. **Default to a fake.** A fake is a working implementation that behaves like
   the real collaborator (in-memory store, fake clock, in-memory queue). Tests
   built on fakes assert the **outcome** — the resulting state — so they read
   like a spec and survive refactors that preserve behavior.

2. **Mocks couple tests to implementation.** A test that asserts
   `verify(repo).save(x)` or `mock.assert_called_with(...)` passes or fails based
   on the *call sequence*, not the result. It breaks when you refactor internals
   that still produce the right outcome, and — worse — it can stay **green while
   the real integration is broken**, because nothing exercised a real
   implementation. Brittle and low-signal.

3. **A fake must stay faithful, or it lies.** The risk with fakes is drift: the
   in-memory version diverges from production behavior. Counter it: the fake is
   **owned and maintained by the team that owns the real component**, and it runs
   against the **same contract tests** as the real implementation (one shared
   test suite, two implementations). A fake without contract tests is a
   liability, not an asset.

4. **Prefer state verification over interaction verification.** Assert the
   observable result after the act (`assert repo.get(id) == updated`), not the
   calls made to get there. Verify interactions only when the interaction *is*
   the externally-observable behavior.

5. **When a mock or stub is genuinely justified (keep it narrow):**
   - The real collaborator is unmockable or expensive and **no fake exists yet** —
     a stub returning a canned value is the pragmatic bridge (and a fake is the
     follow-up).
   - You must **inject a failure** (timeout, 500, disk-full) you cannot otherwise
     trigger — stub the error path.
   - The **interaction itself is the contract** — e.g. "publishes exactly one
     `OrderPlaced` event", "must not call the payment API twice". Then verify
     that *one* interaction, deliberately, not the whole call graph.

6. **Don't mock types you don't own.** Mocking a third-party SDK couples your
   tests to *its* surface, which can change or which you may misunderstand. Wrap
   it behind your own narrow interface and **fake that interface** instead.

## Detecting the smell (review / lint)

- A test file dominated by `when(...).thenReturn(...)` + `verify(...)` /
  `mock.assert_called*` / `expect(fn).toHaveBeenCalledWith(...)` is testing
  *interactions* — ask what **state** it should assert instead.
- A growing pile of mock setup at the top of every test = **a missing fake**.
  Build the fake once; delete the boilerplate everywhere.
- Mocks of types from outside the codebase = a **missing wrapper interface**.

## Honest cost

A good fake costs real effort up front — you write a working implementation and
keep it contract-tested. That cost is **amortized** across every test that uses
it and pays back in tests that don't break on refactors. For a genuine one-off
boundary you can't fake yet, a stub is the right, cheaper call — just don't let
one-offs become the house style.

## When to use / not

- **Use** when choosing a test double, or reviewing tests heavy in mock setup
  and interaction assertions.
- **Not** for test *structure/naming* (→ [[test-design]]) or for standing up a
  whole deterministic test environment (→ [[sim-zero-mode]]).

## Per-language bindings

- **Python:** prefer an in-memory fake class (e.g. `InMemoryUserRepo`
  implementing the same protocol) over `unittest.mock` / `MagicMock`; reserve
  `mock`/`monkeypatch` for error injection and un-fakeable boundaries; assert on
  the fake's resulting state, not `assert_called_with`.
- **Java/JVM:** a hand-written or shared `FakeXxx` implementing the interface
  over Mockito `verify(...)`; if you must use Mockito, prefer stubbed returns +
  state assertions over `verify` interaction checks; run the fake against the
  interface's contract tests.
- **TypeScript:** an in-memory object implementing the interface over `jest.fn()`
  / `vi.fn()` spies; keep `toHaveBeenCalledWith` for the rare interaction-as-
  contract case.
- **Go:** a small fake struct satisfying the interface over `gomock`; idiomatic
  Go already favors interface + fake; reserve generated mocks for interaction
  contracts.
