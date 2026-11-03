---
name: agentic-coding-loop
description: Use when implementing code from a spec through a bounded build-test-fix loop with explicit acceptance criteria, automated checks, live verification when needed, and clear stop conditions.
---

# Agentic Coding Loop

Use this when a coding task should move through a closed loop: understand the
spec, create or identify checks, implement the smallest slice, run evidence,
fix the root cause, and repeat until the acceptance criteria are met or the
loop hits a real blocker.

This is not a general instruction to "keep trying." The loop must be bounded,
observable, and grounded in checks that would catch the behavior users or
systems depend on.

## Loop Contract

Before changing code, name:

- **Goal:** the user-visible or system-visible behavior that should change.
- **Acceptance criteria:** what must be true before the work can be called done.
- **Evidence path:** the tests, type checks, lint, API checks, browser checks,
  or runtime probes that will prove the criteria.
- **Budget:** the maximum useful attempts, time, or escalation threshold.
- **Authority boundary:** actions the agent may take autonomously and actions
  that require a human decision.

Derive acceptance criteria from the current governing spec, user request, and
observed behavior; use code and existing tests as evidence, not as the authority
for what the product should do. If criteria are missing, infer only a small,
unambiguous set. Ask for a high-impact missing decision while continuing
independent authorized work. A regression check should fail for the reported
bug and retain a valid positive control, rather than mirror the proposed fix.

## The Loop

1. **Orient.** Read the relevant code, docs, tests, issue, or spec. Identify the
   existing patterns before adding a new one.
2. **Ratchet first when possible.** If the task fixes a bug or adds observable
   behavior, create the cheapest check that should fail before the change and
   turn green after it. Use [[regression-ratchet]] and
   [[feature-coverage-not-just-line-coverage]] when they apply.
3. **Implement the smallest coherent slice.** Prefer one behavior change over a
   broad refactor. Keep mechanical cleanup separate from logic changes.
4. **Run the nearest checks.** Start with fast local checks, then escalate to
   integration, browser, API, or live verification when the behavior depends on
   those surfaces.
5. **Inspect failures.** Treat failing checks as signal. Read the actual error,
   identify the root cause, and make the smallest correction that explains it.
6. **Repeat with memory of prior attempts.** Each iteration must use evidence
   from the previous one. Do not rerun the same failing command without a code,
   data, environment, or hypothesis change.
7. **Close with evidence.** Report what changed, which artifacts were checked,
   which commands or live paths verified it, and any remaining gaps.

## Verification Ladder

Use the cheapest layer that can actually prove the claim:

| Claim | Minimum useful evidence |
| --- | --- |
| Pure code behavior | Unit or behavior test that names the behavior |
| Type or interface contract | Type check, schema check, or contract test |
| Cross-module flow | Integration test or fixture that spans the boundary |
| UI behavior | Browser check, screenshot, or interaction trace |
| API behavior | Request/response check against the target environment |
| Runtime or deployment state | Real artifact check, rollout state, or service probe |

Passing a lower layer does not prove a higher-layer claim. A green unit test
does not prove a browser flow works; a successful build does not prove the
deployed artifact changed. Use [[verify-real-artifact]] and
[[process-aware-done]] for final claims.

## Continuous Backlog Handoff

In a multi-worker backlog, hand the tested PR, exact head, acceptance evidence,
and gaps to the existing PR shepherd. That role owns review, CI, conflicts,
qualified merges, and post-merge verification; the worker may take the next
disjoint authorized slice within the WIP limit. One PR's CI wait does not stall
unrelated workers. Preserve holds and repository-required checks, and use
[[verify-delegated-work]] for policy-permitted exact-artifact evidence reuse.
PR handoff alone does not complete acceptance that requires merge or runtime proof.

## Unattended Worker Loops

When a script restarts a worker until its unit of work is done, keep the
judgment in the model and the control flow in deterministic code:

- **Directive vs feedback.** Give the worker a persistent directive, re-read
  every iteration, and a separate feedback channel consumed once. Rules that
  must outlive one iteration (decisions, scope, blocker rules) go in the
  directive; individual review comments go in feedback.
- **Mechanical exit.** A script, not the model, decides completion: the PR
  exists, required checks are green on its exact head commit, no feedback or
  unresolved review thread is pending, and the branch contains its base's
  current head. A model's claim of "done" changes nothing.
- **Narrow blocker contract.** Only a missing contract owned by another unit of
  work is a blocker. Merge conflicts, missing pins or tools, scaffolding, and
  failing gates are work. A blocked worker writes one line with a fixed prefix
  and stops; the coordinator verifies the claim before accepting it.
- **Isolation.** One worktree, branch, and state directory per loop. Bound
  every external call with a timeout, and stop processes only by the exact
  process IDs the loop recorded.
- **Move judgment left.** When the same correction recurs across loops, promote
  it into the shared directive, then into a script check or required gate.

## Stop Conditions

Stop and report success only when:

- acceptance criteria are satisfied;
- evidence came from the right layer for the claim;
- the final diff is scoped to the task; and
- any follow-up is genuinely separate from the shipped behavior.

Stop and escalate as blocked when:

- required credentials, dependencies, data, or environment access are missing;
- the same failure repeats after a bounded number of distinct hypotheses;
- the task requires a product, security, legal, financial, or operational
  decision outside the agent's authority;
- the implementation would exceed the agreed scope; or
- verification cannot be made independent of the implementation.

Call a partial result "partial" or "blocked." Do not upgrade it to done because
time ran out or the latest command looked promising.

## Output Shape

Use this compact closeout:

```text
Change: <what changed>
Evidence: <checks or live paths run>
Result: <succeeded, partial, or blocked>
Gaps: <none or explicit remaining uncertainty>
Next: <only if a separate follow-up is warranted>
```

## Anti-Patterns

- Iterating without an acceptance criterion or budget.
- Treating retries as progress when no hypothesis changed.
- Adding broad refactors inside a behavior loop.
- Accepting proxy evidence for a user-visible claim.
- Creating tests that only assert the implementation detail you just wrote.
- Continuing autonomously after the loop crosses a human decision boundary.
