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
generalize the change across unrelated CLI orchestrators.

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
2. requests JSON output;
3. sets AGY's internal print timeout;
4. writes AGY's own log inside the evidence directory;
5. places every option before a final `--print` and its single prompt value.

The prompt is read directly from the file and passed as one argument without a
shell. The runner copies it into the evidence directory with owner-only
permissions. Prompts must not contain credentials or other secrets.

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
one. It records the prompt, capability probes, stdout JSON, stderr, AGY log, and
an atomically replaced `status.json`. The status includes timestamps, duration,
process and process-group IDs, requested mode/model/effort, exit code, timeout
state, terminal classification, conversation ID when available, and whether
the process group remained alive after harvesting.

AGY's internal timeout is backed by a slightly longer wall-clock timeout. On a
wall timeout, the runner terminates the entire process group, waits a short
grace period, kills survivors, and verifies that the group is gone.

The runner exits nonzero for capability-probe failure, an unadvertised model or
flag, timeout, nonzero AGY exit, empty stdout, malformed JSON, non-`SUCCESS`
status, or an empty response. An empty response accompanied by AGY's headless
permission notice is classified as `permission_blocked`; other empty responses
are `no_output`. Stderr remains evidence even when AGY exits zero.

## Skill workflow

The skill distinguishes:

- interactive tmux for trust, authentication, approvals, or iterative work;
- headless plan mode for bounded read-only analysis with pre-authorized tools;
- headless accept-edits mode for bounded editing in an isolated one-writer
  worktree with scoped permissions.

If AGY is unavailable or cannot prove the intended model, mode, permission, or
artifact state, stop or use a separately authorized orchestrator skill. Never
silently translate flags or treat a fallback worker's narrative as acceptance.

## Verification

Focused tests use fake AGY executables and assert:

- exact command construction and multiline prompt transport;
- capability and model discovery artifacts;
- rejection of the observed zero-exit, `SUCCESS`, empty-response permission
  failure;
- rejection of nonzero, malformed, empty, and non-success output;
- process-group termination and harvesting after timeout;
- rejection of blanket permission bypass without the acknowledgement gate.

The full repository CI command set remains the pre-push gate. A bounded live
AGY smoke check may supplement these tests, but merge readiness must not depend
on external model availability.
