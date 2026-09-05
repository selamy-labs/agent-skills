# Antigravity Headless Control Design

## Context

`orchestrate-agy` already provides an interactive tmux workflow. The missing
case is bounded headless work. A reproduced AGY run returned exit code `0` and
JSON status `SUCCESS` while its response was empty because a command permission
was auto-denied. Treating either exit code or status as sufficient would accept
work that never happened.

Google's current documentation says that headless mode cannot prompt, that
permission requests are soft-denied by default, and that scoped permission
rules are preferred to `--dangerously-skip-permissions`. It also documents JSON
output, `--print-timeout`, explicit model selection, execution modes, and the
terminal sandbox:

- https://antigravity.google/docs/cli/headless/
- https://antigravity.google/docs/cli/permissions/
- https://antigravity.google/docs/cli/modes/
- https://antigravity.google/docs/cli/sandbox/

## Decision

Extend the existing skill with one Python standard-library runner,
`scripts/run_headless.py`. Do not add a second Antigravity skill and do not
generalize the change across unrelated CLI skills.

The runner accepts absolute paths for a new evidence directory, worktree,
prompt file, and AGY executable. It also requires a positive timeout, an
explicit `plan` or `accept-edits` mode, and an exact model slug. Reasoning effort
is optional.

Before dispatch, the runner captures `--version`, `--help`, and `models` output.
It verifies required flags against current help and verifies the requested
model against the current model listing. This accommodates installations whose
capabilities differ from documentation or from newer releases.

The executed command always:

1. enables the sandbox;
2. requests streaming JSON input and output;
3. sets AGY's internal print timeout;
4. writes AGY's own log inside the evidence directory;
5. places every option in argv and sends the single prompt as one NDJSON user
   event over stdin.

The prompt is read directly from the file without a shell. It never appears in
the child process argv, avoiding process-list exposure and argument-size limits.
The runner copies it and the exact stdin event into the evidence directory with
owner-only permissions. Prompts must not contain credentials or other secrets.

## Permission boundary

The default path relies on narrow `permissions.allow` rules configured through
AGY's supported settings or interactive `/permissions` command. Plan mode
prevents edits but does not grant terminal commands; sandboxing constrains
commands but does not approve them.

Blanket permission bypass is rejected unless the caller supplies both the
runner flag and a dedicated acknowledgement environment variable. This friction
does not create authorization; it only prevents an accidental bypass after an
operator has explicitly authorized one. The sandbox and worktree boundary stay
enabled even when bypass is authorized.

## Evidence and lifecycle

The runner creates the evidence directory rather than overwriting an existing
one and rejects any directory inside the worker cwd. It records the prompt,
stdin event, capability probes, stdout JSON, stderr, AGY log, and an atomically
replaced `status.json`. The status includes timestamps, duration, process and
process-group IDs, observed descendant PIDs, requested mode/model/effort, exit
code, timeout state, terminal classification, conversation ID when available,
and tri-state group and observed-descendant outcomes after harvesting.

One operation deadline covers all capability probes and the AGY turn. AGY gets
the remaining operation budget as its internal timeout, backed by one short
absolute wall-clock grace deadline for cleanup. Every process-inventory call is
capped to the time remaining, and no further inventory probe begins after its
phase deadline. Cleanup starts with at most one short grace window, even when
completion or interruption occurs long before the operation deadline. On normal
completion, timeout, or wrapper interruption, the runner terminates and
kernel-checks its owned process group without trusting complete process
inventory. It also directly signals and checks every observed descendant
identity (PID plus process start time) regardless of its current process group,
including a child that races `setsid()` against cleanup, escalates to `SIGKILL`,
and reports known survivors.
The start-time check prevents a reused PID from being signaled, and repeated
termination signals are ignored until cleanup finishes. Process-group state is
recorded as `absent`, `alive`, or `unknown`; ambiguous permission errors are a
conservative harvest failure unless a complete inventory proves the group is
zombie-only. A previously observed descendant identity is also `unknown` and
rejected when an incomplete inventory omits it before absence can be proved.
A process that deliberately double-forks and
detaches before the supervisor observes it is outside this portable
standard-library boundary; such workloads require a separately verified OS
container, service manager, or disposable machine boundary.

The runner exits nonzero for capability-probe failure, an unadvertised model or
flag, timeout, nonzero AGY exit, empty stdout, malformed JSON, non-`SUCCESS`
status, or an empty response. Spawn failures, wrapper interruption, and internal
wrapper exceptions also produce terminal evidence instead of leaving a false
`running` status. Capability probes and dispatch both reject any group or
descendant harvest state other than `absent`. An empty response accompanied by AGY's headless permission
notice is classified as `permission_blocked`; other empty responses are
`no_output`. Stderr remains evidence even when AGY exits zero.

## Skill workflow

The skill distinguishes:

- interactive tmux for trust, authentication, approvals, or iterative work;
- headless plan mode for bounded read-only analysis with pre-authorized tools;
- headless accept-edits mode for bounded editing in an isolated one-writer
  worktree with scoped permissions.

If AGY is unavailable or cannot prove the intended model, mode, permission, or
artifact state, stop or use a separately authorized worker-control skill. Never
silently translate flags or treat a fallback worker's narrative as acceptance.

## Verification

Focused tests use fake AGY executables and assert:

- exact command construction and multiline stdin prompt transport without argv
  exposure;
- capability and model discovery artifacts;
- rejection of the observed zero-exit, `SUCCESS`, empty-response permission
  failure;
- rejection of nonzero, malformed, empty, and non-success output;
- process-group and observed-descendant harvesting after timeout or SIGTERM;
- callback-failure cleanup, repeated-signal cleanup, and PID-reuse rejection;
- normal-exit and timeout process-group cleanup when process inventory is
  unavailable, including complete-versus-incomplete zombie-only `EPERM`
  handling;
- SIGKILL escalation with a nonempty but incomplete process inventory;
- tri-state group reporting that rejects ambiguous `EPERM` as a harvest failure;
- a hanging process-inventory command that cannot exceed the wall deadline;
- early interruption that cannot consume the unused operation budget;
- an observed detached descendant whose cleanup inventory disappears, forcing
  an `unknown` harvest failure rather than false success;
- an observed same-group child that calls `setsid()` between inventory and the
  group probe and is still directly harvested by identity;
- a capability probe with unknown descendant state that is rejected rather
  than accepted because its known-survivor list is empty;
- a shared overall deadline across capability probes and dispatch;
- terminal evidence for process-launch failure;
- rejection of evidence directories inside the worker cwd;
- rejection of blanket permission bypass without the acknowledgement gate.

The full repository CI command set remains the pre-push gate. A bounded live
AGY smoke check may supplement these tests, but merge readiness must not depend
on external model availability.
