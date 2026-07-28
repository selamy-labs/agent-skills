---
name: orchestrate-claude
description: Reliably launch, supervise, resume, and hand off Claude Code as an interactive tmux worker. Use for bounded Claude delegation, remote or alternate-user operation, auth recovery, dedicated quota routing, and proving a directive was submitted rather than merely pasted.
---

# Orchestrate Claude Code

## Establish the lane

1. Resolve the executable in the target user's login environment with `command -v claude`. Read `claude --help` and `claude --version` before selecting flags; aliases, models, permission modes, and resume options can change.
2. Verify the target OS user, Claude account and quota route, repository, clean exact base commit, and worktree. Give one writer exclusive ownership of each concern. Do not authorize a personal OAuth account when a dedicated delivery account is intended.
3. Name the tmux session for the repository, task, and date. Record the host, OS user, session, worktree, branch, base SHA, worker role, and permitted paths before launch.
4. Put a long directive in a file. Do not interpolate it into nested SSH or shell quoting, and do not put secrets in it. Launch with `scripts/launch_tmux.sh`; it reads the file and supplies the complete text as Claude's single initial positional prompt.

```bash
scripts/launch_tmux.sh \
  lane-repo-claude-task-YYYYMMDD /absolute/worktree /tmp/task.prompt \
  "$(command -v claude)" --model opus --effort high --name lane-repo-task
```

Choose model, effort, agents/team configuration, and permission mode from current help and the task. Use `--dangerously-skip-permissions` or `--permission-mode bypassPermissions` only with explicit authorization and an externally isolated, correctly scoped worktree. Permission bypass does not broaden the authorized task.

## Prove dispatch

Immediately inspect both process and pane:

```bash
tmux list-panes -t lane-repo-claude-task-YYYYMMDD \
  -F '#{pane_pid} #{pane_current_path} #{pane_current_command} #{pane_dead}'
PANE_ID=$(tmux list-panes -t '=lane-repo-claude-task-YYYYMMDD' -F '#{pane_id}' | head -n 1)
tmux capture-pane -p -J -t "$PANE_ID" -S -120
```

Confirm the cwd and child argv match the intended executable, flags, and prompt. Handle workspace-trust and OAuth/login screens in the TUI. Before completing authorization, verify the displayed account is the intended identity and quota owner.

The launcher prints a unique dispatch ID and empty pre-capture path. After resolving trust/auth screens, verify that exact dispatch:

```bash
scripts/verify_dispatch.sh "$SESSION" "$DISPATCH_ID" "$PRE_CAPTURE"
```

For a follow-up already stored in an absolute-path file, use `scripts/submit_followup.sh "$SESSION" "$FOLLOWUP"`. It captures the pane before submission, wraps the directive in unique start/end markers plus an exact acknowledgement request, presses Enter, and retries Enter only when the first attempt still appears unsubmitted.

Count work as dispatched only when the verifier finds the unique start and end markers in order, Claude's exact acknowledgement after the end marker, fresh activity after that acknowledgement, and a new clean composer after the activity. Generic tool-call text anywhere else is stale or unrelated evidence and must fail. A marker still held in the active composer is not submitted work.

## Supervise and recover

Classify every observation explicitly:

- **running:** output, tasks, subagents, or tool activity advances;
- **idle/completed:** the prompt returned after a result;
- **input-blocked:** a question, choice, or approval awaits input;
- **trust/auth-blocked:** setup, OAuth, or login prevents work;
- **quota-blocked:** the intended account cannot continue;
- **dead:** the pane or process exited.

Answer safe in-scope choices, repair trust/auth under the intended identity, or restart from the recorded lane. Preserve the session ID/name and use current `--resume`, `--continue`, or `--session-id` semantics from help. Use subagents or agent teams only for collision-free bounded work with one integrator. Never infer liveness from tmux session existence alone.

## Accept the result

Require a handoff containing exact commit/tree/parent, changed paths, tests and gates, clean status, remaining risks, and tmux/Claude session identifiers. Independently inspect the artifact or PR at the exact head. A narrative, green test from another SHA, or an idle pane is not delivery.
