---
name: ephemeral-workspace-lifecycle
description: Use when creating, inheriting, handing off, or cleaning a temporary worktree, clone, task directory, build output, or local cache. Give every ephemeral resource an owner and terminal cleanup contract, preserve dirty work, and prevent scratch storage from exhausting its host.
---

# Ephemeral Workspace Lifecycle

Treat ephemeral resources as leases. Cleanup is part of task completion unless
a named owner intentionally retains the state.

## Classify Before Creating

Do not apply one deletion policy to everything called a workspace or cache.

| Resource | Ownership proof | Automatic removal authority | Removal method |
| --- | --- | --- | --- |
| Linked Git worktree | Task manifest plus matching `git worktree list --porcelain` entry | Only after the task is terminal, the tree is clean, and its commit is durably reachable | `git worktree remove`, then `git worktree prune` |
| Full temporary clone | Exclusive task manifest at a canonical managed path | Only after the task is terminal, the clone is clean, and every local commit is durably reachable from a freshly verified remote | VCS-aware inspection followed by contained directory removal |
| Exclusive task directory | Unpredictable per-task ID, creator UID, and canonical path in the manifest | Only after the task is terminal and every nested resource has its own safe disposition | Descriptor-relative, no-symlink contained removal |
| Per-task build output or cache | Exact output path and owning task in the manifest | Only after no process or active task references it | Tool-native clean or garbage collection; contained removal only when the tool has no native operation |
| Shared or global cache | Host policy and cache-manager identity, never a single task manifest | Never as part of task teardown | Cache-native size/age GC, quota, or eviction under a host-wide lock |

An exclusively owned workspace may be deleted as one task resource only when
every nested resource is also exclusive to that task. Shared caches, credential
stores, tool installations, registries, logs, and user state are separate
resources even when they appear below a convenient parent path.

## At Creation

- Use a canonical managed scratch root. Resolve and record both the root and
  resource paths before creating anything; reject paths outside the root,
  symlinked roots, mount crossings, and reused ownership markers.
- Record a random task/workspace ID, creator UID, task owner, repository,
  branch or commit, resource class, canonical path, creation time, heartbeat,
  protected state, and terminal cleanup rule.
- Create the path with permissions that prevent another user from replacing
  the resource or its manifest.
- Check free space on every backing filesystem that will receive a workspace,
  output base, or cache. Stop admitting new build-heavy work below the host's
  hard watermark.

Prefer a separate quota-controlled filesystem for high-churn workspaces and
local build caches. A full scratch volume may fail a task; it must not make the
host root filesystem unhealthy.

## Registry State

Use explicit states rather than interpreting age as task state:

```text
active -> terminal-removable -> removed
active -> terminal-retained
terminal-retained -> terminal-removable -> removed
```

The task owner or authoritative task system performs the terminal transition.
A missing heartbeat or old modification time is not a terminal transition.
Reconcile orphaned `active` records with the task system; retain and alert when
that proof is unavailable.

Before removal, atomically claim the resource for reaping and require every
task reference to be in a terminal state. After removal, replace the live
registry entry with an immutable tombstone containing the path, resource ID,
terminal evidence, removal time, and bytes reclaimed. A tombstone is audit
history, not a live reference, so it does not prevent removal or keep a lease
active.

## At Handoff

Either remove the resource or record:

- why it must remain
- who owns the next action
- the exact canonical path and current branch, commit, or task
- whether it is dirty or contains unpushed work
- an expiry or next-review time
- whether each nested cache/output is exclusive or shared

An unowned path with no expiry is a leak.

## Common Safety Gates

Run every gate immediately before mutation while holding the reaper claim:

1. Re-resolve the managed root and candidate without following a final
   symlink. Require the expected root, creator UID, resource ID, filesystem,
   and direct containment relationship to still match the manifest.
2. Refuse symlinked roots, candidates, unknown nested mounts, path traversal,
   ownership mismatches, missing/malformed manifests, and probe errors.
3. Confirm no live process, open file, current working directory, heartbeat,
   lock, or nonterminal task registry entry references the candidate.
4. Preserve durable artifacts, useful logs, handoff evidence, dirty state, and
   commits before reclaiming rebuildable data.
5. Use the resource-specific contract below. Never replace a failed native
   cleanup with an unconditional recursive delete.
6. Record the resource ID, exact path, safety evidence, bytes reclaimed,
   reason, and before/after free space in an append-only audit sink.

## Resource-Specific Removal

### Linked Git worktree

From the shared repository, confirm that the candidate is the exact canonical
path reported by `git worktree list --porcelain`. Require a clean tracked and
untracked status. Refresh the trusted remote and prove the worktree commit is
reachable from the intended durable remote ref; an unrelated merged PR number
is not proof.

```bash
cd <main-repository>
git worktree remove <canonical-workspace>
git worktree prune
```

Do not use `--force` in unattended cleanup.

### Full temporary clone

Require a clean tracked and untracked status and enumerate every local ref and
commit that is not reachable from the freshly fetched trusted remote. Retain
the clone if fetch, reachability, submodule, nested-repository, or object-health
checks fail. Remove it only by an implementation that anchors traversal to an
already-open managed-root directory, rejects symlinks and mount crossings, and
cannot escape through path replacement.

### Exclusive task directory

Require the unpredictable resource ID and creator UID from the protected
manifest. Inventory nested repositories, mounts, sockets, outputs, and caches;
apply the matching contract to each. Remove the directory only when the
inventory contains no unknown or shared resource. Use descriptor-relative
deletion that does not follow symlinks and re-check the directory identity
immediately before unlinking it.

### Build outputs and caches

For a per-task output base or cache, use the build tool's shutdown, clean,
expunge, or garbage collector after verifying that no live process uses it.
When no native operation exists, delete only the exact canonical output path
recorded in the manifest with the common containment gates.

For shared/global caches, task completion may release a reference but must not
delete the cache tree. Use the cache manager's native GC under a host-wide lock
with a size budget, minimum free-space target, and protected active entries.
If native GC is unavailable, alert and design a cache-specific reaper; do not
generalize the workspace recursive-delete path.

## Scheduled Reaping

Run cleanup on authoritative terminal task transitions and as a periodic
safety net.

- Remove verified `terminal-removable` exclusive resources immediately.
- Reconcile stale `active` leases with the task system; never infer terminal
  state from age alone.
- Alert on dirty, protected, retained, unknown-owner, shared, or probe-failed
  resources.
- Keep an allowlist of managed roots and resource classes, dry-run output, an
  append-only audit log, and mutual exclusion between admission and reaping.
- Trigger GC before a filesystem is critical. Check the actual backing
  filesystems, not only `/`.

## Done

Report one of:

- `removed`: resource ID, canonical path, tombstone, and native/contained
  cleanup evidence
- `retained`: owner, reason, dirty/protected state, and expiry
- `blocked`: failed safety probe and the next action

Do not claim the task fully complete while its ephemeral resources have no
verified disposition.
