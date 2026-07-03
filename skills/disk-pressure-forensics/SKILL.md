---
name: disk-pressure-forensics
description: Use when disk usage is high, df and du disagree, or an agent must diagnose full filesystems without deleting valuable work.
---

# Disk Pressure Forensics

Use this when a filesystem is full, nearly full, or `df` reports usage that
normal `du` output does not explain.

## First Principles

- `df` measures allocated blocks on the filesystem.
- `du` measures visible directory entries from the current permissions view.
- A large `df` versus `du` gap usually means deleted-open files, inaccessible
  paths, mounts, snapshots, or accounting outside the user's view.

## Diagnostic Order

1. Confirm the filesystem and mount:
   - `df -h`
   - `findmnt`
2. Measure visible usage from a privileged view when available:
   - `du -xhd1 <mount>`
   - repeat inside the largest directories
3. Check deleted but still open files:
   - `lsof +L1`
   - restart or stop only the specific process that owns the deleted file if the
     service impact is acceptable.
4. Check journal growth:
   - `journalctl --disk-usage`
   - use size-based vacuuming instead of deleting journal files by hand.
5. Look for un-reaped workspaces:
   - old clones, worktrees, virtual environments, package caches, build outputs,
     test artifacts, and temporary archives.
6. Check permission blind spots:
   - root-owned directories can make unprivileged `du` under-report.
   - suppressed errors can turn a permission problem into a false "empty" result.

## Kubernetes Local-Path Trap

On single-node k3s or similar clusters, `local-path` PersistentVolumes,
container images, writable overlay layers, pod logs, and kubelet eviction
accounting can all share the node root filesystem. A pod may report its mounted
state path as "full" even when its PVC request looks large enough, because the
PVC is just a directory on the same root disk.

When Kubernetes reports `DiskPressure=True`:

- Check the node condition and kubelet summary first:
  - `kubectl describe node <node>`
  - `kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary`
- Separate `imageFs` from local-path PVC usage:
  - containerd image/snapshot data is usually under
    `/var/lib/rancher/k3s/agent/containerd`
  - local-path PVC data is usually under `/var/lib/rancher/k3s/storage`
- If GitOps controllers are down because of disk pressure, merge the desired
  source change first, then use a short controller-gap bridge that applies the
  same declared manifest. Record it as bridge work, not a new source of truth.
- Stop recurring CronJobs that pull large images before trying deeper cleanup;
  a failed image pull can consume the last free blocks repeatedly.

## Cleanup Rules

- Prefer deleting caches, build outputs, and clearly abandoned temp work.
- Do not delete live worktrees, state directories, credentials, or logs needed
  for incident reconstruction without an explicit owner decision.
- For recurring pressure, codify cleanup as a timer or job with an allowlist and
  a dry-run mode.
- In Kubernetes incidents, prefer service-native cleanup before manual host
  deletion:
  - delete terminal `Failed` / `Succeeded` pods when their logs have been
    captured
  - use `crictl rmi --prune` for unused images
  - prune only allowlisted rebuildable cache paths such as `.cache`, `.npm`,
    `tmp`, or language-server caches; do not prune databases, sessions,
    credentials, queues, or archives unless they have an owner-approved backup

## Done

- You can explain the `df` usage with concrete evidence.
- Any cleanup target is identified by path, age, owner, and reason.
- Recurrence has either a declared guard or a recorded follow-up.
