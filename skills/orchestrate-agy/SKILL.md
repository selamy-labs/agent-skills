---
name: orchestrate-agy
description: Reliably launch, supervise, resume, and hand off lowercase `agy` as an interactive tmux worker. Use for bounded AGY delegation, remote or alternate-user operation, auth recovery, quota routing, and proving a prompt was submitted rather than merely pasted.
---

# Orchestrate AGY

## Establish the lane

1. Resolve the executable in the target user's login environment with `command -v agy`; AGY's command is lowercase. Read `agy --help` and `agy --version` before selecting flags. Do not copy stale flags from another installation.
2. Verify the target OS user, AGY identity, model/quota route, repository, clean exact base commit, and worktree. Give one writer exclusive ownership of each concern.
3. Name the tmux session for the repository, task, and date. Record the host, OS user, session, worktree, branch, base SHA, worker role, and permitted paths before launch.
4. Put a long directive in a file. Do not interpolate it into nested SSH or shell quoting, and do not put secrets in it. Launch with `scripts/launch_tmux.sh`; it reads the file and passes the complete text atomically to `agy --prompt-interactive`.

```bash
scripts/launch_tmux.sh \
  lane-repo-agy-task-YYYYMMDD /absolute/worktree /tmp/task.prompt \
  "$(command -v agy)" --mode plan
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

## Supervise and recover

Classify every observation explicitly:

- **running:** output or tool activity advances;
- **idle/completed:** the prompt returned after a result;
- **input-blocked:** a question or choice awaits an answer;
- **trust/auth-blocked:** setup or login prevents work;
- **dead:** the pane or process exited.

Respond to safe in-scope choices, repair trust/auth under the intended identity, or restart from the recorded lane. Preserve the conversation ID and use the current `--conversation` or `--continue` semantics from `agy --help` when resuming. Never infer liveness from tmux session existence alone.

## Accept the result

Require a handoff containing exact commit/tree/parent, changed paths, tests and gates, clean status, remaining risks, and session/conversation identifiers. Independently inspect the artifact or PR at the exact head. A narrative, green test from another SHA, or an idle pane is not delivery.
