---
name: orchestrate-gemini
description: Safely control Google Gemini CLI on local or remote hosts with private checkouts, immutable goals, least-privilege sandbox policy, durable leases and evidence, restart-safe resume, and system-of-record verification. Use for bounded delegation, jump-box operation, auth or quota routing, tmux recovery, and non-generating validation.
---

# Orchestrate Gemini CLI

This skill controls Google's `gemini` executable. It does not control, wrap, or
alias the distinct Antigravity CLI (`agy`); use `orchestrate-agy` for that tool.

## Establish the lane

1. Run probes as the intended OS user. In that user's login environment, resolve
   `command -v gemini`, then capture `gemini --version` and `gemini --help`.
   Treat the installed executable as the flag contract.
2. Verify the Gemini auth type and quota owner without printing credential
   values. An adjacent Antigravity account directory is not evidence that Gemini
   supports or selected that identity. Stop if the intended identity or funding
   route cannot be proven.
3. Use a dedicated clone whose Git common directory is inside the checkout. Do
   not dispatch in an active product checkout or a linked Git worktree with
   shared metadata. Pin the exact clean base SHA and give one lane one writer.
4. Put the objective, already-existing allowed paths, immutable verification argv, stop
   conditions, attempt budget, auth type, and paid-generation policy in a goal
   JSON file. Start from `references/goal.example.json`; replace every example
   value before dispatch. Pin the container image by `image@sha256` digest in
   the same goal.
5. Create the lane state directory outside the checkout with mode `0700`. Store
   the prompt and a task-specific default-deny policy as owner-only files.

Use batched, read-only SSH probes before mutations. Never inspect credential
contents, transmit secrets through prompts, or infer authorization from another
process, account, checkout, or supervisor.

## Choose the control surface

| Need | Surface | Acceptance boundary |
| --- | --- | --- |
| Login, trust, approvals, or discussion | Interactive tmux | Prove exact marked submission and fresh activity; never count it as contained delivery |
| Capability/auth/sandbox proof only | `--preflight-only` | No model process; terminal `preflight_succeeded` |
| No-generation installation/sandbox smoke test | `--validation-fake-responses` | Terminal `validation_succeeded`, never delivery |
| Bounded delegated work | Headless runner | Verified stream, process harvest, Git scope, and gates |

Prefer `plan` for read-only investigation. Use `default` or `auto_edit` only
when the immutable goal authorizes edits and the private checkout plus policy
contain them. The runner rejects model aliases, YOLO, extension loading,
inherited sandbox mounts, shared Git metadata, and unreviewed shell strings.

## Run a preflight

```bash
skills/orchestrate-gemini/scripts/run_headless.py \
  --run-dir /absolute/lane-state/runs/attempt-001 \
  --state-dir /absolute/lane-state \
  --cwd /absolute/private-clone \
  --goal-file /absolute/goal.json \
  --prompt-file /absolute/task.prompt \
  --policy-file /absolute/policy.toml \
  --gemini /absolute/path/to/gemini \
  --sandbox-provider docker \
  --timeout-seconds 120 \
  --model gemini-2.5-pro \
  --approval-mode plan \
  --preflight-only
```

Preflight acquires a non-blocking `flock` lease, persists the canonical goal and
digest, proves the exact clean Git base, validates the default-deny policy,
creates an isolated Gemini home and temp directory, proves the selected
container service and digest-pinned image are available, trusts only the
verified checkout, isolates `HOME`, disables extensions, MCP, skills, hooks,
and credit overage, installs fixed container hardening, and requires exactly
Gemini CLI 0.51.0. It never starts a generated turn.

The policy passed with `--admin-policy` is supplemental in Gemini CLI 0.51.0.
The runner stops if a standard system admin-policy directory would supersede
it. Review `references/policy.example.toml` and add only the narrow tool names
the goal requires at a priority above the catch-all deny.

## Run headlessly

Remove `--preflight-only` only after its evidence passes. The runner stages the
owner-only prompt beneath the private checkout's Git metadata and puts only an
`@<path>` reference in argv. This matters because Gemini 0.51.0 transforms
non-TTY stdin into an inner Docker `--prompt` argument. The staged file is
removed after worker harvest; an owner-only evidence copy remains.

The worker and capability probes run in fresh process groups under one absolute
operation deadline plus a fixed short cleanup grace. Verification commands run
inside the same digest-pinned image with no network, a read-only checkout, an
isolated home/temp, and the same process/container harvesting. The supervisor
tracks descendants by PID plus process start time,
including a child that calls `setsid()`, and escalates TERM to KILL. Known or
uncertain survivors prevent success. Output must contain exactly one `init`, one successful
`result`, a non-empty assistant message, no fatal error, the exact requested
model, and a session ID.

The provider guard labels every Gemini and verifier container, removes the
upstream host-gateway mapping, binds proxy readiness to loopback, makes the
checkout root and Git metadata read-only, and remounts only declared paths
writable. It also requires Gemini's fixed worker network to be internal,
requires the fixed proxy network to be external, and rejects every unexpected
network operation or attachment. After harvest, the runner checks sanitized Git state and a full
filesystem snapshot, including ignored paths. Any Git metadata change fails.
Exit zero means verified delivery, not merely that Gemini stopped talking.

Google stopped serving Gemini CLI requests for free, Google AI Pro, and Google
AI Ultra individual accounts on June 18, 2026. This v0.51 workflow therefore
rejects consumer OAuth and supports live execution only with a paid Gemini API
key. Live generation requires `allow_paid_generation: true` plus
`ORCHESTRATE_GEMINI_PAID_GENERATION_ACK=authorized`. Supply the key only in an
owner-only file containing `GEMINI_API_KEY=<value>` via
`--credential-env-file`; never export it. The outer CLI receives a non-secret
placeholder, and the provider guard replaces that placeholder with a runtime
`--env-file` path. The ephemeral copy is removed on every terminal path after
container reconciliation. Any real key in runtime argv is rejected.

Live model transport crosses an internal container network through the bundled
CONNECT proxy, which permits only `generativelanguage.googleapis.com:443`.
The proxy checkout mount is read-only and its readiness port binds only to
loopback. Validation mode instead forces `--network none`. The wrapper
serializes v0.51 executions because upstream uses a global proxy container name.

## Validate without generation

To exercise the real executable and container launch without contacting a
model, use a reviewed Gemini `--fake-responses` fixture:

```bash
skills/orchestrate-gemini/scripts/run_headless.py \
  ...same reviewed arguments... \
  --validation-fake-responses /absolute/reviewed.responses
```

The runner retains the fixture and its digest, marks `validation_mode: true`,
forces container network `none`, and can only finish as
`validation_succeeded`. Never present this classification as delegated
work. Do not run a live smoke turn merely to strengthen validation when the
quota route is unknown or paid generation is forbidden.

## Run interactively

For login, trust, or approval recovery, launch Gemini in a named tmux lane. The
helper stages the marked directive as an owner-only file and passes only an
`@<path>` reference through Gemini's `--prompt-interactive` interface:

```bash
skills/orchestrate-gemini/scripts/launch_tmux.sh \
  lane-repo-gemini-task-YYYYMMDD \
  /absolute/private-clone \
  /absolute/task.prompt \
  "$(command -v gemini)" \
  --model MODEL --sandbox
```

The launcher rejects an existing session and prints a unique dispatch ID plus
pre-capture path. After resolving setup screens under the intended identity,
prove the exact dispatch:

```bash
skills/orchestrate-gemini/scripts/verify_dispatch.sh \
  "$SESSION" "$DISPATCH_ID" "$PRE_CAPTURE"
```

For a file-backed follow-up, run
`scripts/submit_followup.sh "$SESSION" "$FOLLOWUP_FILE"`. Count work as
submitted only when start/end markers appear in order, Gemini emits the exact
post-marker acknowledgement, fresh tool or thinking activity follows, and a
clean composer returns. Session existence, stale `Working` text, or a marker
still sitting in the composer is not proof of dispatch.

## Resume and recover

Each attempt uses a new direct child of `STATE_DIR/runs` and reuses the lane's
immutable goal. Before dispatch, the runner reconciles a stale worker using its
PID, process group, and start-time identity and removes daemon-owned containers
carrying the lane/UID labels. The kernel lane lock and v0.51 global execution
lock prevent replacement dispatch until reconciliation completes. Resume only
by naming a terminal prior run:

```bash
skills/orchestrate-gemini/scripts/run_headless.py \
  ...same immutable lane arguments... \
  --resume-from /absolute/lane-state/runs/attempt-001
```

The runner accepts only a prior `succeeded` delivery with the same immutable
execution-request digest and checkout identity, then forwards that run's exact
session ID with `--resume`. Validation and failed runs are not resumable. It
never uses `latest`. Do not
resume after scope expansion, goal change, ambiguous process cleanup, identity
change, or attempt-budget exhaustion; create a newly reviewed goal instead.

## Accept the result from systems of record

Inspect `status.json`, `command.json`, stream output, process harvest state,
changed-path evidence, verification outputs, and the exact Git tree. Require
`classification: succeeded`, matching requested/reported model plus exclusive
requested-model result statistics, no survivor,
base ancestry, in-scope paths, and passing immutable commands. For a PR, rerun
the repository gates at its exact head, inspect hosted CI and review state, then
verify the merged commit from canonical `main`. A model narrative, an idle
pane, a locally green test from another SHA, or `validation_succeeded` is not
delivery.

## Primary sources

- [Gemini CLI 0.51.0 release](https://github.com/google-gemini/gemini-cli/releases/tag/v0.51.0)
- [Headless mode](https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/cli/headless.md)
- [CLI reference](https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/cli/cli-reference.md)
- [Sandboxing](https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/cli/sandbox.md)
- [Policy engine](https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/reference/policy-engine.md)
- [Trusted folders](https://github.com/google-gemini/gemini-cli/blob/v0.51.0/docs/cli/trusted-folders.md)
- [Individual-account transition](https://github.com/google-gemini/gemini-cli/discussions/28017)
