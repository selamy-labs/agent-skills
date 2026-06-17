---
name: restart-resilience
description: Use when a stateful worker or service can be restarted, rescheduled, or crash mid-task. Guarantees it resumes its full in-flight and scheduled work from durable state on any restart, never silently dropping work.
---

# Restart Resilience

A restart is not a clean slate. A stateful worker that loses its place when the
process dies — drops the task it was mid-way through, forgets its queue
position, or skips scheduled work whose time passed while it was down — has a
**defect**, not bad luck. Restarts happen for ordinary reasons: deploys,
rescheduling, node pressure, crashes, out-of-memory. The bar is: after **any**
restart, the worker resumes its full work from durable state.

This is the **resume** side of durability. The shutdown signal path (draining on
SIGTERM within a grace period) is a separate, complementary concern — see
`graceful-shutdown-stateful-agents`. This skill assumes the shutdown might be
abrupt and asks: when the process comes back, does it pick up everything it owed?

## What must survive a restart

- **In-flight task state** — the unit being processed and its progress, so it
  resumes from the last checkpoint rather than restarting from zero or losing it.
- **Queue position** — committed-but-unfinished items stay claimed/visible so a
  restart re-acquires them; they are not silently dropped or double-run.
- **Scheduled work** — jobs whose fire time elapsed during downtime are detected
  and run on recovery (catch-up), not skipped because "their moment passed".
- **Session / continuity context** — the working context needed to keep going;
  on a miss in the fast path, recover it from the durable store rather than
  starting fresh.

## How to build it

1. **Checkpoint in-flight state durably** as work progresses — frequently
   enough that a restart loses at most a small, re-doable increment. A
   checkpoint that only exists in memory is not a checkpoint.
2. **Recover from the store on startup, not just from the hot cache.** When the
   in-memory/session lookup misses, fall back to reconstructing state from the
   durable record. A miss must trigger recovery, never a silent fresh start.
3. **Make resume idempotent.** A re-acquired or re-delivered task must not
   double-apply its effect. Use a stable work key + an applied/seen marker so
   replays are safe.
4. **Keep queue claims durable and reclaimable.** An in-progress claim that
   outlives the process (visibility timeout, lease, or persisted claim) lets a
   restart take the item back instead of orphaning it.
5. **Reconcile scheduled work on boot.** Compare the schedule against last-run
   markers and run anything whose window elapsed while down, exactly once.

## Verify it

Test the brutal path, not the polite one. Kill the process mid-task (not a clean
exit) and confirm on restart it: resumes the in-flight unit, re-acquires its
queue position, runs the schedule it missed, and applies nothing twice. "It
restarted and kept running" is not the test — "it restarted and lost nothing" is.

## Anti-patterns

- progress held only in memory, so a crash restarts the unit from zero or drops
  it
- treating a session/context cache miss as "new start" instead of recovering
  from the store
- scheduled jobs that silently skip any window the worker was down for
- non-idempotent resume that double-applies a re-delivered task
- queue items that vanish when the worker that claimed them dies (no lease, no
  visibility timeout, no persisted claim)
- declaring resilience proven by a clean restart only, never testing a kill
  mid-task

## Done means

After an abrupt mid-task kill, the worker recovers from durable state: it resumes
in-flight work, re-acquires its queue position, runs scheduled work it missed,
and applies each unit exactly once — proven by a kill-mid-task recovery test, not
a clean-restart test.
