---
name: batched-ssh-enumeration
description: Use when a loop or watchdog SSHes across many hosts to probe, enumerate, or act. Force non-interactive SSH with timeouts so one un-authable host cannot hang the loop, and check a heartbeat so a stuck-but-alive process is detected.
---

# Batched SSH Enumeration

A loop that SSHes to a list of hosts — a fleet probe, a deploy fan-out, a
shepherd that visits N repos/nodes — has two failure modes that quietly wedge
the whole run. Both are avoidable with a few flags and a heartbeat.

## Failure 1: one host drops to a password prompt and blocks forever

If key auth fails for **any** host (key rotated, host re-imaged, account
removed), OpenSSH falls back to an **interactive password prompt**. In an
unattended loop there is no human to type, so that one `ssh` call blocks
**indefinitely**, and every host after it never gets visited. One bad host =
total stall.

**Force non-interactive, bounded SSH on every call:**

```bash
ssh -o BatchMode=yes \        # never prompt; fail fast if key auth can't work
    -o ConnectTimeout=10 \    # cap the TCP/handshake wait
    -o StrictHostKeyChecking=accept-new \
    "$host" "$remote_cmd"
```

- `BatchMode=yes` turns any prompt (password, passphrase) into an immediate
  non-zero exit instead of a hang.
- `ConnectTimeout` bounds connection setup.
- Wrap the whole call in an **overall** `timeout` too, because a connection that
  succeeds but then hangs mid-command is not covered by `ConnectTimeout`:

```bash
timeout 60 ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" "$remote_cmd" \
  || log "host $host unreachable/timed out — skipping"
```

Treat a failed host as **skip-and-continue with a logged warning**, never as a
reason to block. Isolate each host so one failure cannot take down the sweep.

## Failure 2: the watchdog trusts liveness, not progress

A watchdog that only checks "is the process / tmux session / pod still up?" is
fooled by a **live-but-stuck** worker: the process is running, so liveness is
green, but it has been blocked on one hung SSH call for hours making zero
progress. Liveness ≠ progress.

**Watch a heartbeat, not just a pulse.** The worker writes a monotonic
heartbeat — a timestamp file, a counter, a `last_progress_at` field — every
time it actually advances (per host visited, per item done). The watchdog reads
that heartbeat and declares the worker **WEDGED** when it is *stale* beyond a
threshold, then restarts/alerts:

```bash
age=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") ))
if (( age > STALE_THRESHOLD )); then
  alert "worker WEDGED — heartbeat stale ${age}s"; restart_worker
fi
```

Real incident: a global PR shepherd ran wedged for ~37 hours. The process was
alive (liveness checks passed) but stuck on a single un-authable host's
interactive prompt; no heartbeat check existed to notice it had stopped making
progress.

## Checklist

- [ ] Every `ssh` uses `BatchMode=yes` + `ConnectTimeout`.
- [ ] Every remote call is wrapped in an overall `timeout`.
- [ ] A failed host is skipped-and-logged, never blocking.
- [ ] The worker emits a heartbeat on real progress.
- [ ] The watchdog flags **stale heartbeat** as wedged, not just dead process.
