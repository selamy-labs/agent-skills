---
name: using-laneq
description: Use when draining a laneq priority queue. Covers the pull loop, priority semantics, well-formed directives, and queue hygiene.
---

# Using laneq

laneq is a SQLite-backed local priority queue CLI for handing off directives
between producers (humans or agents that author work) and consumers (agents
that execute it). State survives process death. Multiple consumers can drain
the same queue concurrently via lease-based locking.

## The Pull Loop

A consumer runs a tight loop: claim the next item, execute it, mark it done,
repeat. When the queue is empty, back off or exit.

```bash
while true; do
  BODY=$(laneq next --id 2>/tmp/laneq-id)
  EXIT=$?
  if [ $EXIT -eq 3 ]; then
    # exit code 3 = queue empty; back off or exit
    sleep 30
    continue
  fi
  ID=$(cat /tmp/laneq-id | sed 's/#//')

  # --- execute the directive described in $BODY ---

  laneq done "$ID"
done
```

Key behaviors of `laneq next`:

- Atomically claims the highest-priority pending item (P0 before P1 before P2,
  FIFO within a priority) and sets its status to `taken` with a lease.
- `--id` prints `#<id>` to stderr so the consumer can capture the item ID
  separately from the body on stdout.
- Returns exit code **3** when the queue has no pending items. Exit code 0
  means an item was claimed.
- `--lease <duration>` sets how long the lease lasts (default 1800s). If the
  consumer dies, the lease expires and the item returns to `pending`
  automatically.
- `--consumer <name>` tags who took the item (visible in `laneq show`).

Extend a lease on a long-running item before it expires:

```bash
laneq touch <id> --lease 1h
```

## Priority Semantics

Three levels: **P0** (highest/most urgent), **P1** (default), **P2** (lowest).
Items are drained P0-first, then P1, then P2, FIFO within each level.

```bash
laneq push -p P0 -b "production is down -- roll back deploy"
laneq push -p P2 -b "update README badges when convenient"
```

Change priority on the fly when new evidence arrives:

```bash
laneq reprioritize <id> P0
```

## One Change, One Queue Item

One logical code change owns one queued directive from allocation through
cleanup. Attempts, corrections, and independent reviews are children of that
same item, not new top-level directives. Never allocate a second writable
task item for a review or a retry of the same change.

```bash
laneq push -p P1 -b "add retry to the upload worker"   # e.g. item #15
laneq push -p P1 -b "independent review of the upload retry" --parent 15
laneq thread-status 15   # review still belongs to #15: thread from it, do not
# push a second top-level item for the same change
# the parent stays open while the review child is unfinished
# If a claimed item stalls, laneq requeue <id> returns it to pending.
# Evidence-before-done is policy: record artifact evidence in the completion
# note. A bare laneq done with no evidence is accepted natively and is
# rejected only where an optional environment-specific guard is explicitly configured.
```

Read-only reviews may drain from an immutable snapshot with a private cache
instead of claiming the writable task item.

## Pushing Well-Formed Directives

A good directive has one clear goal plus enough context for the consumer to
act without asking follow-up questions.

**Include:**

- A single, specific objective (not a vague wish).
- Context: relevant file paths, error messages, prior attempts, or links.
- Evidence: logs, stack traces, or reproduction steps that ground the request.

**Register ownership and cleanup up front:**

- Task ID, repository, owner, and the single worktree/branch/PR identity.
- Who owns cleanup, and the terminal receipt that closes the obligation.

**Use file-based push for long bodies:**

```bash
laneq push -p P1 -f /tmp/directive.md
```

**Or pipe from stdin:**

```bash
echo "Fix the flaky retry test in client_test.go" | laneq push -p P1
```

**Thread related items** with `--parent` so downstream work stays linked:

```bash
laneq push -p P1 -b "migrate database schema" --parent 42
laneq thread-status 42   # see open/done status of the whole thread
```

## Queue Hygiene

### Partial completion: queue the scoped tail first, then close the finished stage

When a directive is partially done, the logical task stays open until the
remainder has a tracked tail. Queue the narrower follow-up for the remaining
work first, name its link or ID in the current item, then close only the
finished stage — never mark the logical parent done before its remaining-work
child exists:

```bash
laneq push -p P1 -b "remaining: update integration tests for new schema" --parent 15
laneq thread-status 15   # remainder is tracked; item #15 stays open
# while the remainder child is unfinished — never laneq done 15 here
```

### Cleanup is a queued tail, not an assumption

When a change reaches its terminal state (merged, closed, cancelled, or
superseded), the same task queues one idempotent cleanup item before the
parent obligation closes. Cleanup verifies the worktree, branch, and scratch
state natively and records a `removed`, `retained`, or `blocked` receipt with
evidence. Product delivery and cleanup completion are distinct; a merge, a
closed PR, or a producer exit never closes the cleanup obligation by itself.

For work that never allocated a writable resource (read-only review, audit,
or admin note), record `worktree/branch/PR: N/A` instead of queuing cleanup.

### Superseded items: drop with a note

When a newer directive replaces an older one, drop the stale item rather than
leaving it pending or faking it done:

```bash
laneq drop 12
laneq push -p P1 -b "replaces #12 -- use v2 API endpoint instead of v1"
```

### Reprioritize on new evidence

If an item is discovered to be blocked or less urgent, change its priority and
note why instead of silently reordering or dropping it:

```bash
laneq reprioritize 20 P2
# optionally push a note explaining the change
```

### Requeue stuck items

If a consumer fails mid-execution, requeue the item so another consumer (or a
retry) can pick it up:

```bash
laneq requeue <id>
```

### Reap stale or expired leases

Recover items that consumers abandoned without completing:

```bash
laneq reap --expired-leases     # reclaim items whose lease has passed
laneq reap --stale-seconds 7200 # reclaim items taken more than 2 hours ago
```

A missing heartbeat or an old timestamp never proves a task is terminal;
reconcile a stale lease with the owning task record before reaping it.

## Inspecting the Queue

```bash
laneq list               # pending items, ordered by priority
laneq list --all          # include taken/done/dropped items
laneq show <id>           # full detail on one item
laneq peek                # preview next item without claiming it
laneq stats               # counts by priority and status
laneq thread-status <id>  # open/done summary for a parent thread
```

## Lanes

Separate independent workstreams into lanes so consumers only drain relevant
work:

```bash
laneq push -b "deploy staging" --lane ops
laneq next --id --lane ops
```

## Producer/Consumer Pattern

- **Producers** (humans or upstream agents) push directives with priority,
  context, and optional threading.
- **Consumers** (downstream agents or scripts) drain items via the pull loop.
- The queue is the only coordination surface. Producers and consumers do not
  need to run at the same time.
- State is durable in SQLite with WAL mode. A crash loses nothing.
- Multiple consumers can run concurrently; lease-based locking prevents
  double-processing.
