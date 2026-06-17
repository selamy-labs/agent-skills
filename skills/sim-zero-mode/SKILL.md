---
name: sim-zero-mode
description: Use when building/testing an app or agent that depends on paid APIs or LLM calls and you must exercise plumbing/UI/harness without spend or real intelligence, for CI, demos, local dev. Activates on test without burning API budget, demo without live calls, or fakes/offline mode.
---

# Sim-Zero Mode

A whole-application mode where **everything runs on fake data with NO real LLM/paid/external calls.** The app **still works end-to-end — it just knows nothing.** Every screen renders, every flow completes, every agent step fires; only the *intelligence* and the *spend* are absent.

This is **fakes-over-mocks elevated to a first-class runtime mode**, not a test-only scaffold.

## What sim-zero proves (and doesn't)
- **Proves:** plumbing, wiring, UI rendering, navigation, the agent harness/orchestration, state flow, error handling, contracts between components.
- **Does NOT prove:** answer quality, model behavior, real third-party correctness. (That's what live evals are for — keep them separate.)

So: a green sim-zero run means "the machine moves correctly," not "the machine is smart." Both matter; sim-zero isolates the first cheaply.

## Design rules
1. **One switch, whole app.** A single config/flag (`SIM_ZERO=1`) flips every external boundary to a fake — not per-call opt-in. The boundary, not the caller, decides.
2. **Fakes return plausible, deterministic, shaped data** that satisfies the real contract (same schema/types), so downstream code and UI behave exactly as in production. Deterministic → CI-stable.
3. **No network/secret required.** Sim-zero must run with zero credentials and zero egress — that's what makes it safe for CI and untrusted demos.
4. **Honest labeling.** The UI/logs should make it discoverable that data is simulated (so a demo isn't mistaken for real intelligence).
5. **Cover the error paths too.** Fakes should be able to simulate failures/timeouts/empties, not just happy-path data.

## Where it pays off
- **CI:** run the full app/agent harness on every PR with no spend and no flakiness from external services.
- **Local dev:** work offline, fast, free.
- **Demos:** show the product working safely without live keys or unpredictable model output.
- **Plumbing regression:** catch "the wiring broke" independently of "the model changed."

## The analog to generalize from
A trading/agent system's **paper-mode** (executes the full pipeline against simulated fills, no real orders/money) is sim-zero for that domain. Generalize the pattern: **every agent and app should ship a sim-zero mode.** If you can't run your app with all external intelligence/spend switched off, your boundaries aren't clean enough — fixing that is itself valuable.

## Anti-patterns
- Per-call mocks scattered through tests instead of one app-wide boundary switch → drift, partial coverage, "works in tests, not in the app".
- Fakes that don't match the real contract → green sim-zero, broken production.
- Non-deterministic fakes → flaky CI.
- Sim-zero that secretly still calls something paid → defeats the purpose; assert zero egress.
