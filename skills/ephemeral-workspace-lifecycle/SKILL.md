---
name: ephemeral-workspace-lifecycle
description: Use when creating, inheriting, handing off, or cleaning a temporary worktree, clone, task directory, build output, or local cache. Give every ephemeral workspace an owner and terminal cleanup contract, preserve dirty work, and prevent scratch storage from exhausting its host.
---

# Ephemeral Workspace Lifecycle

Treat workspaces as leased resources, not permanent side effects. Cleanup is
part of task completion unless a named owner intentionally retains the state.

## At Creation

- Use a canonical managed scratch root instead of scattering work across
  persistent checkouts or arbitrary temporary paths.
- Record the task ID, owner, repository, branch, workspace path, build-output
  path, creation time, heartbeat or last activity, and protected status.
- Declare terminal behavior: remove on merge, closure, cancellation, failed
  experiment, or another explicit terminal state.
- Check free-space headroom before starting a build-heavy task. Stop admitting
  new scratch work below the host's hard watermark.

Prefer a separate quota-controlled filesystem for high-churn workspaces and
local build caches. A full scratch volume may fail a task; it must not make the
host root filesystem unhealthy.

## At Handoff

Either remove the workspace or record:

- why it must remain
- who owns the next action
- the exact path and current branch or task
- whether it is dirty or contains unpushed work
- an expiry or next-review time

An unowned path with no expiry is a leak.

## Safe Removal

1. Preserve the durable artifact, useful logs, and handoff evidence.
2. Confirm no live process, open file, current working directory, heartbeat,
   lock, or task registry still references the path.
3. Inspect repository state. Never automatically delete dirty, unpushed, or
   unmerged work.
4. From outside a linked worktree, use the VCS-native removal operation, then
   prune stale registrations. Do not replace a failed native removal with an
   unconditional recursive delete.
5. Remove rebuildable build outputs and caches through their native cleanup or
   garbage-collection mechanism when available.
6. Record the removed path, bytes reclaimed, reason, and before/after free
   space.

For Git worktrees, the normal sequence is:

```bash
cd <main-repository>
git worktree remove <workspace>
git worktree prune
```

## Scheduled Reaping

Run cleanup on terminal task transitions and as a periodic safety net.

- Remove verified terminal workspaces immediately.
- Reap clean, inactive, unregistered workspaces after a short grace period.
- Alert on dirty, protected, unknown-owner, or probe-failed paths; do not
  delete them automatically.
- Use age only after ownership and liveness checks. Directory modification time
  alone is not proof that nested work is inactive.
- Keep an allowlist of managed roots and resource types, with dry-run output
  and an audit log.

Trigger cleanup before the filesystem is critical. A low-space alarm that only
reports after cleanup cannot preserve headroom by itself.

## Done

Report one of:

- `removed`: path and evidence that native cleanup succeeded
- `retained`: owner, reason, dirty/protected state, and expiry
- `blocked`: failed safety probe and the next action

Do not claim the task fully complete while its ephemeral resources have no
verified disposition.
