---
name: continuous-issue-resolution
description: Use when an agent should continuously resolve an authorized backlog through bounded implementation, tested PRs, independent review, and evidence-backed closure. GitHub Issues may hold the queue; an existing authoritative backlog works too.
---

# Continuous Issue Resolution

Drain the user's current authorized backlog. Use GitHub Issues when they are
the repository's source of truth; otherwise use its existing task list, spec,
or work queue. Do not require issue creation or a second ledger to start work.
Refresh priorities, existing PRs, active owners, and blockers before selecting
the next item. A historical report is context, not current queue state.

## Ready Contract

Take a bounded item with a clear outcome, current acceptance criteria, an
authorized target branch, and a meaningful validation path. Honor the queue's
existing readiness and ownership signals. If its scope is ambiguous or
outside the user's authority, record the missing decision and continue other
independent authorized work. Keep completion criteria, dependencies, and
evidence in the existing item or PR; avoid a second planning layer.

Before claiming work, inspect existing branches and PRs. An open PR for the
same outcome belongs in review or repair, not in a second implementation lane.
Adopt the exact existing PR when authorized and its source is sound: inspect
its current head, diff, authorship, acceptance evidence, and remaining work.
Preserve honest attribution. Branch-name preferences alone do not justify
closing and recreating it. Use [[verify-delegated-work]] before accepting it.

## Bounded Workers And A Dedicated Shepherd

For parallel work, keep one writer per concern in isolated workspaces. Assign
one shepherd to own PR review coordination, CI failures, conflicts, qualified
merges, and post-merge follow-through. Existing user authorization and explicit
holds govern that role; assigning it grants no extra merge or deploy authority.

Workers repeat this loop while authorized backlog remains:

1. Claim one ready item using the existing ownership mechanism. Confirm the
   claim and its file/interface scope before editing; do not race another owner.
2. Implement the smallest coherent, independently reviewable change.
3. Run the required local checks and meaningful acceptance tests.
4. Open or update the PR with its exact head, evidence, and remaining gaps;
   hand it to the shepherd and release the implementation claim.
5. Select the next disjoint ready item within the work-in-progress limit.

An implementation handoff is not merged, deployed, or accepted completion.
The shepherd retains delivery ownership while workers continue useful work.
CI or review waiting on one PR must not become a controller-wide wait for
unrelated workers. Dependent work may be stacked when repository policy permits;
preserve one writer per concern and refresh affected evidence after changes.

Set work-in-progress limits from actual implementation capacity, review and CI
service rates, conflict frequency, and main health. Count waiting PRs as WIP.
When the queue backs up, finish or unblock existing work before admitting more;
do not choose a large PR count as a throughput target. Reserve capacity to
repair main when unhealthy, and obey any repository-wide stop-the-line policy.

## Triage And Decomposition

Implement now when one clear outcome fits one reviewable PR. Split only when
there are separable outcomes, excessive risk, or a validation boundary too broad
for one change. Give each slice acceptance criteria, dependencies, ownership,
and a validation expectation in the existing backlog. Create child issues only
when that is the queue's convention. Avoid tiny PRs that merely move incomplete
behavior across files or increase integration work.

## Acceptance And Verification

Anchor acceptance tests to the current governing spec, user requirement, or
observed bug. A test that restates the new implementation can preserve the
wrong behavior. Exercise the real boundary the claim depends on, including
positive controls and failure recovery when relevant.

Read exact-artifact qualification from the system of record. Reuse valid
evidence only when repository policy permits and source, tools, configuration,
inputs, and environment still match; rerun affected or time-sensitive checks
when they do not. Keep required CI intact. Do not skip, weaken, or relabel
failing checks to improve apparent throughput. A green PR alone does not prove
the combined tree, main health, deployed behavior, or the user's full journey.

## Blockers

Record the exact blocker, observed evidence, legitimate next action, and owner
on the existing item or PR. Distinguish ordinary CI/review waiting from a real
permission, credential, or external decision blocker. Do not repeat an unchanged
failing action; repair its cause or move to independent ready work. Release or
hand off the claim using the existing convention.

Use [[ephemeral-workspace-lifecycle]] to remove a clean disposable workspace or
record its retained path, owner, reason, dirty state, and review deadline.

## Closure

Close a backlog item only after its acceptance criteria are verified. Record:

- exact PR and merge state, head and resulting main commit;
- authoritative checks and meaningful behavior evidence;
- deployed or produced artifact verification when the item requires it;
- remaining gaps, next owner, and continuation for pending verification;
- workspace disposition: removed, or retained with owner, reason, and expiry.

Use repository-required post-merge qualification. Auto-merge requests, queued
merges, and checkpoints are intermediate states. Keep delivery ownership until
the required outcome is verified, while independent authorized work continues.
