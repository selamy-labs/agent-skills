---
name: rwo-volume-maintenance
description: Use when a job does maintenance on a single-writer (ReadWriteOnce) volume or live database that a running service still owns — pruning, compaction, retention, vacuum. Prefer snapshot/offline processing and bound the job generously so it cannot starve itself.
---

# RWO Volume Maintenance

Maintenance jobs that shrink or tidy state — log/retention pruning, DB
compaction/VACUUM, snapshot trimming — often must run against a volume the live
service still owns. On a `ReadWriteOnce` PersistentVolume or a single-writer
database, the maintenance job and the live writer **contend** for the same
resource. That contention creates a self-reinforcing trap.

## The self-defeating deadline trap

Give such a job a **too-tight** time bound (`activeDeadlineSeconds`, lock
timeout, statement timeout) and you get a vicious cycle:

1. The store grows because maintenance hasn't run.
2. The larger the store, the longer maintenance takes.
3. It now exceeds its tight deadline and is killed before finishing.
4. The store keeps growing → next run is even slower → killed again.

**The job that is supposed to shrink the store is the one that times out** — and
it gets worse every cycle. The store grows unbounded while the maintenance job
shows a steady stream of "deadline exceeded".

## Two fixes, apply both

### Bound generously, sized to the worst case

Set the deadline to the realistic worst-case runtime on the **largest** the
store is allowed to get, with margin — not the time on a fresh, small store.
A maintenance deadline is a budget for the heaviest run, not the lightest. Add
an alert if a run approaches the budget, so you tune it before it starts failing.

### Prefer offline / snapshot processing — don't contend on the live volume

Better than racing the live writer: don't race it.

- **Snapshot, then process the copy.** Take a volume snapshot or clone, mount it
  to the maintenance job, do the heavy work off the live path. The live service
  is never blocked; the job has no deadline pressure from contention.
- **Compact offline** where the engine supports it (e.g. dump/reload, or an
  offline compaction tool) instead of online vacuum that fights live traffic.
- **Stream/export, transform elsewhere, swap back** when the store can tolerate
  a brief swap.

Offline/snapshot processing removes the contention that made the tight deadline
fatal in the first place — which is why it is the real fix, and a generous
deadline is the safety net.

## Checklist

- [ ] Deadline sized to the worst-case store size, with margin and an alert.
- [ ] Heavy maintenance runs against a **snapshot/clone**, not the live volume.
- [ ] If online is unavoidable, the job is idempotent and resumable so a kill
      mid-run doesn't corrupt or lose progress.
- [ ] Store growth is monitored, so maintenance falling behind is visible before
      it becomes unrecoverable.
