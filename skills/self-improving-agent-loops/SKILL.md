---
name: self-improving-agent-loops
description: Use when designing a system that should autonomously generate, evaluate, and refine its own candidate solutions (agents, prompts, configurations, experiments) in a loop — rather than hand-managing each iteration. The system does the search; the human owns the architecture and the final decision.
---

# Self-Improving Agent Loops

Hand-managing every iteration of an optimization — spawning each candidate, reading
each result, tweaking the next one by hand — does not scale and wastes the operator
on work the system can do itself. The higher-leverage design is a **loop that
improves itself**: it proposes candidates, evaluates them against a measurable
objective, keeps what wins, refines from there, and repeats until it converges —
all without a human in the inner loop.

The shift is from *operating* the search to *architecting* it. The system does the
heavy lifting of generation and evaluation. The human sets the problem up, reviews
the proposals, disengages, and returns to a finished result. This generalizes far
beyond code: research directions, experiment design, hyperparameter and config
search, prompt and policy optimization, content variants — anything where "try a
candidate, score it, improve" is the unit of work.

## The Non-Negotiable: human owns architecture and the final decision

Automating the loop does **not** mean ceding judgment. The system searches; the
human decides what the search is *for* and whether the result ships.

The human always owns:

- **The objective.** What "better" means, as a measurable quantity, not a vibe.
- **The search space and constraints.** What the system is allowed to vary, the
  budget, and the guardrails it must not cross.
- **The stopping condition.** What counts as converged or good enough.
- **The final decision.** Accepting, shipping, or discarding the result. The loop
  proposes; the human disposes.

The loop owns the tedious middle — generating candidates, running evaluations,
comparing, and refining — precisely because that is the part that is mechanical and
high-volume. If a step requires taste, novel architecture, or an irreversible
real-world commitment, it belongs to the human, not the loop.

## The four-step human-in-the-loop workflow

This is the operator's interaction model. The human is in the loop at the
boundaries, not inside every iteration.

1. **Request.** Ask the system for its best candidate hypotheses or strategies for
   the stated objective. Let it propose the directions; do not pre-decide them.
2. **Review and configure.** Inspect the proposals, prune the bad ones, and set the
   parameters: objective, search space, evaluation method, budget, and stopping
   condition. This is where human judgment is spent.
3. **Disengage.** Step away completely. The loop runs unattended — generating,
   evaluating, and refining — without per-iteration babysitting. Watching it run is
   wasted operator time; trust the convergence criterion and the guardrails you set.
4. **Return and decide.** Come back to completed reports and an optimized result.
   Verify it against the real objective, then accept, ship, or reject. The decision
   is yours; the search was the system's.

The value of the model is concentrated in steps 1–2 (setup) and step 4 (decision).
Step 3 is where the leverage comes from: the system works while you do not.

## The loop

```
1. PROPOSE   candidates for the objective (the system generates these)
2. EVALUATE  each against the measurable objective, the same way every time
3. SELECT    the winners; discard or down-weight the rest
4. REFINE    generate the next round informed by what won and why
5. CHECK     converged or out of budget? if not → back to step 1
6. REPORT    surface the best result, the trajectory, and the evidence
```

Loop-until-converged, not loop-forever. Each round must be informed by the last —
a refinement step, not a fresh random draw — or it is search without learning.

## Design requirements

- **A measurable objective.** The loop can only optimize what it can score. Define
  a concrete, reproducible metric the system computes itself each round (a fraction
  of tests passing, an eval score, a benchmark number, a rubric). No human-judged score in
  the inner loop, or the loop stalls on you.
- **Automated evaluation.** Scoring must run without a human, identically each
  round, or the loop is not autonomous. If evaluation needs taste, you have not
  closed the loop — fix the metric first.
- **A bounded search space and budget.** Cap iterations, cost, and what the system
  may vary. An unbounded self-improving loop is an unbounded bill and an unbounded
  risk surface.
- **A convergence / stopping condition.** Stop on a plateau (rounds stop improving),
  a target reached, or budget exhausted. Grinding past the plateau adds cost and
  risk without gain.
- **Guardrails on autonomous action.** Anything the loop does that touches the real
  world — spending, deploying, sending, deleting — needs an explicit boundary. The
  loop may search freely inside a sandbox; crossing out of it is a human decision.
- **A trajectory record.** Keep what was tried, what each candidate scored, and why
  a winner won. Grade the journey, not just the final number — a good result from a
  broken or overfit search is not trustworthy.

## Done means

The loop ran unattended to convergence and produced a result that is **better on
the stated objective**, with a trajectory record the human can inspect, and the
human reviewed and explicitly accepted (or rejected) it. Not "the loop finished" —
the loop finishing is a claim; the verified, accepted result is the outcome.

Verify the winning candidate against the real objective yourself before accepting
it; an autonomous loop's self-reported best is still a self-report
([[verify-delegated-work]]). A self-improving loop with no human decision at the end
is not autonomy, it is an unowned process.

## When to use

- A search with many candidates and a cheap, automatable score per candidate.
- Iterative optimization (prompts, configs, strategies, experiment designs) where
  "propose, score, refine" is the natural unit and a human-in-the-inner-loop is the
  bottleneck.
- Sustained improvement efforts where the operator's time is better spent on the
  objective and the decision than on running each round.

## When not to use

- One-shot tasks with a single obvious endpoint and no search.
- Objectives that cannot be measured without human judgment each iteration — close
  that gap first, or the loop cannot run unattended.
- Irreversible real-world actions inside the inner loop. Keep the loop in a sandbox;
  the commitment is a separate, human-owned step.

## Anti-patterns

- **Babysitting the inner loop.** Watching every iteration defeats the point; the
  leverage is in disengaging. If you cannot disengage, your stopping condition or
  guardrails are not trustworthy yet — fix those, do not hover.
- **A loss you do not actually measure.** A "self-improving" loop with a vibe metric
  is not improving anything measurable ([[product-loss-descent]]).
- **Refinement that ignores prior rounds.** Re-rolling random candidates each round
  is search without learning; each round must build on what won.
- **No budget or stopping condition.** An unbounded loop burns cost and risk past
  the point of diminishing returns.
- **Letting the loop make the final call.** The system proposes; the human owns the
  architecture and the decision to ship. Removing the human from step 4 is not
  automation, it is abdication.
- **Trusting the loop's self-reported best without checking it.** A converged result
  is a claim until verified against the real objective ([[verify-delegated-work]]).
