---
name: orchestrate-agy
description: Reliably launch, bound, supervise, and verify lowercase `agy` in interactive tmux or headless mode. Use for AGY delegation, remote or alternate-user operation, auth recovery, quota routing, durable run evidence, and detecting silent permission or no-output failures.
---

# Orchestrate AGY

## Establish the lane

1. Resolve the executable in the target user's login environment with `command -v agy`; AGY's command is lowercase. Capture `agy --help`, `agy --version`, and `agy models` before selecting flags or a model. Treat the installed CLI as the executable contract: releases differ, and some versions do not support machine-readable model listing even when newer documentation does.
2. Verify the target OS user, AGY identity and quota route, repository, clean exact base commit, and isolated worktree. Give one writer exclusive ownership of each concern. Record the host, OS user, worktree, branch, base SHA, worker role, permitted paths, model, effort, mode, sandbox state, and timeout.
3. Put the complete directive in a file. Do not interpolate a long prompt into nested SSH or shell quoting. Do not put credentials, tokens, private keys, or other secrets in prompts or evidence.

## Choose the control surface

| Use | Control surface | Required boundary |
| --- | --- | --- |
| Login, trust, approvals, iterative discussion, or recovery | Interactive tmux | Inspect the live pane and prove submission |
| Bounded read-only investigation | Headless `plan` | Pre-authorize only the required tools; verify a non-empty result |
| Bounded implementation | Headless `accept-edits` | Isolated one-writer worktree, sandbox, scoped permissions, independent diff verification |

Plan mode controls agent editing behavior. Sandbox mode controls OS containment. Neither grants a terminal command: headless tools that require an unconfigured approval are soft-denied because no prompt can be shown. Configure narrow `permissions.allow` rules through the supported settings or interactive `/permissions` UI before starting a headless run.

## Run interactively

Name the tmux session for the repository, task, and date. Launch with `scripts/launch_tmux.sh`; it reads the prompt file and passes the complete text atomically to `agy --prompt-interactive`.

```bash
scripts/launch_tmux.sh \
  lane-repo-agy-task-YYYYMMDD /absolute/worktree /tmp/task.prompt \
  "$(command -v agy)" --mode plan --sandbox --model MODEL
```

Choose mode and model from the current help and the task. Add `--effort` only when this installation's `agy --help` advertises it; otherwise omit it. Use `--dangerously-skip-permissions` only with explicit authorization and an externally isolated, correctly scoped worktree. Permission bypass does not broaden the authorized task.

## Prove dispatch

Immediately inspect both process and pane:

```bash
tmux list-panes -t lane-repo-agy-task-YYYYMMDD \
  -F '#{pane_pid} #{pane_current_path} #{pane_current_command} #{pane_dead}'
PANE_ID=$(tmux list-panes -t '=lane-repo-agy-task-YYYYMMDD' -F '#{pane_id}' | head -n 1)
tmux capture-pane -p -J -t "$PANE_ID" -S -120
```

Confirm the cwd and child argv match the intended executable, flags, and prompt. Handle workspace-trust and login screens in the TUI; verify the authenticated identity before authorizing when accounts have different quota or authority.

The launcher prints a unique dispatch ID and empty pre-capture path. After resolving trust/auth screens, verify that exact dispatch:

```bash
scripts/verify_dispatch.sh "$SESSION" "$DISPATCH_ID" "$PRE_CAPTURE"
```

For a follow-up already stored in an absolute-path file, use `scripts/submit_followup.sh "$SESSION" "$FOLLOWUP"`. It captures the pane before submission, wraps the directive in unique start/end markers plus an exact acknowledgement request, presses Enter, and retries Enter only when the first attempt still appears unsubmitted.

Count work as dispatched only when the verifier finds the unique start and end markers in order, the client's exact acknowledgement after the end marker, fresh activity after that acknowledgement, and a new clean composer after the activity. Generic `Working` text anywhere else is stale or unrelated evidence and must fail. A marker still held in the active composer is not submitted work.

## Run headlessly

Use `scripts/run_headless.py` for one bounded turn. Give it a new absolute evidence directory outside the worker cwd and outside any cleanup boundary that might disappear before review. The runner probes current capabilities, validates the exact model, enables the sandbox, requests streaming JSON, configures AGY's internal timeout and log, then sends one file-loaded user event over stdin. The prompt never appears in process argv.

```bash
scripts/run_headless.py \
  --run-dir /absolute/evidence/run-001 \
  --cwd /absolute/worktree \
  --prompt-file /absolute/task.prompt \
  --agy "$(command -v agy)" \
  --timeout-seconds 120 \
  --mode plan \
  --model MODEL \
  --effort high
```

The runner writes owner-only prompt, stdin event, command, version, help, model, stdout, stderr, AGY log, and atomic `status.json` artifacts. Inspect `prompt.txt`, `input.ndjson`, and `command.json` together when prompt transport matters. While running, status records the PID and process group. One operation deadline covers the probes and AGY turn; AGY receives the remaining budget, backed by a short wall grace for cleanup. On normal exit, timeout, or SIGINT/SIGTERM, the runner always terminates and kernel-checks the owned process group without trusting process inventory, and separately terminates every detached descendant identity (PID plus start time) it observed. It then records any known survivor. Repeated termination signals are ignored until this cleanup finishes.

Exit `0` means AGY returned exactly one streaming JSON `result` event with a `SUCCESS` envelope and non-empty response, with no surviving process group or observed descendant. Treat every other classification as undelivered:

- `permission_blocked`: AGY exited zero with `SUCCESS` but returned an empty response alongside a headless permission notice;
- `no_output` or `invalid_output`: the response cannot prove a completed turn;
- `agy_status_*` or `cli_error`: AGY reported or exited with failure;
- `timed_out`, `interrupted`, or `harvest_failed`: the bounded process did not terminate cleanly; ambiguous `EPERM` group probes are conservatively `harvest_failed` unless the process inventory proves the group is zombie-only;
- `launch_error` or `internal_error`: the worker or wrapper failed before producing a valid terminal result;
- `capability_probe_failed` or `capability_mismatch`: the executable, flags, or selected model were not proven.

Do not retry an empty response with blanket approval. Add the narrow `action(target)` rule the task needs, or switch to the interactive path. Only after explicit authorization for all tool calls in an externally isolated worktree may you add `--allow-all-permissions` and set `ORCHESTRATE_AGY_PERMISSION_BYPASS_ACK=authorized`; the runner then supplies `--dangerously-skip-permissions` while retaining the sandbox, timeout, and evidence boundaries. The acknowledgement records intent but does not create authority.

## Supervise and recover

Classify every observation explicitly:

- **running:** output or tool activity advances;
- **idle/completed:** the prompt returned after a result;
- **input-blocked:** a question or choice awaits an answer;
- **trust/auth-blocked:** setup or login prevents work;
- **dead:** the pane or process exited.

Respond to safe in-scope choices, repair trust/auth under the intended identity, or restart from the recorded lane. Preserve the conversation ID and use the current `--conversation` or `--continue` semantics from `agy --help` when resuming. Never infer liveness from tmux session existence alone. After delivery or failure, capture the final pane/process state, terminate the owned tmux session or process group, and verify that no known worker survives. Portable PID tracking cannot prove containment of a process that deliberately double-forks and detaches before observation. If the authorized task may daemonize or launch external services, add a separately verified OS container, service manager, or disposable-machine boundary and clean that boundary explicitly.

If AGY is unavailable, its capability probe fails, or the intended model or authority cannot be proven, report the exact blocker. Use another worker-control skill only when that fallback is separately authorized; reselect its model, reasoning, sandbox, and permission flags from its own current help rather than translating AGY flags. A fallback worker's report still requires the same independent artifact checks.

## Accept the result

Require a handoff containing exact commit/tree/parent, changed paths, tests and gates, clean status, remaining risks, evidence directory, and session/conversation identifiers. Independently inspect the artifact or PR at the exact head, rerun the relevant checks, and confirm the worker stayed within its worktree and allowed paths. A narrative, green test from another SHA, JSON `SUCCESS` with an empty response, or an idle pane is not delivery.

## Primary sources

- [Headless mode](https://antigravity.google/docs/cli/headless/)
- [Execution modes](https://antigravity.google/docs/cli/modes/)
- [Permissions](https://antigravity.google/docs/cli/permissions/)
- [Sandbox](https://antigravity.google/docs/cli/sandbox/)
