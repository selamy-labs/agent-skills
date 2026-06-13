---
name: continuous-issue-resolution
description: Use when an agent should treat GitHub issues as an ongoing work queue, including triage, decomposition of oversized issues into smaller child issues, PR-based implementation, verification, and evidence-backed issue closure.
---

# Continuous Issue Resolution

Use GitHub issues as the durable queue and audit log. The loop is:

1. Pull the highest-priority ready issue.
2. Triage before implementing.
3. Implement if the issue is independently shippable.
4. Decompose if it is too broad, ambiguous, or spans multiple independent outcomes.
5. Record progress and blockers on the issue.
6. Close only with linked PR and real verification evidence.
7. Pull the next ready issue.

## Ready Contract

Only take an issue that is explicitly ready by label, assignment, milestone, or
another repo-defined signal. Do not infer readiness from an interesting title.
If no ready issues exist, report idle state and any blocked/non-ready work.

## Triage Decision

Implement now when the issue has:

- one clear outcome
- acceptance criteria or a small obvious fix
- a repo owner and target branch
- a validation path that fits in one PR

Decompose when the issue has:

- multiple separable outcomes
- vague scope or missing acceptance criteria
- cross-repo or cross-system work
- a roadmap/epic/platform shape
- risk that would make one PR hard to review or verify

## Decomposition

When decomposing:

- create 2-5 child issues, each independently shippable
- link each child back to the parent
- give every child acceptance criteria and a validation expectation
- carry priority only when still justified
- remove the parent from the ready queue and mark it as tracking/blocked-by-children
- comment on the parent with the child issue links and why decomposition was chosen

Decomposition counts as completed work for the current cycle because it turns
unworkable queue input into executable backlog.

## Implementation

For a workable issue:

- comment that you are taking it, including the intended branch/worktree
- create an isolated branch
- add or update tests where practical before implementation
- open a PR against the default branch
- enable auto-merge only when branch protection and required checks are real
- keep one writer per repo/concern

## Blockers

If blocked, comment on the issue with:

- exact blocker
- evidence observed
- next legitimate unblock path
- whether work should be retried, decomposed further, or reassigned

Then label or mark it blocked and move to the next ready issue.

## Closure

Close an issue only after the real artifact is verified. The closing comment
must include:

- PR URL and merge state
- tests/checks run
- deployed or produced artifact checked, when applicable
- any residual gap or follow-up issue

Do not use green CI alone as proof of completion when the issue requires a
runtime, user-visible, sent, deployed, or externally landed artifact.
