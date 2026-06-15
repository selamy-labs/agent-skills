---
name: graceful-shutdown-stateful-agents
description: Make a stateful container/agent survive SIGTERM→SIGKILL on Kubernetes without losing in-flight work or corrupting on-disk state.
---

# graceful-shutdown-stateful-agents

When K8s evicts, rolls, or scales down a pod it sends **SIGTERM**, waits `terminationGracePeriodSeconds`, then **SIGKILL**. A stateful agent that ignores this loses in-flight work and can corrupt its store. Default behavior is NOT safe — you must design for the signal.

## The shutdown contract
1. **Catch SIGTERM** in the process and start an ORDERLY drain — stop accepting new work, finish or checkpoint in-flight work, flush + fsync state, close connections, exit 0. Do NOT let the default handler hard-kill you.
2. **Size `terminationGracePeriodSeconds`** to the real worst-case drain (measure it). Too short → SIGKILL mid-flush → corruption. Too long → slow rollouts. The grace period is a budget; your drain must finish inside it with margin.
3. **`preStop` hook** for work the app can't self-trigger (e.g. deregister from the load balancer before draining, or signal a sidecar). A common pattern is a `preStop` that sends an app-specific signal (e.g. SIGUSR1) to begin drain while the LB removes the endpoint — closing the race where traffic still arrives after SIGTERM.
4. **Assume SIGKILL can still win.** The grace period is best-effort; node pressure or a stuck drain can cut it short. So durability cannot DEPEND on a clean shutdown — it must survive an abrupt kill too (next section).

## Durability that survives an abrupt kill
- **Write-ahead log / journaling** (e.g. SQLite WAL mode): commits are durable on `fsync`, and recovery replays the WAL after a crash — so a SIGKILL mid-operation leaves a recoverable, not corrupt, store.
- **Atomic writes**: write to a temp file + `fsync` + atomic `rename`; never partially overwrite a live file.
- **Idempotent + checkpointed work**: persist progress so a restarted pod resumes, not restarts; make replays safe (a re-delivered task must not double-apply).
- **fsync at the boundaries that matter** — a buffered write the OS hasn't flushed is gone on SIGKILL.

## Verify it (don't assume)
Test the real signal path: send SIGTERM, confirm the process drains and exits 0 within the grace period; then test the brutal path — SIGKILL mid-write — and confirm the store recovers cleanly on restart (WAL replay, no corruption, no lost committed work). "It shut down cleanly once" is not the test; the abrupt kill is.

## DONE means
SIGTERM triggers an orderly drain that finishes inside `terminationGracePeriodSeconds`; a `preStop` closes the traffic/drain race; and state is durable under SIGKILL (WAL/atomic-rename/fsync), proven by a kill-mid-write recovery test — not just a clean-exit test.
