# Antigravity Headless Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded, evidence-producing headless runner to the existing `orchestrate-agy` skill and reject silent permission/no-output failures.

**Architecture:** A Python 3.11 standard-library script probes the live AGY executable, validates its advertised model and flags, then sends one sandboxed NDJSON turn over stdin in a fresh process group. It streams evidence to owner-only files, atomically updates a status envelope, and harvests the group plus observed descendants on every terminal path.

**Tech Stack:** Python 3.11 standard library, pytest, existing skill Markdown and OpenAI metadata.

## Global Constraints

- Extend `orchestrate-agy`; do not create a duplicate skill or generalize unrelated CLI skills.
- Require absolute paths, a new evidence directory outside the worker cwd, an explicit model, `plan|accept-edits`, and a positive timeout.
- Always use `--sandbox`, streaming JSON input/output, AGY's log file, one shared operation timeout, and a slightly longer cleanup grace.
- Prefer scoped AGY permission rules; blanket bypass requires a runner flag plus `ORCHESTRATE_AGY_PERMISSION_BYPASS_ACK=authorized`.
- Treat a zero-exit `SUCCESS` envelope with an empty response as failure and preserve stderr.
- Use no third-party runtime dependencies and expose neither credentials nor the prompt in process argv.

---

### Task 1: Headless runner regression ratchet

**Files:**
- Create: `tests/test_orchestrate_agy_headless.py`
- Create: `skills/orchestrate-agy/scripts/run_headless.py`

**Interfaces:**
- Consumes: `run_headless.py --run-dir PATH --cwd PATH --prompt-file PATH --agy PATH --timeout-seconds INT --mode {plan,accept-edits} --model SLUG [--effort {low,medium,high}] [--allow-all-permissions]`
- Produces: exit code `0` only for a non-empty `SUCCESS` response; `status.json`, `prompt.txt`, `input.ndjson`, `command.json`, `agy-version.*`, `agy-help.*`, `agy-models.*`, `stdout.json`, `stderr.log`, and `agy.log` in the new run directory.

- [ ] **Step 1: Write the fake-CLI test fixture and command-construction test**

  Create a fake executable that handles `--version`, `--help`, and `models`, then records the real-run argv, reads one NDJSON user event from stdin, and emits a configurable result event. Assert that a multiline shell-sensitive prompt is absent from argv and intact in stdin, sandbox/stream/timeout/log flags are present, the exact mode/model/effort are present, and discovery files plus a successful status are durable.

  ```python
  result = subprocess.run(
      [
          sys.executable,
          str(RUNNER),
          "--run-dir", str(run_dir),
          "--cwd", str(tmp_path),
          "--prompt-file", str(prompt),
          "--agy", str(fake_agy),
          "--timeout-seconds", "2",
          "--mode", "plan",
          "--model", "gemini-test-medium",
          "--effort", "high",
      ],
      env=os.environ | {"FAKE_AGY_ARGV": str(argv_path)},
      capture_output=True,
      text=True,
  )
  assert result.returncode == 0
  assert prompt.read_text() not in json.loads(argv_path.read_text())
  assert json.loads((run_dir / "input.ndjson").read_text())["message"]["content"] == prompt.read_text()
  assert json.loads((run_dir / "status.json").read_text())["classification"] == "succeeded"
  ```

- [ ] **Step 2: Run the command-construction test and verify RED**

  Run: `python -m pytest -q tests/test_orchestrate_agy_headless.py::test_runner_constructs_a_sandboxed_bounded_command`

  Expected: FAIL because `skills/orchestrate-agy/scripts/run_headless.py` does not exist.

- [ ] **Step 3: Add failure-mode tests**

  Add separate named cases for: zero-exit `SUCCESS` plus empty response and permission stderr; nonzero exit; empty stdout; malformed JSON; non-success AGY status; unavailable model; timeout with a child process in the same process group; SIGTERM while a worker is active; a descendant that starts a new session; a launch error; probes sharing the overall deadline; evidence nested under the worker cwd; and `--allow-all-permissions` without the acknowledgement environment variable. Each test asserts a nonzero wrapper exit and the exact final `classification` where a run directory is created.

  ```python
  status = json.loads((run_dir / "status.json").read_text())
  assert result.returncode != 0
  assert status["classification"] == "permission_blocked"
  assert "headless mode cannot prompt" in (run_dir / "stderr.log").read_text()
  ```

- [ ] **Step 4: Implement the minimal runner**

  Implement a frozen `ProcessResult` dataclass with `returncode`, `pid`,
  `process_group_id`, `timed_out`, interruption/launch error state,
  observed-descendant state, `survivors_harvested`, and
  `process_group_alive_after_harvest` fields. Add `run_process`,
  `write_status`, `advertised_models`, and `classify_result` functions with the
  signatures used by the tests.

  Launch the child in a new session, poll its descendant tree while applying a
  shared absolute deadline, and install SIGINT/SIGTERM handling around the
  supervised operation. Feed the owner-only `input.ndjson` file to stdin:

  ```python
  process = subprocess.Popen(
      command,
      cwd=cwd,
      stdin=input_file,
      stdout=stdout_file,
      stderr=stderr_file,
      start_new_session=True,
  )
  while process.poll() is None and time.monotonic() < deadline:
      observed_descendants.update(descendant_pids(process.pid, process_snapshot()))
      time.sleep(POLL_INTERVAL_SECONDS)
  ```

  On normal exit, timeout, interruption, or an exception after launch, harvest
  the owned process group unconditionally and every observed descendant
  identity (PID plus start time) directly with a TERM/KILL sequence. Group cleanup must
  use kernel group probes and must not trust process inventory, including a
  nonempty but partial snapshot. Ignore repeated termination signals until
  cleanup finishes, record group state as `absent|alive|unknown`, prove a
  zombie-only group as absent only from a complete inventory whether the kernel
  probe succeeds or returns `EPERM`, treat ambiguous state as a harvest failure,
  and never signal a stale PID whose start time changed. Verify every tracked
  descendant independently of process group so a child racing `setsid()`
  against a group probe remains covered.
  Reject descendant state as unknown if incomplete inventory omits an observed
  identity before absence is proved. Use one
  absolute cleanup deadline, cap every process-inventory subprocess to the
  remaining time, and start no new inventory probe after the deadline. Cap an
  early completion or interruption to one short cleanup grace rather than the
  unused operation budget.
  `write_status` must write mode `0600` to a
  sibling temporary file and replace `status.json` atomically.
  `advertised_models` must return the first whitespace-delimited field from
  each nonblank model-list line except progress lines. `classify_result` must
  prioritize incomplete harvesting before interruption, timeout, launch error,
  nonzero exit, malformed output, non-`SUCCESS`, permission-blocked empty
  response, and other empty response. Capability probes must apply that same
  harvest-completeness predicate. Process launch and unexpected wrapper errors
  must produce terminal classifications rather than stale running evidence.

- [ ] **Step 5: Run focused tests and verify GREEN**

  Run: `python -m pytest -q tests/test_orchestrate_agy_headless.py`

  Expected: all headless-runner tests pass; lifecycle cases leave no live process group or observed descendant.

- [ ] **Step 6: Run shared orchestration regressions**

  Run: `python -m pytest -q tests/test_cli_orchestration_skills.py tests/test_orchestrate_agy_headless.py`

  Expected: all tests pass, with only existing platform-dependent tmux skips when tmux is unavailable.

- [ ] **Step 7: Commit the runner and ratchet**

  ```bash
  git add skills/orchestrate-agy/scripts/run_headless.py tests/test_orchestrate_agy_headless.py
  git commit -m "feat(orchestrate-agy): add bounded headless runner"
  ```

### Task 2: Executable guidance and metadata

**Files:**
- Modify: `skills/orchestrate-agy/SKILL.md`
- Modify: `skills/orchestrate-agy/agents/openai.yaml`

**Interfaces:**
- Consumes: the Task 1 runner CLI and evidence classifications.
- Produces: public instructions that select interactive versus headless execution, configure scoped permissions, operate the runner, harvest evidence, fall back cleanly, and independently verify delegated artifacts.

- [ ] **Step 1: Add a documentation conformance test**

  Extend `tests/test_orchestrate_agy_headless.py` with one test that requires the skill to name `run_headless.py`, both execution modes, scoped permission rules, `--dangerously-skip-permissions`, `permission_blocked`, fallback, one-writer worktrees, and independent artifact verification.

- [ ] **Step 2: Run the conformance test and verify RED**

  Run: `python -m pytest -q tests/test_orchestrate_agy_headless.py::test_skill_documents_the_headless_control_contract`

  Expected: FAIL because the current skill does not describe the runner or its classifications.

- [ ] **Step 3: Update the skill and metadata**

  Add a compact decision table for interactive, headless `plan`, and headless `accept-edits`. Document capability/model discovery, prompt-file transport, sandbox-versus-mode semantics, scoped permissions, the guarded bypass, durable run artifacts, timeout/process harvesting, all failure classifications, clean fallback, one-writer isolation, and independent acceptance. Link the four primary Google documentation pages from the design.

  Change the OpenAI metadata to:

  ```yaml
  interface:
    display_name: "Orchestrate AGY"
    short_description: "Run AGY in bounded interactive or headless sessions"
    default_prompt: "Use $orchestrate-agy to run and verify a bounded AGY worker."
  ```

- [ ] **Step 4: Run focused validation and verify GREEN**

  Run: `python -m pytest -q tests/test_orchestrate_agy_headless.py tests/test_cli_orchestration_skills.py`

  Run: `python scripts/lint_skills.py`

  Expected: all tests succeed and every skill validates.

- [ ] **Step 5: Commit the guidance**

  ```bash
  git add skills/orchestrate-agy/SKILL.md skills/orchestrate-agy/agents/openai.yaml tests/test_orchestrate_agy_headless.py
  git commit -m "docs(orchestrate-agy): define bounded control workflow"
  ```

### Task 3: Pre-push proof and review

**Files:**
- Verify only: all changed paths against `origin/main`

**Interfaces:**
- Consumes: exact branch head from Tasks 1 and 2.
- Produces: a locally green, adversarially reviewed commit ready for one push and PR.

- [ ] **Step 1: Run the repository's exact CI gates in workflow order**

  ```bash
  python scripts/lint_skills.py
  python scripts/privacy_scan.py
  python scripts/test_privacy_scan.py
  python scripts/security_scan.py
  python scripts/test_security_scan.py
  python scripts/public_api_stability.py
  python scripts/test_pr_workflow_cancellation.py
  python scripts/test_semantic_release.py
  python scripts/validate_pr_title.py --title "feat(orchestrate-agy): add bounded headless control" --body "Diagram impact: none - command-line skill and tests only."
  ruff check tools/ tests/
  ruff format --check --diff .
  pytest --cov=tools --cov-report=term-missing --cov-fail-under=90
  ```

  Expected: every command exits `0`.

- [ ] **Step 2: Inspect the exact diff and identity**

  Verify `git diff origin/main...HEAD`, `git diff --check`, changed-path scope, executable mode on `run_headless.py`, neutral commit author, clean status, and absence of credential-like material.

- [ ] **Step 3: Obtain an adversarial SHIP verdict**

  Review from a clean base/diff context with the instruction to find the strongest correctness, process-control, security, and requirement-coverage reasons not to ship. Fix and re-run all affected gates on any `REVISE` verdict; retain the final verdict as PR evidence.
