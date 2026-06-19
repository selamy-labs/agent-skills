---
name: full-disk-forensics
description: Use when a filesystem is full or nearly full, especially when df and du disagree. Checks deleted-open files, privilege blind spots, journald, temp worktrees, caches, and safe cleanup evidence.
---

# Full Disk Forensics

Use this skill when `df` reports a full filesystem, when `du` totals do not
explain used space, or when agents/builds fail from disk pressure.

## First Snapshot

Capture the facts before cleaning:

```sh
df -h
df -ih
mount | sort
```

Then identify the affected mountpoint and keep every later command scoped to
that filesystem unless you intentionally broaden the search.

## Explain `df` Versus `du`

If `df` says space is used but `du` cannot find it, check:

- Deleted but still-open files: `sudo lsof +L1`.
- Permission blind spots: rerun targeted `du` with enough privilege to see
  root-owned directories.
- Mount boundaries: use `du -x` when staying on one filesystem matters.
- Reserved blocks and filesystem metadata when the gap is small.

The `df`/`du` gap is a diagnostic signal, not an inconsistency to ignore.

## Common Large Sources

Check the usual growth points before deleting anything unusual:

- Journald: `journalctl --disk-usage`.
- Container images, writable layers, and build caches.
- Package-manager caches.
- Test artifacts, coverage reports, core dumps, and crash logs.
- Temporary clones, worktrees, virtualenvs, and dependency caches.
- Old release artifacts or local database snapshots.

## Cleanup Rules

- Prefer service-native cleanup commands over manual deletion.
- Bound cache cleanup to paths and tools whose ownership is understood.
- For deleted-open files, restart or signal the owning process only when that
  is operationally safe.
- Do not remove active worktrees, persistent volumes, databases, or unknown
  root-owned paths just because they are large.

## Done Criteria

Report:

- The full mountpoint before and after.
- The largest confirmed source or the unexplained gap that remains.
- The cleanup command or declared follow-up.
- Any process restart needed to release deleted-open files.
