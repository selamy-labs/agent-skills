# Gemini CLI Contained Control Design

## Context

The catalog already contains `orchestrate-agy`, `orchestrate-codex`, and
`orchestrate-claude`. `orchestrate-agy` was recently extended with a hardened
headless runner for the lowercase `agy` executable. Google Gemini CLI is a
different executable and protocol: Gemini 0.51.0 uses `gemini`, triggers
headless operation from non-TTY standard input or `--prompt`, emits its own
stream-JSON schema, uses `--approval-mode` plus policy TOML, and selects a
sandbox provider through `GEMINI_SANDBOX`.

The existing AGY lifecycle decisions remain valid and must be preserved:
prompt content stays out of argv, one absolute deadline covers probes and the worker,
the worker runs in its own process group, descendants are harvested on every
terminal path, evidence is owner-only and atomically finalized, and a zero
process exit is not sufficient evidence of successful delivery. The new
workflow must not translate AGY flags into Gemini flags or weaken the recently
hardened AGY runner.

The versioned Gemini 0.51.0 sources establish several additional constraints:

- Headless mode accepts non-TTY stdin and supports newline-delimited
  `stream-json` events with `init`, `message`, `tool_use`, `tool_result`,
  `error`, and terminal `result` events.
- `--sandbox` is disabled unless requested and can select Docker, Podman,
  gVisor, Seatbelt, or LXC. Container sandboxing mounts the current working
  directory, the Gemini state directory, and the operating-system temporary
  directory.
- Policy `ask_user` decisions become deny in headless mode. Workspace policy
  files are non-functional in 0.51.0; command-line supplemental admin policies
  are the available high-precedence per-run control when no standard admin
  policy directory supersedes them.
- `GEMINI_CLI_HOME`, `GEMINI_CLI_SYSTEM_SETTINGS_PATH`, and `TMPDIR` let a
  supervisor isolate mutable Gemini state, system overrides, and temporary
  files from other lanes.
- Gemini's native `--worktree` support is experimental and shares Git metadata.
  A worker that can write shared Git metadata can still affect sibling lanes,
  so native worktree creation is not a sufficient containment boundary.
- Since June 18, 2026, Gemini CLI no longer serves free, Google AI Pro, or
  Google AI Ultra individual accounts. Enterprise licenses and paid API-key
  access remain supported. This workflow cannot treat personal OAuth as live.
- In 0.51.0 the entire CLI enters the container. A network-disabled container
  cannot contact the model, while unrestricted egress would let tools bypass
  containment. Live runs require an allowlisting proxy on Gemini's internal
  sandbox network.

Primary sources:

- https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/cli/headless.md
- https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/cli/cli-reference.md
- https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/cli/sandbox.md
- https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/reference/policy-engine.md
- https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/reference/configuration.md
- https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/cli/git-worktrees.md
- https://github.com/google-gemini/gemini-cli/discussions/28017

## Decision

Add a sibling `orchestrate-gemini` skill. Do not overload
`orchestrate-agy`: the two clients have different capability, authentication,
policy, sandbox, output, and resume contracts. Do not extract a repository-wide
runtime from the fresh AGY runner in this change because single-skill installs
must remain self-contained and that refactor would expand the regression
surface.

The skill includes the established interactive tmux launch, submission, and
causal dispatch-verification helpers. It also includes one Gemini-specific
Python 3.11 standard-library headless runner. The runner is intentionally a
single-turn execution primitive; a supervisor may invoke it again only through
the same durable lane state and within the goal's attempt budget.

## Durable goal contract

Every headless lane requires an immutable JSON goal file with this shape:

```json
{
  "schema_version": 1,
  "goal_id": "unique-stable-id",
  "objective": "observable desired outcome",
  "base_sha": "40-character-lowercase-git-object-id",
  "allowed_paths": ["src/", "tests/"],
  "verification_commands": [
    {"argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 120}
  ],
  "stop_conditions": ["scope would expand", "credentials are unavailable"],
  "max_attempts": 3,
  "auth_type": "gemini-api-key",
  "credential_identity": "reviewed-non-secret-quota-owner-id",
  "allow_paid_generation": false,
  "sandbox_image": "registry.example/sandbox@sha256:64-lowercase-hex-digits"
}
```

Paths are repository-relative, normalized prefixes without `..`, absolute
components, or shell syntax. Verification commands are non-empty argv arrays
executed without a shell and have positive individual deadlines. The objective,
stop conditions, exact base, allowed paths, verification commands, attempt
budget, reviewed non-secret credential identity, billing policy, and
digest-pinned sandbox image are requirements, not
optional annotations. Allowed paths must already exist so the runtime can mount
only those paths writable over a read-only checkout root. Their trees cannot
contain symlinks, multiply-linked regular files, or special files.

On the first attempt, the runner copies the goal to the lane state directory
with mode `0600` and records its SHA-256 digest. Later attempts must present the
same digest. The runner rejects a changed goal, an exhausted attempt budget, or
a paid-capable auth type unless both the manifest allows paid generation and a
separate acknowledgement environment variable records explicit authorization.
The acknowledgement records intent but does not create spending authority.

## Lane and containment contract

The runner accepts absolute paths for a new run directory, an existing lane
state directory, the dedicated repository checkout, prompt file, policy file,
and Gemini executable. The run and state directories must be outside the
checkout and owner-only. The state directory owns a non-blocking OS file lock
for the entire attempt and an atomically updated lease record with lane, goal,
PID, start time, and heartbeat. A locked lane is busy; stale metadata without a
held kernel lock is audit history, not ownership.

Before launch, the runner verifies:

1. the checkout is a Git repository whose resolved common Git directory is
    inside the checkout, excluding linked worktrees and shared repositories;
2. repository attribute files are absent, so host Git cannot invoke configured
   clean, process, or diff drivers;
3. `HEAD` equals the goal's exact base SHA and the worktree is clean;
4. the live executable's version/help advertise stdin headless operation,
   `--output-format`, `--model`, `--approval-mode`, `--sandbox`, and
   `--admin-policy`;
5. an explicit Docker, Podman, or gVisor provider exists;
6. the supplemental policy is a regular owner-controlled file and no standard
   admin policy directory would cause Gemini to ignore it.

The worker receives an isolated `HOME`, Gemini home, system-settings path, and
temp directory below the lane state directory. Validation uses no network;
live execution uses only the internal proxy network. The
runner trusts only the already verified checkout and writes system overrides
that disable auto-update, YOLO, permanent approvals, extension loading, MCP
servers, skills, and hooks; enable environment-variable redaction and folder
trust; ignore project `.env` files; and require the selected sandbox provider. The private temp directory
prevents Gemini's container sandbox from mounting a shared host temp tree.

Gemini 0.51.0 reads non-TTY stdin in the outer process and injects that content
as an inner `--prompt` argument before launching its container sandbox. Sending
the directive directly over stdin would therefore expose it in a host-visible
Docker argv. The runner instead stages an owner-only prompt file under the
private checkout's Git directory, supplies only an `@<absolute-path>` reference
through `--prompt`, and removes the staged file after worker harvest. Gemini
expands the reference inside the sandbox; the full directive never enters argv.

The runner rejects inherited sandbox mounts, sandbox flags, raw-output flags,
and `--skip-trust`. It supplies a fixed container-hardening flag set, disables
all extensions with both the system `admin.extensions.enabled` override and
Gemini's documented `-e none` selector, passes only the staged prompt reference,
requests `stream-json`, uses a non-YOLO approval mode, and passes the reviewed
policy as a supplemental admin policy. The policy must default-deny all tools
and narrowly allow only the goal's required operations. It cannot allow
`run_shell_command` or the legacy `ShellTool` alias because a child process
could read the Gemini parent's billing credential from the process namespace.

An owner-only provider guard removes the upstream host-gateway mapping, binds
the proxy readiness port to loopback, labels every container, rewrites the
checkout root and Git metadata read-only, and remounts only goal paths writable.
It attests that Gemini's fixed worker network is internal, that the fixed proxy
network is external, and rejects unexpected network creates, connections, or
worker network selections. A pre-existing network with the right name must
still match the required internal/external isolation mode.
OS isolation protects the checkout boundary even if policy or model judgment is
wrong; policy remains the least-privilege tool boundary inside that checkout.

Live authentication is restricted to a paid Gemini API key because consumer
OAuth service is discontinued and other enterprise routes are not proven by this v0.51
runner. The key arrives in one owner-only env file. The host-side Gemini process
gets only a placeholder; the provider guard replaces it with `--env-file` for
the sandboxed Gemini worker, keeping the real key out of process argv and
evidence. Process-execution tools are forbidden so the worker cannot expose that
environment to a model-directed child. Its lane-local runtime copy is removed
after container reconciliation on every terminal path. The bundled CONNECT proxy permits
only `generativelanguage.googleapis.com:443`. The supervisor must still prove
quota ownership, record its reviewed non-secret identity in the immutable goal,
and require explicit paid-generation authorization. `--preflight-only`
performs every non-generating check without contacting a model.

## Execution, evidence, and acceptance

The run directory is created once with mode `0700`. Prompt, goal, policy,
command metadata, version/help probes, stdout, stderr, Git before/after state,
verification outputs, and status are owner-only. Command metadata records argv
and passed environment-variable names, never secret values. Status is replaced
atomically and includes goal digest, attempt, lease identity, PID/process group,
heartbeat and last-output time, requested/reported model, result model statistics, session ID,
classification, Git identities, changed paths, verification results, and
survivor state.

One absolute attempt deadline covers probes, Gemini, Git inspection, and
verification. A fixed short cleanup grace remains available after that deadline
so timeout cannot prevent process harvesting. Probes, the worker, and verification
commands all run in fresh process groups. The runner preserves the AGY runner's
conservative process-group and observed-descendant harvesting semantics,
including start-time identity checks, TERM/KILL escalation, repeated-signal
handling, incomplete-inventory uncertainty, and no success with a known or
unknown survivor.

Success requires all of the following:

- exactly one `init` event and one terminal `result` event;
- terminal status `success`, a non-empty assistant response, and no fatal
  stream error;
- no process-group or observed-descendant survivor;
- post-run `HEAD` descended from the exact base;
- every Git and filesystem change, including ignored paths, within an allowed
  path prefix and no Git-control metadata change;
- every immutable verification command passing inside a networkless,
  digest-pinned verification container with the checkout read-only.

Exit zero means those conditions all hold. Capability mismatch, locked lease,
goal mismatch, attempt exhaustion, billing-policy violation, sandbox/policy
failure, invalid/empty output, Gemini failure, timeout, interruption, survivor,
scope violation, Git-boundary violation, or verification failure are distinct
nonzero classifications. A worker narrative is never completion evidence.

## Restart resilience

Each attempt gets a new run directory but reuses the lane state directory and
immutable goal. Before dispatch, a replacement reconciles a stale PID/process
group by start-time identity and removes runtime containers by lane/UID labels.
Gemini 0.51's fixed proxy name requires a user-global execution lock. A later
invocation may resume only a prior `succeeded` delivery whose complete execution
request digest and checkout identity match and whose worker is absent. Resume
is explicit; validation, failure, and `latest` are forbidden.

## Verification strategy

Tests use temporary real Git repositories and fake Gemini executables. They
first prove red for missing behavior, then cover goal validation and immutability,
single-writer locking, private-clone enforcement, capability/sandbox/policy
preflight, prompt secrecy, fixed command construction, output classifications,
timeouts and process cleanup, path-scope checks across all Git states,
verification-command success/failure, billing gates, atomic evidence, and exact
session resume. Shared tmux tests add Gemini to the established cross-client
dispatch ratchet.

A jump-box smoke test uses `--preflight-only` against the real Gemini binary and
container provider. A separate validation-only mode may supply Gemini's documented
`--fake-responses` fixture to the real binary, exercise sandbox launch, staged
prompt expansion, stream output, and cleanup without contacting a model, and finish
with `validation_succeeded` rather than the operational `succeeded`
classification. The fake fixture, its digest, and validation marker are retained in
evidence so they cannot be mistaken for delegated delivery. A generated live
turn is supplemental and must not run unless an explicitly funded quota route
is proven. Merge readiness depends on
deterministic tests and non-generating real-installation evidence, not external
model availability.
