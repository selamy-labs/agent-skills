---
name: adaptive-loop-budgeting
description: Use when an autonomous loop needs explicit limits on attempts, time, cost, tool calls, verification depth, or risk, and a clear stop, continue, yield, or escalate decision.
---

# Adaptive Loop Budgeting

Use this when a loop could keep consuming time, tokens, money, tool calls, or
risk without a natural stopping point. The goal is to spend effort where it
reduces uncertainty, stop when confidence is high enough, and escalate when the
next iteration would be expensive or unsafe.

This skill applies to coding loops, research loops, product feedback loops,
self-improving experiments, debugging sessions, and long-running verification.

## Budget Contract

Before starting or continuing the loop, name:

- **Objective:** what the loop is trying to improve, prove, or decide.
- **Budget:** allowed attempts, time, cost, tool calls, external requests, or
  verification depth.
- **Confidence target:** what evidence is enough for this risk level.
- **Risk tier:** low, medium, or high based on reversibility, user impact,
  money, data sensitivity, and production exposure.
- **Escalation trigger:** the condition that requires a human decision or a
  different plan.

If the task already has an explicit user budget, honor it. If no budget exists,
choose the smallest budget that can produce useful evidence and state it before
looping.

## Cheap-To-Expensive Ladder

Run the cheapest check that can reduce the current uncertainty:

| Uncertainty | Start with | Escalate to |
| --- | --- | --- |
| Is the task understood? | Restate goal and acceptance criteria | Ask for a decision |
| Is the code shape plausible? | Static read, type check, unit test | Integration or browser check |
| Is the user path working? | Local browser/API check | Target-environment verification |
| Is feedback actionable? | Cluster and confidence score | Experiment, metric, or user research |
| Is a fix durable? | Regression test or fixture | Monitor, alert, or policy gate |
| Is a claim real? | Direct artifact check | Independent system-of-record check |

Do not jump to expensive verification because it feels thorough. Escalate when
the cheaper layer cannot prove the claim or when the risk tier requires it.

## Loop Decision

At each iteration boundary, choose one:

- **Continue:** the latest evidence changed the hypothesis, the budget remains
  healthy, and the next step is likely to reduce uncertainty.
- **Stop succeeded:** the confidence target is met with evidence at the right
  layer.
- **Stop partial:** the loop produced useful work, but remaining uncertainty is
  explicit and bounded.
- **Yield:** progress is waiting on slow external state; checkpoint and switch
  work using [[yield-on-wait]].
- **Escalate:** the next step crosses budget, authority, risk, or decision
  boundaries.

Continuing requires a changed hypothesis. Repeating the same action because the
last result was inconvenient is not iteration.

## Adaptive Signals

Tighten or stop the loop when:

- the same failure repeats after distinct hypotheses;
- each iteration changes less than the one before;
- verification cost is rising faster than confidence;
- the diff, plan, or artifact is growing beyond the original objective;
- tool output is noisy enough that more calls would not clarify it; or
- the next action has irreversible or high-impact consequences.

Expand the budget only when:

- the loop is still reducing a named uncertainty;
- the remaining risk justifies the extra cost;
- the next step has a clear expected signal;
- no cheaper check can answer the question; and
- the user or governing policy permits the expansion.

## Confidence Targets

Match confidence to risk:

- **Low risk:** relevant local check, clear reasoning, and no known conflicting
  signal may be enough.
- **Medium risk:** automated checks plus direct artifact verification.
- **High risk:** independent verification, source-of-record readback, rollback
  plan, and human decision before irreversible action.

Use [[process-aware-done]] for completion claims and [[verify-real-artifact]]
when the result matters outside the local checkout.

## Output Shape

```text
Objective: <what the loop is optimizing or proving>
Budget: <attempts/time/cost/tool calls/risk limit>
Evidence so far: <strongest signal>
Decision: <continue, succeeded, partial, yield, or escalate>
Reason: <why this is the right boundary decision>
Next step: <only if continuing or escalating>
```

## Anti-Patterns

- Looping because no one chose a stopping condition.
- Spending expensive checks on low-risk uncertainty.
- Stopping because of fatigue while claiming confidence.
- Expanding scope instead of escalating for a product decision.
- Ignoring repeated failures because each one looks slightly different.
- Reporting "done" when the budget ended but the evidence did not close the
  claim.
