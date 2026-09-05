---
name: orchestrate-gemini
description: Safely control Google Gemini CLI (including installations used beside Antigravity) on a local or remote host with private checkouts, immutable goals, least-privilege policy, explicit sandboxing, durable leases/evidence, restart-safe exact-session resume, and system-of-record verification. Use for bounded Gemini CLI delegation, jump-box operation, auth or quota routing, interactive tmux recovery, and non-generating installation validation.
---

# Orchestrate Gemini CLI

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
4. Put the objective, allowed paths, immutable verification argv, stop
   conditions, attempt budget, auth type, and paid-generation policy in a goal
   JSON file. Start from `references/goal.example.json`; replace every example
   value before dispatch.
5. Create the lane state directory outside the checkout with mode `0700`. Store
   the prompt and a task-specific default-deny policy as owner-only files.

Use batched, read-only SSH probes before mutations. Never inspect credential
contents, transmit secrets through prompts, or infer authorization from another
process, account, checkout, or supervisor.

## Choose the control surface

| Need | Surface | Acceptance boundary |
| --- | --- | --- |
| Login, trust, approvals, discussion, or recovery | Interactive tmux | Prove exact marked submission and fresh activity |
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
creates an isolated Gemini home and temp directory, disables extensions and
network access, installs fixed container hardening flags, and probes the live
version/help. It never starts a generated turn.

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

The worker runs in a fresh process group with one absolute operation deadline.
The supervisor tracks descendants by PID plus process start time, including a
child that calls `setsid()`, and escalates TERM to KILL. Known or uncertain
survivors prevent success. Output must contain exactly one `init`, one successful
`result`, a non-empty assistant message, no fatal error, the exact requested
model, and a session ID.

After harvest, the runner obtains changed paths from Git's committed, staged,
unstaged, deleted, renamed, and untracked states. Every path must match an
immutable allowed prefix. It then runs each goal verification command as an
argv array without a shell, captures owner-only stdout/stderr, rechecks scope,
and records terminal status atomically. Exit zero means verified delivery, not
merely that Gemini stopped talking.

For `gemini-api-key`, Vertex AI, compute credentials, or gateway auth, generated
execution requires both `allow_paid_generation: true` in the immutable goal and
`ORCHESTRATE_GEMINI_PAID_GENERATION_ACK=authorized` in the wrapper environment.
The acknowledgement cannot create task authority. Personal OAuth is not treated
as a paid-capable route, but its identity and available quota must still be
verified before generation.

## Validate without generation

To exercise the real executable and container launch without contacting a
model, use a reviewed Gemini `--fake-responses` fixture:

```bash
skills/orchestrate-gemini/scripts/run_headless.py \
  ...same reviewed arguments... \
  --validation-fake-responses /absolute/reviewed.responses
```

The runner retains the fixture, marks `validation_mode: true`, and can only
finish as `validation_succeeded`. Never present this classification as delegated
work. Do not run a live smoke turn merely to strengthen validation when the
quota route is unknown or paid generation is forbidden.

## Run interactively

For login, trust, or approval recovery, launch Gemini in a named tmux lane. The
helper atomically supplies the marked directive through Gemini's current
`--prompt-interactive` interface:

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
immutable goal. A stale `lease.json` is audit history; the kernel lock is the
ownership authority. Resume only by naming a terminal prior run:

```bash
skills/orchestrate-gemini/scripts/run_headless.py \
  ...same immutable lane arguments... \
  --resume-from /absolute/lane-state/runs/attempt-001
```

The runner accepts only the same goal digest and checkout identity and forwards
that run's exact session ID with `--resume`. It never uses `latest`. Do not
resume after scope expansion, goal change, ambiguous process cleanup, identity
change, or attempt-budget exhaustion; create a newly reviewed goal instead.

## Accept the result from systems of record

Inspect `status.json`, `command.json`, stream output, process harvest state,
changed-path evidence, verification outputs, and the exact Git tree. Require
`classification: succeeded`, matching requested/resolved model, no survivor,
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
