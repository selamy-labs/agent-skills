---
name: github-pr-shepherding
description: Use when monitoring, unblocking, or driving GitHub PRs or native stacks through required checks, group merge, merge queues, auto-merge for ordinary PRs, runner pickup, or blocker comments. Verify current GitHub state and report merged or blocked with evidence.
metadata:
  audience: universal
  helpers: scripts/review_monitor.py
---

# GitHub PR Shepherding


## Deterministic helper

Call the bundled helper from this skill directory; see `references/helper.md`
for input contracts and examples. It collects mechanical evidence. Keep readiness,
authority and semantic decisions with the reviewer. Missing evidence remains pending.


## Overview

Shepherd a pull request from current GitHub state to either merged or explicitly blocked. Treat GitHub as the system of record, avoid CI churn, and make every wait, comment, or unblock action evidence-based.

## Workflow

1. Verify vantage before diagnosing access.
   - For local GitHub CLI reads, run `env -u GH_TOKEN -u GITHUB_TOKEN gh ...` so environment tokens do not shadow host auth.
   - Check authenticated account, repo, PR URL/number, base branch, and whether missing data is an auth problem or a real PR state.
2. Read PR state from GitHub fields, not local guesses.
   - Detect native stack membership before choosing merge commands. Follow `stacked-diff-discipline` for the ordered members, trunk, selected prefix, and per-member heads/bases/checks. Distinguish a native stack from ordinary parent-based PRs.
   - Inspect draft state, merge state/status, review decision, auto-merge request, merge queue state, base branch, head branch, and `statusCheckRollup`.
   - For checks, capture the exact check names, conclusions/statuses, URLs, and last update times needed to justify the classification.
3. Classify the PR into exactly one current state:
   - `merged`: merged into the intended base.
   - `mergeable`: approved, non-draft, checks satisfied, and the applicable PR or stack merge can proceed. A native member also requires every unmerged member below it to be ready.
   - `waiting on checks`: checks are pending or queued with recent activity.
   - `failed checks`: required or blocking checks failed or were cancelled.
   - `missing review/signoff`: review decision or required approval is blocking.
   - `draft`: draft state blocks merge or queue entry.
   - `queued/no runner pickup`: queued, requested, or expected CI work has not started; distinguish normal queue delay from stale pickup.
   - `inaccessible due to auth`: GitHub data needed for classification cannot be read after vantage is verified.
4. Choose the least disruptive legitimate action.
   - For an ordinary PR, enable or preserve supported auto-merge when requirements are expected to clear. Native stacks do not support auto-merge: use `gh stack merge <highest-included-pr> --yes` or its native queue path after verifying the selected prefix. Do not use a per-PR auto-merge loop for a native stack.
   - Do not push just to retrigger checks unless there is evidence the old run is stale and a legitimate retrigger path is unavailable.
   - Do not churn red CI. Fix failures locally, then push one focused update.
   - Bound CI using actual workflow and runner controls. Drafts suppress jobs only when workflows implement that behavior. Promote all members of a ready merge prefix within capacity; parent merge is not a universal prerequisite for child review or CI.
   - A lower-head, trunk, order, or membership change requires fresh applicable evidence. For a queued stack's normal trunk advancement, verify the queue's current merge-group SHA/checks without competing branch rewrites. Queue admission remains `queued` until GitHub confirms actual merges.
   - For long waits, checkpoint with timestamped evidence and the next planned recheck instead of repeatedly polling without new information.
5. Leave blocker comments only when useful and evidenced.
   - State the exact blocker, observed GitHub status, and next legitimate unblock path.
   - Keep comments concise and omit sensitive details, credentials, private logs, or speculative blame.

## Done Criteria

Done means the PR is merged into the intended base and the real artifact is verified from the default branch or other system of record the user cares about. For bug fixes, verifier fixes, or CI fixes, include same-PR regression or ratchet evidence before calling it done.

For a stack, verify every included PR and map it to the resulting trunk SHA or
queue group and post-merge evidence. Verify the combined artifact and remaining
members' new bases/heads; one green member does not establish stack delivery.

If the PR cannot be merged, report `blocked` with the classification, the GitHub evidence observed, the exact missing requirement, and the next legitimate owner/action.
