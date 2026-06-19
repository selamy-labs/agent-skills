---
name: trajectory-scoring
description: Use when evaluating an agent run, reviewing fleet performance, designing trajectory telemetry, or grading the journey rather than only the final answer. Applies promise-and-progress scoring to each step without requiring a trained reward model.
---

# Trajectory Scoring

Grade an agent run by the quality of the path it took, not only by whether the
last message looked right. Use a lightweight promise/progress rubric inspired by
process reward models for agents: each step is judged by whether it made the
goal more reachable and whether it actually reduced uncertainty, risk, or work.

## Step Rubric

Score each meaningful decision, tool call, handoff, or wait checkpoint:

- **Promise:** Was this step a good bet before seeing the result?
  - 2 = directly targeted the goal or highest-risk unknown.
  - 1 = plausibly useful but indirect or low-leverage.
  - 0 = cosmetic, duplicative, or poorly aimed.
- **Progress:** Did the observed result move the run forward?
  - 2 = changed state, removed a blocker, produced evidence, or narrowed the next action.
  - 1 = produced partial information or a useful negative result.
  - 0 = no new information, no state change, or avoidable churn.
- **Cost/risk adjustment:** subtract 1 when the step burned scarce resources,
  increased blast radius, bypassed a safer existing tool, or created cleanup
  debt without proportional value.

## Run-Level Review

For each run, report:

- average promise, average progress, and the share of zero-progress steps;
- the first step where promise and progress diverged;
- waits that lacked a durable checkpoint;
- repeated low-promise actions that should become a guardrail, skill, MCP tool,
  or automation change;
- the final artifact and whether its trajectory evidence is independently
  reproducible.

## Usage

- During live work: use the rubric to pick the next action when several options
  are available.
- During review: score the trace or log, then identify one concrete change that
  would improve future trajectories.
- For telemetry design: emit fields that let the rubric be computed later:
  run id, step index, step kind, intended promise, observed progress, outcome,
  cost/time, checkpoint id, and linked artifact.

## Anti-Patterns

- Scoring only the final answer and ignoring lucky or unsafe paths.
- Rewarding long activity streams when the run made no state change.
- Penalizing a failed experiment that was high-promise and produced useful
  evidence.
- Using the rubric as a vague LLM opinion without step evidence.
