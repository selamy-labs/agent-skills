---
name: bazel-ephemeral-sandbox
description: Use when an agent or CI task runs Bazel on a shared host. Run each invocation in a per-task worktree with a private native output_user_root, a capped owner-only disk cache, free-space guards, a durable lease, and teardown that retains dirty or unpushed work.
---

# Bazel Ephemeral Sandbox

Bazel keeps a full output base (server, install base, execroot, external
repositories) per workspace path and output user root. Parallel agents that
each check out a repository leave one multi-gigabyte output base per checkout,
and nothing reaps them. On a shared host this fills the root disk.

Run every Bazel invocation through the bundled wrapper so each task gets its
own output root that dies with the task. Action outputs are reused through one
disk cache that the wrapper brings back under a cap after each build. It is a
post-run cap, not a hard limit: a cache can exceed it by what in-flight builds
add before the next collection.

## When To Use

- Any Bazel invocation by an agent, worktree, or CI job on a shared host.
- A host has run out of space because of `~/.cache/bazel` or abandoned output
  bases.
- A gate runner must build an exact candidate commit and fail closed when the
  branch is missing, stale, or at a different SHA.

This wrapper builds committed Git refs inside an isolated linked worktree
(`<root>/<id>/src`); when a caller must run Bazel directly against uncommitted
edits in an existing working tree, use an in-place output-base gate that pins
`--output_user_root` and `--output_base` without creating a linked Git worktree.

The sandbox gives up a warm analysis cache between invocations; the shared
disk cache keeps rebuilds cheap.

## Contract

The resource model and removal rules are those of
[ephemeral-workspace-lifecycle](../ephemeral-workspace-lifecycle/SKILL.md).
That skill is binding; this one applies it to Bazel.

| Resource | Class | Disposition |
| --- | --- | --- |
| `<root>/<id>/out` | Per-task build output, including the per-task repository cache | Scoped `bazel shutdown`. Then the recorded server process must be proven gone. The identity recorded in the lease (PID plus kernel start time) is used; the PID file is consulted only when no verified identity was recorded, and then only a process of the caller's UID holding the output root by a handle counts as the server. If shutdown failed or timed out, that exact process gets TERM, a bounded wait, and KILL. The root is deleted only when the server is proven gone and no process holds it; otherwise it is retained as `blocked` |
| `<root>/<id>/src` | Linked Git worktree | Disposed only after the output root is released. Removed with `git worktree remove` (never `--force`) only when it passes the same containment and holder gates as the output root, is clean (ignored files and hidden index state included), and HEAD plus every commit in its HEAD reflog is reachable from a fresh explicit fetch of the branch or base, or HEAD is covered by a verified bundle. Otherwise it is retained with the reason. A branch the sandbox created is then deleted with `git branch -d` |
| `<root>/disk-cache` | Shared cache, owner-only | Never deleted by a task. Builds use it under a shared lock; the collector caps it (see below) |
| `<root>/<id>/manifest.json`, `lease.lock` | Durable lease | Exclusively locked for the owner's lifetime. It records the owner PID, start time, and session, plus the Bazel server PID and start time once known. Removed on `removed`; otherwise rewritten as `terminal-retained` or `terminal-blocked` with the owner, reason, next action, and next review time |
| Audit file | Append-only | One JSON line per invocation, with per-resource outcomes and the exact exit code. The wrapper refuses to start a build without it |

Guarantees:

- The managed root and the disk cache must be `0700` directories owned by the
  caller and not symlinks, or the run exits 77.
- Each task gets a random 16-hex ID, its own `--output_user_root`, and a pinned
  `--output_base` beneath it. The caller may not supply `--output_user_root`,
  `--output_base`, `--install_base`, `--server_jvm_out`,
  `--failure_detail_out`, `--disk_cache`, or `--repository_cache` (exit 64).
  The wrapper pins `--install_base`, `--server_jvm_out`, and
  `--failure_detail_out` under the output root on the command line, which
  overrides any `.bazelrc` `startup` line. Bazel's own startup and cache paths
  (output user root, output base, install base, server log, failure detail,
  disk and repository caches) therefore stay under the output root. The wrapper
  does not control paths a build or the caller directs elsewhere:
  `--sandbox_base`, `--test_tmpdir`, `--profile`, execution-log and BEP files,
  a JVM `HeapDumpPath`, whatever `bazel run` targets write, and Bazelisk's
  download cache. The cost: each task extracts its own install
  base and downloads its external repositories again (only the disk cache is
  shared). Every
  command except `shutdown`, `version`, and `help` gets the sandbox's
  `--disk_cache` and a per-task `--repository_cache`. Build-family commands
  (`build`, `test`, `run`, `coverage`, `cquery`, `aquery`) also get
  `--symlink_prefix=/`. These flags go after the caller's options, before any
  `--` that starts a run target's arguments, so no `.bazelrc` or `--config`
  expansion can override them. `BAZEL_SANDBOX_DISK_CACHE=off` passes
  `--disk_cache=`.
- Apart from those output and cache paths, the wrapper changes no Bazel
  policy. It adds no `--lockfile_mode`, registry, or module-override flag, and
  passes every other caller and `.bazelrc` option through. A
  `MODULE.bazel.lock` that Bazel creates or updates is a source change, so the
  worktree is retained. Commit the lockfile in the repository's durable
  baseline.
- One `try`/`finally` runs teardown on success, failure, exception, and
  signals. While Bazel runs, `SIGINT`, `SIGTERM`, and `SIGHUP` are forwarded
  to the Bazel client, escalated to `SIGKILL` after the kill wait, and end
  with exit 128 + signal after teardown. A client killed by a signal also
  exits 128 + signal. Outside that window a signal is only recorded. In a
  build invocation (waiting at the cache gate, teardown, the post-run
  collection) the wrapper finishes the step and then exits 128 + signal, so it
  can outlive a caller's grace period, and a signal after a successful build
  still turns the exit status into 128 + signal. A wrapper refusal raised after
  the signal wins instead: in gate mode (`BAZEL_SANDBOX_COMMIT` set) a terminal
  Ctrl-C during the candidate fetch kills `git fetch`, and that run exits 75
  (fetch failed); without a pinned candidate the failed fetch is only logged.
  `--reap` and `--gc` ignore signals: a reap itself returns 0 (configuration
  refusals 64, 70, and 77 still apply), and `--gc` exits 0 when the cache ends
  at or below the cap, 75 when deferred or still above cap after failed unlinks,
  and 77 if the root or disk cache is unsafe. A terminal Ctrl-C reaches the client both
  directly (same foreground process group) and through forwarding, so Bazel
  sees two interrupts for one keypress; a third would kill its server.
- Admission: free space below the minimum on any backing filesystem (the
  managed root, and the disk cache when it is elsewhere) refuses the task
  before a task directory or worktree exists (exit 75, audited as `blocked`).
- Watchdog: while Bazel runs, free space below the abort floor on any backing
  filesystem, or three unreadable probes in a row, stops the client and exits
  75 after teardown.
- Deletion is descriptor-relative from the managed root with `O_NOFOLLOW`. It
  re-proves the canonical root, its mode, owner UID, and the task directory
  inode from the lease when opening the task, and checks the device of every
  entry it walks. A path swapped for a symlink or another directory is
  refused, not followed. Not detected: a bind mount from the same filesystem
  inside `out/` or `src/` (that needs mount-namespace inspection the wrapper
  does not do); its source would be walked and deleted.
- Process probes fail closed. Every `/proc` read separates an entry that
  vanished (`ENOENT`/`ESRCH`: the process is gone and is skipped) from one that
  cannot be read (`EACCES` and other errors: UNKNOWN, never dead), so an
  unreadable PID cannot prove a server terminated. A zombie leader is gone only
  when every thread in `/proc/<pid>/task` is a zombie too; a thread group whose
  main thread exited while other threads run is alive, and those threads' cwd,
  root, executable, and open files are checked. `stat` is parsed as bytes after
  the last `)`, so an odd command name cannot shift its fields or raise. A holder is a
  process whose command line names the path, or a process of the caller's UID
  whose cwd, root, executable, or an open file lies under it. A process of the
  caller's UID that cannot be inspected counts as an unknown holder, and the
  path is retained, when it is in the task owner's session or the Bazel
  server's recorded session, when its parent chain reaches the owner or the
  server (Bazel's process-wrapper and linux-sandbox call `setsid()` but stay
  children of the server), or when its session or parent chain cannot be
  read. Before such a process is counted it is re-probed for up to a second
  (and at most two seconds per scan in total): it is skipped if it vanishes or
  while every live thread stays exiting across at least two probes spanning a
  sleep window (`PF_EXITING` in the flags field of each thread's `stat`; an
  exiting thread refuses access to its handles until it is a zombie, though a
  thread wedged exiting past the 1 s window is still skipped), and counted UNKNOWN
  if it stays alive and uninspectable,
  including when its `stat` cannot be read. Real scope of that rule: an uninspectable process that has both left
  those sessions and been reparented away from the owner and the server, and
  holds the path only through a handle, is not detected. The parent-chain check
  only helps while the server is alive (the reaper's pre-scan). At a normal
  teardown the server is already shut down, so a non-dumpable, `setsid()`
  sandboxed action child that outlives it has been reparented and is ignored:
  that is the residual case. Unrelated non-dumpable session processes of the same
  UID (`sshd`, `systemd --user`, `gpg-agent`) are ignored for the same reason.
  Processes of every other UID, root included, are checked by command line
  only: other non-root UIDs cannot open paths inside the `0700` root, and root
  is trusted.
- `SIGKILL` and host loss bypass teardown. The kernel releases the lease and
  cache locks, the lease stays `active`, and the cache collector and `--reap`
  both treat it as orphaned.
- The exit status is Bazel's. The wrapper's own codes are 64 (usage or
  configuration), 65 (candidate ref absent or not the expected SHA), 70 (setup
  failed or no audit log), 75 (disk pressure, a failed candidate fetch, or a
  deferred collection), and 77 (unsafe root or cache).

## Disk Cache Cap

Bazel's own `--experimental_disk_cache_gc_max_size` collects only after the
server has idled for minutes. The sandbox shuts the server down at once, so
that flag never fires. Bazel's on-demand collector `//src/tools/diskcache:gc`
ships no prebuilt binary and must be built from a Bazel source checkout (it
links Bazel's internal VFS, remote, and Unix JNI libraries). That is too heavy
a bootstrap for every host, so the wrapper carries an equivalent:

- Scope: only regular files owned by the caller under the cache's own `cas/`
  and `ac/`, opened descriptor-relative, without following symlinks or
  crossing filesystems. Anything else in the cache is never touched.
- Metric: the total apparent size (`st_size`) of those entries. Entries go
  least recently used first, by the later of atime and mtime; Bazel refreshes
  them on cache hits. `bytes_after` is the measured remainder after the
  successful unlinks: it is at most `BAZEL_SANDBOX_DISK_CACHE_MAX` when every
  needed unlink succeeds, and above it otherwise. Writing the eviction log is
  best-effort and never changes the result.
- Locking: a build passes `<cache>.gate` (shared) and then holds `<cache>.lock`
  shared until its own teardown ends. The collector takes the gate exclusively,
  which stops new builds from entering, waits for running builds to drain from
  the lock, and backs off within `BAZEL_SANDBOX_GC_LOCK_WAIT`. A sustained
  stream of short builds therefore cannot starve it; builds that each run
  longer than that wait can, and then every collection defers. A build that
  arrives while a collection is pending or running waits, before Bazel starts
  and without the watchdog, for up to `BAZEL_SANDBOX_GC_LOCK_WAIT` plus the
  time the collector needs to scan and evict. Collectors serialize on
  `<cache>.gc`. A run that finds a collection already running records
  `delegated` instead of queueing. That collection scans only after this run
  released its shared lock, so if it completes it covers this run's entries;
  if it defers, nothing re-collects until the next run or `--gc`.
- Orphans: the locks die with their owner's process, but a lease does not. The
  collector defers while any lease is orphaned (`active` with a free lock),
  keeps an output root (`terminal-blocked` or `terminal-retained` with `out/`
  present), or has a missing or unprobeable lease lock, because its Bazel
  processes might still use the cache. One such lease defers collection for
  the whole cache. `--reap` resolves orphaned and output-retaining leases. A
  lease whose lock is missing or unprobeable is reported as `blocked` and
  needs an operator.
- Deferral and errors never change Bazel's exit status. They are recorded in
  the audit line's `cache` object, and the next run or `--gc` catches up.
  `--gc` exits 75 when it has to defer.
- Evidence: every run's audit line carries the cache bytes before and after
  collection; `BAZEL_SANDBOX_GC_LOG` appends one JSON line per evicted entry.

## Run It

Call it from inside the repository, or set `BAZEL_SANDBOX_REPO`:

```bash
scripts/bazel-sandbox.sh my-task-branch -- test //pkg/...
scripts/bazel-sandbox.sh my-task-branch -- --host_jvm_args=-Xmx4g build //:app
```

`scripts/bazel-sandbox.sh` execs `scripts/bazel_sandbox.py`, keeping its PID
for signal delivery. The wrapper first fetches `refs/heads/<branch>` explicitly
from the remote into a private ref; stale remote-tracking refs are never used.
An existing local branch (`refs/heads/<branch>`) is checked out detached at its
current commit so a branch already checked out in the caller's worktree never
collides with `git worktree add`. Otherwise the freshly fetched remote tip
is used. A branch that exists nowhere is created from a fresh fetch of the
base, with a warning. Give startup options as `--name=value` before the
command (two-token value startup flags such as `--bazelrc /path` are refused
with exit 64). `bazelisk` is used when present, so `.bazelversion` is honored.

Gate runners pin the exact candidate:

```bash
BAZEL_SANDBOX_COMMIT=<40-hex sha> scripts/bazel-sandbox.sh pr-branch -- test //...
```

The remote branch must exist and its freshly fetched tip must equal the SHA.
Otherwise the run exits 65 before Bazel starts; a fetch that fails or times
out exits 75 with the reason instead. The worktree is a detached checkout of
exactly that commit.

To let teardown remove a worktree whose commits were never pushed, write a
bundle outside the managed root (it must cover every reflog commit, so
reset-away commits keep the worktree retained):

```bash
BAZEL_SANDBOX_BUNDLE=/durable/evidence/task-123.bundle \
  scripts/bazel-sandbox.sh my-task-branch -- build //...
```

## Reap And Collect

```bash
scripts/bazel-sandbox.sh --reap   # after a crash or host loss, and from an existing timer
scripts/bazel-sandbox.sh --gc     # cache cap only
```

`--reap` re-evaluates every lease whose lock it can take: `active` leases left
by a dead owner, and `terminal-retained` and `terminal-blocked` leases, so a
blocked lease is not stuck. It acts only when the recorded owner PID with its
kernel start time is gone. It also requires that no process other than that
output root's own Bazel server references the output root, and that no
process is an unknown holder under the probe rule above. It then applies the
same teardown as a normal exit.
The orphaned server is identified by the PID, start time, and session
persisted in the lease (recorded only once that process held the output root by
a handle); that recorded identity always wins over the PID file. Only when no
identity was recorded is the PID file used, and then only a process of the
caller's UID holding the output root by a handle counts as the server. It is
terminated by that exact identity; a PID that now names a different process
(another start time, or no handle on the output root) is never signalled. When no server was
ever recorded (Bazel failed before writing its PID file), the reaper cannot run
`bazel shutdown`; it releases the output root only after a complete process
scan finds nothing referencing it, and says so in the reason. A lease that is
locked or whose owner runs is left in
place, as is one whose client or another process still references the output
root. So is a lease whose manifest is malformed or names another location or
UID. A task directory that holds only `lease.lock.new` (its owner died
between creating and publishing the lease, before any worktree or Bazel
process existed) is removed when its name and owner are the sandbox's and
nobody holds that lock; one younger than a minute is left alone as a guard
against the creation window. Every decision is printed and each teardown audited. Age alone never
makes a lease removable. Both modes finish with a collection. `next_review`
in a retained manifest is a date for operators; the wrapper does not act on
it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BAZEL_SANDBOX_ROOT` | `${XDG_CACHE_HOME:-$HOME/.cache}/bazel-sandboxes` | Managed root |
| `BAZEL_SANDBOX_COMMIT` | unset | Exact candidate SHA (gate mode) |
| `BAZEL_SANDBOX_DISK_CACHE` | `<root>/disk-cache` | Shared disk cache, or `off` |
| `BAZEL_SANDBOX_DISK_CACHE_MAX` | `20G` | Post-run cap (apparent bytes of `cas/` and `ac/`) |
| `BAZEL_SANDBOX_GC_LOCK_WAIT` | `60` | Seconds the collector waits for running builds |
| `BAZEL_SANDBOX_GC_LOG` | unset | JSON Lines file of evicted entries |
| `BAZEL_SANDBOX_AUDIT` | `${XDG_STATE_HOME:-$HOME/.local/state}/bazel-sandboxes/audit.jsonl` | Audit file, outside the root so refusals are recorded |
| `BAZEL_SANDBOX_MIN_FREE` | `40G` | Admission minimum free space |
| `BAZEL_SANDBOX_ABORT_FREE` | `20G` | Watchdog abort floor; must be below the minimum |
| `BAZEL_SANDBOX_WATCH_INTERVAL` | `15` | Watchdog period, seconds |
| `BAZEL_SANDBOX_SHUTDOWN_TIMEOUT` | `120` | Seconds allowed for `bazel shutdown` |
| `BAZEL_SANDBOX_KILL_WAIT` | `10` | Seconds between TERM and KILL |
| `BAZEL_SANDBOX_FETCH_TIMEOUT` | `60` | Seconds allowed for each `git fetch` |
| `BAZEL_SANDBOX_RETAIN_REVIEW` | `604800` | Seconds until a retained or blocked lease's next review |
| `BAZEL_SANDBOX_REMOTE` | `origin` | Remote proving durability |
| `BAZEL_SANDBOX_BASE` | `main` | Base for a new branch and a durability ref |
| `BAZEL_SANDBOX_BUNDLE` | unset | Bundle path that makes unpushed work removable |
| `BAZEL_SANDBOX_REPO` | current directory | Repository to take the worktree from |
| `BAZEL_SANDBOX_TASK` | unset | Task-system identity recorded in the lease |

Sizes take an optional `K`, `M`, `G`, or `T` suffix and must be positive; an
invalid value exits 64. Keep the cache cap well below the free-space floors.
The admission gate is a check, not a reservation: parallel admissions can
still overrun, which the watchdog then stops. Requires Linux, Python 3.11+
(standard library only), Git, and `df`.

## Read The Result

Each invocation appends one line to the audit file, for example:

```json
{"mode":"run","ts":"...","id":"9f2c...","branch":"my-task-branch","head":"<sha>","outcome":"retained","resources":{"output_root":"removed","worktree":"retained"},"reason":"worktree has modified, untracked or ignored files","exit_code":1,"bytes_reclaimed":5368709120,"df_before":171798691840,"df_after":177167400960,"worktree":"...","output_user_root":"...","server_pid":4242,"shutdown_rc":0,"server_gone":true,"cache":{"enabled":true,"collected":true,"cap_bytes":21474836480,"bytes_before":22548578304,"bytes_after":21474000000,"entries_before":9120,"evicted_entries":310,"evicted_bytes":1074578304}}
```

`resources` gives each resource's own disposition: `removed`, `retained`,
`blocked`, or `absent` (never created). `bytes_reclaimed` is the allocated size
of single-link files removed, measured the same way for both resources.
`df_before` and `df_after` are `null` when unreadable.

- `removed`: the output root and worktree are gone; the audit line is the
  tombstone.
- `retained`: the worktree holds dirty or unpushed work. Push or bundle the
  work, then run `--reap`; the manifest names the owner, reason, next action,
  and next review time.
- `blocked`: a safety check failed or the task never started: refused
  admission, usage error, unsafe root, candidate mismatch, setup failure,
  unproven server shutdown, a live or uninspectable holder, or a swapped
  path. Investigate before deleting anything. `--reap` re-evaluates it.

## Why Native `--output_user_root`

- It is Bazel's own isolation boundary. Server, install base, and every output
  base for the task live under one path, so a scoped `bazel shutdown` plus one
  contained removal reclaims all of it.
- No container runtime, image layers, or overlay storage, which on a full
  disk add their own pressure and their own cleanup problem.
- Same toolchains, credentials, and sandboxing (`linux-sandbox`) as a normal
  local build, so results match what developers and CI see.

## Verify

Before trusting a run, check its audit line: `server_gone` is `true`, and
`cache.bytes_after` is at most `cache.cap_bytes` (or the collection is marked
deferred or delegated with its reason; a `bytes_after` above the cap means an
unlink failed). A disk incident is closed only when `--reap`
reports nothing reclaimable and every remaining task directory has a
`terminal-retained` manifest with an owner action.
