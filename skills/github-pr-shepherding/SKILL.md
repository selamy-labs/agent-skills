---
name: github-pr-shepherding
description: Use when monitoring, unblocking, or driving GitHub pull requests through required checks, auto-merge, merge queues, runner pickup, or blocker comments. Classify PR state from GitHub as the system of record, verify auth context, preserve CI capacity, checkpoint waits, and report merged or blocked with evidence.
---

# GitHub PR Shepherding

## Overview

Shepherd a pull request from current GitHub state to either merged or explicitly blocked. Treat GitHub as the system of record, avoid CI churn, and make every wait, comment, or unblock action evidence-based.

## Workflow

1. Verify vantage before diagnosing access.
   - For local GitHub CLI reads, run `env -u GH_TOKEN -u GITHUB_TOKEN gh ...` so environment tokens do not shadow host auth.
   - Check authenticated account, repo, PR URL/number, base branch, and whether missing data is an auth problem or a real PR state.
2. Read PR state from GitHub fields, not local guesses.
   - Inspect draft state, merge state/status, review decision, auto-merge request, merge queue state, base branch, head branch, and `statusCheckRollup`.
   - For checks, capture the exact check names, conclusions/statuses, URLs, and last update times needed to justify the classification.
3. Classify the PR into exactly one current state:
   - `merged`: merged into the intended base.
   - `mergeable`: approved, non-draft, checks satisfied, and merge/auto-merge can proceed.
   - `waiting on checks`: checks are pending or queued with recent activity.
   - `failed checks`: required or blocking checks failed or were cancelled.
   - `missing review/signoff`: review decision or required approval is blocking.
   - `draft`: draft state blocks merge or queue entry.
   - `queued/no runner pickup`: queued, requested, or expected CI work has not started; distinguish normal queue delay from stale pickup.
   - `inaccessible due to auth`: GitHub data needed for classification cannot be read after vantage is verified.
4. Choose the least disruptive legitimate action.
   - Enable or preserve auto-merge when requirements are expected to clear.
   - Do not push just to retrigger checks unless there is evidence the old run is stale and a legitimate retrigger path is unavailable.
   - Do not churn red CI. Fix failures locally, then push one focused update.
   - When CI capacity is constrained, keep stacked follow-up PRs draft until their base is healthy and runner capacity is available.
   - For long waits, checkpoint with timestamped evidence and the next planned recheck instead of repeatedly polling without new information.
5. Leave blocker comments only when useful and evidenced.
   - State the exact blocker, observed GitHub status, and next legitimate unblock path.
   - Keep comments concise and omit sensitive details, credentials, private logs, or speculative blame.

## Done Criteria

Done means the PR is merged into the intended base and the real artifact is verified from the default branch or other system of record the user cares about. For bug fixes, verifier fixes, or CI fixes, include same-PR regression or ratchet evidence before calling it done.

If the PR cannot be merged, report `blocked` with the classification, the GitHub evidence observed, the exact missing requirement, and the next legitimate owner/action.
