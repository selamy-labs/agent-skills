# Gemini CLI Contained Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-contained `orchestrate-gemini` skill that runs bounded Gemini CLI repository work only inside a durable, single-writer, OS-sandboxed lane and accepts completion only from verified repository state.

**Architecture:** A Python 3.11 standard-library runner validates an immutable JSON goal, acquires kernel-backed lane and v0.51 execution leases, proves a private Git checkout plus a digest-pinned sandbox, and launches one staged-file-fed stream-JSON turn. A provider guard, allowlisting API proxy, owner-only evidence, process/container reconciliation, full-filesystem scope checks, and networkless container verification determine the terminal result. The sibling skill retains hardened tmux helpers for interactive setup only.

**Tech Stack:** Python 3.11 standard library, pytest, POSIX process control, Git CLI, Docker/Podman/gVisor capability probes, Bash tmux helpers, Markdown Agent Skill metadata.

## Global Constraints

- Add `orchestrate-gemini`; do not repurpose `orchestrate-agy` or translate AGY flags.
- Keep every runtime dependency inside the skill so single-skill installs remain usable.
- Require immutable durable goals, exact base SHAs, existing normalized allowed paths, bounded shell-free verification commands, non-empty stop conditions, attempt limits, explicit billing policy, and a digest-pinned image.
- Require a private Git common directory inside the checkout; reject linked worktrees or shared Git metadata.
- Require owner-only state/run directories outside the checkout and one non-blocking kernel lease per lane.
- Require explicit Docker, Podman, or gVisor sandboxing, isolated Gemini home/system settings/temp, and a reviewed default-deny supplemental admin policy.
- Keep prompt content out of argv by passing only an owner-only staged-file reference. For live paid API-key work, replace a non-secret outer placeholder with an owner-only runtime env file in the provider guard; never record credential values.
- Reject YOLO, `--skip-trust`, raw output, inherited sandbox mounts/flags, standard admin-policy conflicts, and paid-capable auth without two-part authorization.
- Preserve conservative process-group and observed-descendant cleanup on success, error, timeout, and interruption.
- Exit zero only for strict stream output, clean process/container harvest, in-scope Git and full-filesystem state, immutable Git metadata, exclusive requested-model statistics, and passing networkless container verification.
- Validate the real installation without paid generation or shared-checkout mutation; deterministic tests remain the merge gate.

---

### Task 1: Durable goal, private checkout, and lease ratchet

**Files:**
- Create: `tests/test_orchestrate_gemini_headless.py`
- Create: `skills/orchestrate-gemini/scripts/run_headless.py`

**Interfaces:**
- Consumes: `run_headless.py --run-dir PATH --state-dir PATH --cwd PATH --goal-file PATH --prompt-file PATH --policy-file PATH --gemini PATH --sandbox-provider {docker,podman,runsc} --timeout-seconds INT --model MODEL --approval-mode {plan,default,auto_edit} [--credential-env-file PATH] [--preflight-only] [--resume-from PATH] [--validation-fake-responses PATH]`
- Produces: validated `Goal`, a stable `goal.json`/`goal.sha256`, exclusive `lease.lock`, atomic `lease.json`, and exact Git preflight evidence.

- [ ] **Step 1: Write the manifest validation tests**

  Create a valid goal fixture with schema version `1`, a stable ID/objective,
  the temporary repository's exact 40-character `HEAD`, normalized allowed
  prefixes, argv-based verification commands with positive timeouts, non-empty
  stop conditions, `max_attempts`, `auth_type`, non-secret
  `credential_identity`, and `allow_paid_generation`.
  Add separate tests that reject missing fields, unknown fields, empty strings,
  duplicate or escaping paths, shell-string verification commands, non-positive
  budgets, and malformed SHAs.

  ```python
  result = run_runner(tmp_path, goal={"allowed_paths": ["../outside"]}, preflight=True)
  assert result.returncode != 0
  assert read_status(result)["classification"] == "invalid_goal"
  ```

- [ ] **Step 2: Run the first manifest test and verify RED**

  Run: `python -m pytest -q tests/test_orchestrate_gemini_headless.py::test_preflight_persists_an_immutable_durable_goal`

  Expected: FAIL because `skills/orchestrate-gemini/scripts/run_headless.py` does not exist.

- [ ] **Step 3: Add immutable-goal and attempt-budget tests**

  Assert first use creates owner-only canonical goal state, a second attempt with
  the same digest succeeds, changed bytes classify `goal_mismatch`, and attempts
  beyond `max_attempts` classify `attempt_exhausted`. Assert a new run directory
  is mandatory and the state/run directories cannot be nested under the worker
  checkout.

- [ ] **Step 4: Add Git-boundary and lease tests**

  Use real temporary Git repositories. Assert a clean private clone at the exact
  base passes, while a dirty start, wrong HEAD, non-repository, and linked
  worktree whose common Git directory resolves outside the checkout fail with
  exact classifications. Hold `fcntl.flock(LOCK_EX)` in a sibling process and
  assert a concurrent run classifies `lane_locked`; after releasing the lock,
  stale lease metadata must not block the next attempt.

- [ ] **Step 5: Implement the minimum validation and lease layer**

  Add frozen dataclasses for `Goal`, `VerificationCommand`, and `GitState`.
  Parse JSON with exact-key checking, normalize allowed paths with
  `PurePosixPath`, serialize the canonical goal with sorted keys, and hash it
  with SHA-256. Create private files with `O_EXCL`, write mutable JSON through a
  mode-`0600` temporary sibling plus `os.replace`, and hold `fcntl.flock` for the
  attempt lifetime.

  Resolve Git facts with argv-only subprocess calls:

  ```python
  git = ["git", "-C", str(cwd)]
  head = checked_output([*git, "rev-parse", "HEAD"])
  common = checked_output([*git, "rev-parse", "--path-format=absolute", "--git-common-dir"])
  status = checked_output([*git, "status", "--porcelain=v1", "-z", "--untracked-files=all"])
  ```

  Require `head == goal.base_sha`, empty status, and `common` beneath the
  resolved checkout. Record all failures atomically before releasing the lock.

- [ ] **Step 6: Verify GREEN and commit**

  Run: `python -m pytest -q tests/test_orchestrate_gemini_headless.py -k 'goal or attempt or git or lease'`

  Expected: all selected tests pass.

  Commit: `feat(orchestrate-gemini): add durable lane preflight`

### Task 2: Gemini, sandbox, policy, and billing preflight

**Files:**
- Modify: `tests/test_orchestrate_gemini_headless.py`
- Modify: `skills/orchestrate-gemini/scripts/run_headless.py`

**Interfaces:**
- Consumes: Task 1's validated lane and goal.
- Produces: captured Gemini version/help, sandbox-provider proof, policy proof, isolated runtime directories/settings, and `preflight_succeeded|capability_mismatch|sandbox_unavailable|policy_conflict|invalid_policy|billing_blocked`.

- [ ] **Step 1: Add capability and isolation tests**

  Fake Gemini must advertise the exact 0.51-compatible flags. Assert missing
  `--output-format`, `--model`, `--approval-mode`, `--sandbox`, or
  `--admin-policy` is rejected. Fake provider discovery through a controlled
  `PATH`; assert an unavailable or mismatched requested provider is rejected.
  Assert the runner creates isolated `gemini-home`, `tmp`, and immutable system
  settings under the lane, sets `GEMINI_CLI_HOME`, `TMPDIR`,
  `GEMINI_CLI_SYSTEM_SETTINGS_PATH`, and `GEMINI_SANDBOX`, and never inherits
  caller `SANDBOX_MOUNTS` or `SANDBOX_FLAGS`.

- [ ] **Step 2: Run one capability test and verify RED**

  Run: `python -m pytest -q tests/test_orchestrate_gemini_headless.py::test_preflight_builds_an_isolated_gemini_runtime`

  Expected: FAIL because the runner does not yet probe or build the isolated runtime.

- [ ] **Step 3: Add policy and billing tests**

  Require a regular owner-controlled TOML file containing a catch-all `deny`
  rule plus at least one narrower higher-priority `allow` rule. Reject symlinks,
  group/other-writable files, missing deny/allow rules, and a populated standard
  admin policy directory that would supersede `--admin-policy`. Assert
  live execution is restricted to `gemini-api-key`, supplied through an
  owner-only env file. It is blocked unless both `allow_paid_generation` and
  `ORCHESTRATE_GEMINI_PAID_GENERATION_ACK=authorized` are present. Consumer
  OAuth, Vertex AI, and gateway goals fail closed because this v0.51 workflow
  does not prove those routes;
  `--preflight-only` never needs this acknowledgement because it cannot issue a
  model request.
  Reject every `run_shell_command`/`ShellTool` allow because a sandbox child
  could inspect the parent worker's credential environment.

- [ ] **Step 4: Implement capability, runtime, policy, and billing checks**

  Run version/help probes under the one attempt deadline. Create system settings
  that disable auto-update, YOLO, permanent approvals, raw project env loading,
  extensions through `admin.extensions.enabled = false`, and telemetry; enable
  environment redaction; and fix the selected sandbox provider. Add `-e none`
  to the fixed CLI arguments. Construct a minimal child environment from an explicit
  safe-name allowlist and record names only. Reject reserved environment input
  rather than merging it.

- [ ] **Step 5: Verify GREEN and commit**

  Run: `python -m pytest -q tests/test_orchestrate_gemini_headless.py -k 'capability or sandbox or policy or billing or preflight'`

  Expected: all selected tests succeed with no model process launched in preflight-only cases.

  Commit: `feat(orchestrate-gemini): enforce sandboxed Gemini preflight`

### Task 3: Bounded dispatch, evidence, cleanup, and verified acceptance

**Files:**
- Modify: `tests/test_orchestrate_gemini_headless.py`
- Modify: `skills/orchestrate-gemini/scripts/run_headless.py`

**Interfaces:**
- Consumes: Task 2's immutable runtime and reviewed lane.
- Produces: staged-file Gemini execution with only an `@<path>` reference in argv; `stdout.jsonl`, `stderr.log`, Git and verification artifacts; atomic terminal status; exit zero only for verified success.

- [ ] **Step 1: Add command and stream tests**

  Assert the exact command contains `--output-format stream-json`, an explicit
  non-alias model, non-YOLO approval mode, `--sandbox`, and `--admin-policy`, but
  never the prompt, raw-output flags, `--skip-trust`, or `--worktree`. Assert the
  prompt bytes are loaded from the staged owner-only file. Cover valid `init/message/result`, empty
  assistant content, duplicate init/result, malformed JSON, fatal error events,
  terminal error status, model mismatch, and nonzero CLI exit.

- [ ] **Step 2: Run the command test and verify RED**

  Run: `python -m pytest -q tests/test_orchestrate_gemini_headless.py::test_runner_keeps_prompt_content_out_of_process_arguments`

  Expected: FAIL because dispatch is not implemented.

- [ ] **Step 3: Add lifecycle tests before implementation**

  Port the relevant AGY regression classes without weakening them: timeout child
  cleanup, SIGINT/SIGTERM cleanup, repeated termination signals, a child that
  calls `setsid()`, PID start-time reuse, unavailable/partial process inventory,
  unknown group state, launch callback failure, one absolute operation deadline,
  and a fixed cleanup grace. Every case asserts terminal evidence
  and no known survivor.

- [ ] **Step 4: Implement bounded execution and conservative harvesting**

  Launch Gemini with `start_new_session=True`, owner-only staged prompt/stdout/stderr
  files, and the minimal environment. Track process identities from bounded
  `ps` snapshots; update lease/status heartbeat only when output size advances.
  On every terminal path, TERM then KILL the owned group and every observed
  identity, rejecting incomplete or ambiguous absence proof. Keep repeat
  termination handlers ignored until cleanup and final status finish.

- [ ] **Step 5: Add Git scope and verification tests**

  Cover committed, staged, unstaged, deleted, renamed, and untracked paths.
  Assert every path comes from immutable Git porcelain/diff queries and lies
  under an allowed prefix; reject changed `HEAD` that is not descended from
  base. Execute each manifest verification argv without a shell, under its
  timeout and remaining attempt budget, capturing stdout/stderr and classifying
  any timeout/nonzero exit as `verification_failed`.

- [ ] **Step 6: Implement acceptance and resume**

  Parse JSONL strictly, require one init/result, a matching concrete model,
  non-empty assistant text, and terminal success. Record the session ID.
  `--resume-from` must name a terminal prior run with the same goal digest and
  checkout identity; forward its exact session ID via `--resume`, never `latest`.
  Run Git and verification acceptance only after worker harvest is proven.

- [ ] **Step 7: Verify GREEN and commit**

  Run: `python -m pytest -q tests/test_orchestrate_gemini_headless.py`

  Expected: all tests succeed and lifecycle cases leave no live known process.

  Commit: `feat(orchestrate-gemini): verify bounded headless delivery`

### Task 4: Public skill workflow and interactive dispatch

**Files:**
- Create: `skills/orchestrate-gemini/SKILL.md`
- Create: `skills/orchestrate-gemini/agents/openai.yaml`
- Create: `skills/orchestrate-gemini/scripts/launch_tmux.sh`
- Create: `skills/orchestrate-gemini/scripts/submit_followup.sh`
- Create: `skills/orchestrate-gemini/scripts/verify_dispatch.sh`
- Modify: `tests/test_cli_orchestration_skills.py`
- Modify: `tests/test_orchestrate_gemini_headless.py`

**Interfaces:**
- Consumes: the runner and existing cross-client dispatch protocol.
- Produces: discoverable public guidance for capability/auth setup, interactive recovery, contained headless work, restart, and system-of-record acceptance.

- [ ] **Step 1: Add Gemini to shared tmux regressions and verify RED**

  Add `gemini` to the parameterized tool tuple. The launcher must supply the
  marked directive to `--prompt-interactive`, reject an existing session, and
  retain the established exact acknowledgement/activity/clean-composer proof.

  Run: `python -m pytest -q tests/test_cli_orchestration_skills.py`

  Expected: FAIL because `skills/orchestrate-gemini/scripts` is absent.

- [ ] **Step 2: Add skill conformance tests and verify RED**

  Require the skill to distinguish Gemini from AGY; require durable goals,
  private clones, OS sandbox, supplemental admin policy, lease/heartbeat,
  billing/auth proof, preflight, resume, scope/verification evidence, and real
  artifact acceptance. Require versioned official source links.

- [ ] **Step 3: Add interactive helpers and public guidance**

  Adapt the existing shell helpers only where Gemini's composer and
  `--prompt-interactive` contract match; retain their quoting and causal proof.
  Write concise progressive-disclosure guidance around the executable runner,
  including the non-obvious 0.51 policy-tier and temp-mount risks. Add metadata:

  ```yaml
  interface:
    display_name: "Orchestrate Gemini CLI"
    short_description: "Run Gemini CLI in bounded, isolated repository lanes"
    default_prompt: "Use $orchestrate-gemini to run and verify a contained Gemini CLI worker."
  ```

- [ ] **Step 4: Verify GREEN and commit**

  Run: `python -m pytest -q tests/test_cli_orchestration_skills.py tests/test_orchestrate_gemini_headless.py`

  Run: `python scripts/lint_skills.py`

  Expected: focused tests and skill lint pass.

  Commit: `docs(orchestrate-gemini): define contained control workflow`

### Task 5: Real-installation validation without paid generation

**Files:**
- Verify only: isolated remote temporary checkout, lane state, and evidence outside any live supervisor path.

**Interfaces:**
- Consumes: exact committed runner and a disposable remote Git repository.
- Produces: sanitized version/provider/preflight evidence and a no-cost sandboxed validation classification.

- [ ] **Step 1: Create a disposable remote lane**

  Use one bounded non-interactive SSH call to create a new private temporary
  root, initialize a tiny Git repository, commit a fixture base, and copy only
  the exact committed skill directory plus a non-secret plan-mode goal/policy.
  Record the path locally but never place environment-specific names or paths in
  the public repository.

- [ ] **Step 2: Run real preflight-only validation**

  Invoke the runner against the absolute installed Gemini executable and Docker
  provider with `--preflight-only`. Assert version/help, goal, policy, Git
  boundary, sandbox provider, private runtime, and lease evidence all succeed;
  assert no Gemini model process and no non-disposable path changed.

- [ ] **Step 3: Exercise sandbox launch with Gemini's fake-response source**

  In the disposable lane only, provide a reviewed `--fake-responses` fixture
  using Gemini 0.51.0's documented test
  format. Invoke the runner's validation-only mode and require
  `classification=validation_succeeded`, `validation_mode=true`, a retained
  fixture digest, valid init/message/result events, no worker/container survivor,
  and no Git change. The runner must never classify this evidence as operational
  delivery. Do not read, copy, or invoke configured live credentials.

- [ ] **Step 4: Remove the disposable lane and retain sanitized evidence**

  Terminate only owned processes/containers, verify absence, remove only the
  exact temporary root, and retain non-secret status/probe summaries for the PR.

### Task 6: Full gates, review, PR, and canonical merge proof

**Files:**
- Verify: all changed paths against current `origin/main`.

**Interfaces:**
- Consumes: exact branch head and sanitized remote evidence.
- Produces: one focused pull request, green required checks, independent review evidence, merged canonical commit.

- [ ] **Step 1: Run repository gates in workflow order**

  ```bash
  python scripts/lint_skills.py
  python scripts/privacy_scan.py
  python scripts/test_privacy_scan.py
  python scripts/security_scan.py
  python scripts/test_security_scan.py
  python scripts/public_api_stability.py
  python scripts/test_pr_workflow_cancellation.py
  python scripts/test_semantic_release.py
  python scripts/validate_pr_title.py --title "feat(orchestrate-gemini): add contained CLI control" --body "Diagram impact: none - standalone CLI skill, runner, and tests only."
  ruff check tools/ tests/
  ruff format --check --diff .
  pytest --cov=tools --cov-report=term-missing --cov-fail-under=90
  git diff --check origin/main...HEAD
  ```

  Expected: every command exits `0` with only documented platform skips.

- [ ] **Step 2: Adversarially self-review the exact diff**

  Try to defeat private-clone proof, path normalization, lease exclusivity,
  prompt/secret redaction, sandbox/environment isolation, policy precedence,
  billing gates, JSONL completion, PID identity, cleanup deadlines, resume
  identity, and verification immutability. Add a failing regression before every
  corrective production change.

- [ ] **Step 3: Obtain independent review of the exact head**

  Give a clean reviewer the spec, exact diff, test outputs, and sanitized remote
  evidence. Require an explicit `SHIP|REVISE` verdict and strongest security,
  correctness, portability, and scope objections. Resolve every `REVISE` finding
  test-first and rerun affected plus full gates.

- [ ] **Step 4: Open and shepherd the PR**

  Push once locally green. The PR body must map acceptance criteria to evidence,
  name the AGY/Gemini separation decision, disclose that no paid live turn was
  used, include real-installation preflight/negative-sandbox evidence, and state
  diagram impact. Watch actual required checks and review state; fix failures on
  the branch and never substitute local or stale-SHA evidence.

- [ ] **Step 5: Merge and verify the system of record**

  Merge only when the PR is mergeable, every applicable required check is green,
  and independent review is `SHIP`. Fetch `origin/main`, verify the PR merge
  commit is an ancestor, verify expected paths at that exact commit, and report
  the canonical PR and merge identities.
