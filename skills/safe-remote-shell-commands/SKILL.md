---
name: safe-remote-shell-commands
description: Use when commands cross shell boundaries through SSH, nested shells, tmux, heredocs, substitutions, variables, or quoted data. Prevent expansion by the wrong interpreter, simplify transport, and verify resulting state.
---

# Safe Remote Shell Commands

Remote commands often traverse several interpreters: the local shell, SSH,
the remote login shell, an optional nested shell, and perhaps tmux. Quoting that
is correct for one boundary can be consumed by an earlier one.

## Workflow

### 1. Count the boundaries

Before running anything, name every interpreter in order. For example:

```text
local shell -> ssh argument -> remote shell -> bash -lc -> tmux process
```

For every `$name`, `${name}`, `$(command)`, backtick, glob, backslash, and quote,
decide which interpreter must see it. If that is difficult to state precisely,
simplify the command instead of adding another escape layer.

### 2. Resolve values at an explicit boundary

Do not place remote command substitution inside a local double-quoted string:

```bash
# Unsafe: the local shell runs git before ssh starts.
ssh build-host "cd /srv/app && test \"$(git rev-parse HEAD)\" = abc123"
```

Prefer one of these patterns:

```bash
# A fixed, already-resolved value needs no substitution.
ssh build-host 'cd /srv/app && test "$(git rev-parse HEAD)" = abc123'

# Resolve remotely, return the value, and compare locally as a separate step.
remote_head=$(ssh build-host 'cd /srv/app && git rev-parse HEAD')
test "$remote_head" = "abc123"
```

Single quotes protect text from the local shell. They do not make arbitrary
nested quoting easy. Also, SSH joins remote command arguments into one command
string; do not assume local argument boundaries survive transport. When dynamic
data must cross the boundary, send it to a fixed remote program over standard
input or another data protocol rather than interpolating it into shell source:

```bash
expected_ref="abc123"
printf '%s\n' "$expected_ref" | ssh build-host '/srv/control/verify-ref'
```

Here `/srv/control/verify-ref` is a previously copied, reviewed script that
reads one constrained ref from standard input. The remote shell parses only the
fixed command path; the ref remains data.

### 3. Reduce quoting depth

Use this preference order:

1. Invoke a program directly with separate arguments.
2. Run a short, single-quoted remote command.
3. Send a static script with a quoted heredoc, or pipe data to a fixed remote
   program over standard input.
4. Copy a reviewed script and run it by an explicit path.
5. Use nested `sh -c` or `bash -lc` only when login-shell behavior is required.

For tmux, provide the program and arguments directly when possible:

```bash
ssh build-host tmux new-session -d -s audit -c /srv/app \
  worker --mode review --input /srv/control/audit.prompt
```

Put long prompts, JSON, or multiline logic in a file. Copying a file and then
executing or reading it is usually clearer than transporting it through nested
quotes.

### 4. Keep data separate from shell source

- Treat hostnames, paths, refs, session names, and user text as data.
- Transmit dynamic data through standard input, files, or a structured protocol
  to a fixed remote program; quote every expansion where that program uses it.
- Do not embed credentials or other secrets in command strings, process titles,
  logs, or shell history.
- Disable tracing around sensitive operations. Never rely on clever quoting to
  make logged command text private.
- Avoid `eval`. If source must be generated dynamically, write and inspect a
  script before executing it.

### 5. Split mutation from verification

Do setup, launch, and verification as separate calls. A shorter command makes
the failure boundary visible and avoids ambiguous partial success.

```bash
ssh build-host 'test -d /srv/app && git -C /srv/app status --short'
ssh build-host tmux new-session -d -s audit -c /srv/app worker --mode review
ssh build-host 'tmux has-session -t audit && pgrep -af "worker --mode review"'
```

After any quoting or transport failure, assume some earlier operations may have
succeeded. Inspect the exact remote directory, process, session, branch, or
artifact before retrying. Do not infer remote state from a local exit message.

## Preflight checklist

- [ ] I named each shell or interpreter that will parse the command.
- [ ] Every substitution and variable expands at the intended boundary.
- [ ] Dynamic values travel as data, not concatenated shell source.
- [ ] Long logic lives in a script or file rather than a nested quoted string.
- [ ] Setup, launch, and verification have distinct failure boundaries.
- [ ] The command does not expose credentials in argv, logs, or history.
- [ ] I will verify the intended remote process and resulting state.
- [ ] If a prior attempt failed, I checked for partial side effects before retrying.

## Stop conditions

Stop and restructure the operation when:

- correctness depends on mentally tracking three or more escaping layers;
- the command contains both local double quotes and intended remote `$()`;
- user-controlled text is being inserted into executable shell source;
- a failure could leave a destructive or expensive partial action;
- verification only checks that SSH returned zero, not that the target state exists.
