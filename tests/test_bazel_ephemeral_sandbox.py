"""Behavior tests for the bazel-ephemeral-sandbox lifecycle wrapper.

The wrapper (scripts/bazel_sandbox.py, entered through scripts/bazel-sandbox.sh)
runs against a stub `bazel`/`bazelisk` with a fake detached server process and a
stub `df` on PATH, so no real Bazel build runs and disk pressure is simulated.
Every test is AAA with one assertion. Property cases use a pinned stdlib
generator so failures reproduce.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "bazel-ephemeral-sandbox"
SCRIPT = SKILL / "scripts" / "bazel-sandbox.sh"
MODULE = SKILL / "scripts" / "bazel_sandbox.py"
GIT = shutil.which("git") or "git"
SEED = 20261110
GIB_KIB = 1 << 20
PLENTY_KIB = str(64 * GIB_KIB)
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

STUB_BAZEL = r"""#!/usr/bin/env bash
set -eu
out="" base="" cmd="" disk="" repos="" lockmode=update
for arg in "$@"; do
  [[ $arg != -- ]] || break
  case $arg in
    --output_user_root=*) out=${arg#*=} ;;
    --output_base=*) base=${arg#*=} ;;
    --disk_cache=*) disk=${arg#*=} ;;
    --repository_cache=*) repos=${arg#*=} ;;
    --lockfile_mode=*) lockmode=${arg#*=} ;;
    -*) ;;
    *) [[ -n $cmd ]] || cmd=$arg ;;
  esac
done
printf '%s %s\n' "$(basename "$0")" "$*" >>"$STUB_LOG"
pidfile=$base/server/server.pid.txt
if [[ $cmd == shutdown ]]; then
  case ${STUB_SHUTDOWN:-ok} in
    fail) exit 1 ;;
    hang) exec sleep 30 ;;
  esac
  if [[ -f $pidfile ]]; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
    while kill -0 "$(cat "$pidfile")" 2>/dev/null; do sleep 0.05; done
    rm -f "$pidfile"
  fi
  exit 0
fi
echo "$$" >>"$STUB_PIDS"
if [[ ${STUB_ACTION:-} == symlink-out ]]; then
  mkdir -p "$ESCAPE_DIR"
  echo keep >"$ESCAPE_DIR/precious"
  rmdir "$out"
  ln -s "$ESCAPE_DIR" "$out"
  exit "${STUB_EXIT:-0}"
fi
mkdir -p "$out/install/ro" "$base/server"
# Like Bazel 7+, write MODULE.bazel.lock into the workspace unless told not to.
[[ $lockmode == off || ! -f MODULE.bazel ]] || echo "{}" >MODULE.bazel.lock
echo data >"$out/install/ro/file"
chmod 0555 "$out/install/ro"
# Simulate cache growth: 1 MiB into whichever caches Bazel was told to use.
for cache in "$disk" "${repos:-$out/cache/repos/v1}"; do
  [[ -n $cache ]] || continue
  mkdir -p "$cache/cas"
  head -c 1048576 /dev/zero >"$cache/cas/blob-$$"
done
if [[ -z ${STUB_NO_SERVER:-} ]]; then
  (
    [[ -z ${STUB_SERVER_IGNORE_TERM:-} ]] || trap '' TERM
    # Like a real server, run with its cwd inside the output base.
    cd "$base"
    exec -a "bazel-server --output_base=$base" sleep 300
  ) </dev/null >/dev/null 2>&1 &
  echo "$!" >"$pidfile"
  echo "$!" >>"$STUB_PIDS"
fi
case ${STUB_ACTION:-} in
  dirty) echo scratch >untracked.txt ;;
  commit) git commit --allow-empty -q -m "local only" ;;
  holder)
    (cd "$out/install" && exec sleep 300) </dev/null >/dev/null 2>&1 &
    echo "$!" >>"$STUB_PIDS"
    ;;
  swap-task)
    task=$(dirname "$out")
    mv "$task" "$task.moved"
    mkdir -m 0700 "$task"
    mkdir -p "$out" "$task/src"
    ;;
  record-head) git rev-parse HEAD >"$STUB_HEAD" ;;
  reset-away)
    git commit --allow-empty -q -m "reset away"
    git reset -q --hard HEAD~1
    ;;
  long-reflog)
    published=$(git rev-parse HEAD)
    git commit --allow-empty -q -m "reset away beyond enumeration boundary"
    git rev-parse HEAD >"$STUB_RESET_SHA"
    git reset -q --hard "$published"
    for ((i=0; i<105; i++)); do
      git reset -q --hard "$published~1"
      git reset -q --hard "$published"
    done
    ;;
  swap-output-directory)
    task=$(dirname "$out")
    mv "$out" "$out-old"
    mv "$task/src" "$out"
    echo preserve >"$out/precious.txt"
    cp -a "$out" "$task/src"
    rm "$task/src/precious.txt"
    ;;
  wt-holder)
    (exec sleep 300) </dev/null >/dev/null 2>&1 &
    echo "$!" >>"$STUB_PIDS"
    ;;
  self-kill) kill -9 $$ ;;
  ignored) echo build-output >build.ignored ;;
  skip-worktree) git update-index --skip-worktree MODULE.bazel ;;
  assume-unchanged) git update-index --assume-unchanged MODULE.bazel ;;
  hidden-dirty) git update-index --assume-unchanged MODULE.bazel; echo precious >>MODULE.bazel ;;
  commit-break-remote)
    git commit --allow-empty -q -m "local only"
    mv "$STUB_ORIGIN" "$STUB_ORIGIN.gone"
    ;;
  symlink-src)
    task=$(dirname "$out")
    mkdir -p "$ESCAPE_DIR"
    mv "$task/src" "$ESCAPE_DIR/src-moved"
    ln -s "$ESCAPE_DIR/src-moved" "$task/src"
    ;;
  trap-int)
    trap 'echo int >"$STUB_INT_MARK"; exit 8' INT
    [[ -z ${STUB_STARTED:-} ]] || : >"$STUB_STARTED"
    sleep 60 &
    wait $!
    ;;
esac
[[ -z ${STUB_STARTED:-} ]] || : >"$STUB_STARTED"
# Like a real client, keep the full command line (including the output root) visible.
[[ -z ${STUB_SLEEP:-} ]] || exec -a "bazel-client $*" sleep "$STUB_SLEEP"
exit "${STUB_EXIT:-0}"
"""

STUB_DF = r"""#!/usr/bin/env bash
# FAKE_DF_KB is a comma-separated list of free KiB values; the last one repeats.
n=$(cat "$FAKE_DF_STATE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" >"$FAKE_DF_STATE"
IFS=, read -r -a values <<<"$FAKE_DF_KB"
((n <= ${#values[@]})) || n=${#values[@]}
value=${values[n - 1]}
[[ -z ${FAKE_DF_LOW_MATCH:-} || $3 != *"$FAKE_DF_LOW_MATCH"* ]] || value=1
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "fake 1 1 $value 1% /"
"""


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_IDENTITY}
    return subprocess.run([GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def _write_exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _pid_gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state == "Z"


def _tree_bytes(path: Path) -> int:
    return sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bazel_sandbox", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bazel_sandbox"] = module
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@dataclass
class Result:
    returncode: int
    root: Path
    repo: Path
    log: Path
    audit_path: Path
    stdout: str = ""
    stderr: str = ""

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def build_args(self) -> list[str]:
        return next(c for c in self.calls() if " shutdown" not in c).split()

    def audit(self) -> dict[str, object]:
        return json.loads(self.audit_path.read_text().splitlines()[-1])

    def task_audit(self) -> dict[str, object]:
        lines = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
        return [line for line in lines if line["id"]][-1]

    def task_children(self, name: str) -> list[Path]:
        return list(self.root.glob(f"*/{name}"))

    def worktrees(self) -> int:
        return _git(self.repo, "worktree", "list", "--porcelain").count("worktree ")


@dataclass
class Sandbox:
    tmp: Path
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        _write_exe(self.bin / "bazel", STUB_BAZEL)
        _write_exe(self.bin / "df", STUB_DF)
        self.root = self.tmp / "cache" / "bazel-sandboxes"
        self.log = self.tmp / "bazel.log"
        self.pids = self.tmp / "stub.pids"
        self.audit_path = self.tmp / "state" / "audit.jsonl"
        origin = self.tmp / "origin.git"
        self.repo = self.tmp / "repo"
        self.env = {
            **GIT_IDENTITY,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "BAZEL_SANDBOX_ROOT": str(self.root),
            "BAZEL_SANDBOX_REPO": str(self.repo),
            "BAZEL_SANDBOX_AUDIT": str(self.audit_path),
            "BAZEL_SANDBOX_WATCH_INTERVAL": "0.1",
            "BAZEL_SANDBOX_KILL_WAIT": "1",
            "BAZEL_SANDBOX_SHUTDOWN_TIMEOUT": "2",
            "FAKE_DF_KB": PLENTY_KIB,
            "FAKE_DF_STATE": str(self.tmp / "df.state"),
            "STUB_LOG": str(self.log),
            "STUB_PIDS": str(self.pids),
            "STUB_INT_MARK": str(self.tmp / "int.mark"),
            "ESCAPE_DIR": str(self.tmp / "escape"),
            "STUB_ORIGIN": str(self.tmp / "origin.git"),
        }
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        _git(self.tmp, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "MODULE.bazel").write_text('module(name = "fixture")\n')
        # Durable baseline includes the lockfile that Bazel would generate.
        (self.repo / "MODULE.bazel.lock").write_text("{}\n")
        (self.repo / ".gitignore").write_text("*.ignored\n")
        _git(self.repo, "add", "MODULE.bazel", "MODULE.bazel.lock", ".gitignore")
        _git(self.repo, "commit", "-q", "-m", "init")
        _git(self.repo, "remote", "add", "origin", str(origin))
        _git(self.repo, "push", "-q", "-u", "origin", "main")

    def command(self, branch: str, *bazel_args: str) -> list[str]:
        return [str(SCRIPT), branch, "--", *(bazel_args or ("build", "//..."))]

    def invoke(self, argv: list[str], **env: str) -> Result:
        proc = subprocess.run(argv, cwd=self.tmp, env={**self.env, **env}, capture_output=True, text=True, timeout=90)
        return Result(proc.returncode, self.root, self.repo, self.log, self.audit_path, proc.stdout, proc.stderr)

    def run(self, branch: str = "task-branch", *bazel_args: str, **env: str) -> Result:
        return self.invoke(self.command(branch, *bazel_args), **env)

    def reap(self, **env: str) -> Result:
        return self.invoke([str(SCRIPT), "--reap"], **env)

    def result(self, returncode: int = 0) -> Result:
        return Result(returncode, self.root, self.repo, self.log, self.audit_path)

    def stub_pids(self) -> list[int]:
        return [int(p) for p in self.pids.read_text().split()] if self.pids.exists() else []

    def kill_stubs(self) -> None:
        for pid in self.stub_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def start(self, **env: str) -> subprocess.Popen[bytes]:
        started = self.tmp / "started"
        proc = subprocess.Popen(
            self.command("task-branch"),
            cwd=self.tmp,
            env={**self.env, "STUB_SLEEP": "60", "STUB_STARTED": str(started), **env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return proc

    def orphan(self, kill_client: bool, kill_server: bool) -> None:
        """SIGKILL the wrapper mid-build so teardown never runs; optionally kill client and server too."""
        proc = self.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            json.loads(m.read_text()).get("server_pid") for m in self.root.glob("*/manifest.json")
        ):
            time.sleep(0.05)
        proc.kill()
        proc.wait(timeout=10)
        client, *server = self.stub_pids()
        targets = ([client] if kill_client else []) + (server if kill_server else [])
        for pid in targets:
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while not all(_pid_gone(p) for p in targets) and time.monotonic() < deadline:
            time.sleep(0.05)


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[Sandbox]:
    box = Sandbox(tmp_path)
    yield box
    box.kill_stubs()


@pytest.fixture(scope="module")
def module() -> ModuleType:
    return _load_module()


# --------------------------------------------------------------- success path


def test_success_exits_with_bazel_exit_code_zero(sandbox: Sandbox) -> None:
    # Arrange: a fresh branch cut from main, stub build succeeds.
    # Act
    result = sandbox.run()
    # Assert
    assert result.returncode == 0


def test_success_removes_per_task_output_root(sandbox: Sandbox) -> None:
    # Arrange: the stub writes a read-only install tree and starts a fake server.
    # Act
    result = sandbox.run()
    # Assert
    assert result.task_children("out") == []


def test_success_removes_clean_pushed_worktree(sandbox: Sandbox) -> None:
    # Arrange: the new branch's HEAD equals origin/main, so it is durable.
    # Act
    result = sandbox.run()
    # Assert
    assert result.worktrees() == 1


def test_success_deletes_branch_the_sandbox_created(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run()
    # Assert
    assert _git(result.repo, "branch", "--list", "task-branch") == ""


def test_success_keeps_preexisting_branch(sandbox: Sandbox) -> None:
    # Arrange
    _git(sandbox.repo, "branch", "mine")
    # Act
    result = sandbox.run("mine")
    # Assert
    assert _git(result.repo, "branch", "--list", "mine").strip() == "mine"


def test_success_writes_removed_audit_line(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run()
    # Assert
    assert result.audit()["outcome"] == "removed"


def test_success_reports_reclaimed_bytes(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run()
    # Assert
    assert int(str(result.audit()["bytes_reclaimed"])) > 0


def test_success_leaves_only_the_shared_cache_in_the_managed_root(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run()
    # Assert
    assert sorted(p.name for p in result.root.iterdir()) == [
        "disk-cache",
        "disk-cache.gate",
        "disk-cache.gc",
        "disk-cache.lock",
    ]


def test_success_stops_the_bazel_server(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    sandbox.run()
    pids = sandbox.stub_pids()
    # Assert
    assert bool(pids) and all(_pid_gone(pid) for pid in pids)


def test_managed_root_is_created_with_mode_0700(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run()
    # Assert
    assert result.root.stat().st_mode & 0o777 == 0o700


def test_default_managed_root_lives_under_home_cache(sandbox: Sandbox) -> None:
    # Arrange
    env = {k: v for k, v in sandbox.env.items() if k != "BAZEL_SANDBOX_ROOT"}
    # Act
    subprocess.run(sandbox.command("task-branch"), cwd=sandbox.tmp, env=env, capture_output=True, timeout=90)
    # Assert
    assert (sandbox.tmp / ".cache" / "bazel-sandboxes").is_dir()


def test_build_uses_per_task_output_user_root_under_managed_root(sandbox: Sandbox) -> None:
    # Arrange
    pattern = re.compile(rf"^--output_user_root={re.escape(str(sandbox.root))}/[0-9a-f]{{16}}/out$")
    # Act
    result = sandbox.run()
    # Assert
    assert pattern.match(result.build_args()[1])


def test_build_uses_the_owned_shared_disk_cache(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run()
    # Assert
    assert f"--disk_cache={sandbox.root}/disk-cache" in result.build_args()


def test_disabled_disk_cache_overrides_any_bazelrc_cache(sandbox: Sandbox) -> None:
    # Arrange: an empty --disk_cache overrides a .bazelrc disk cache.
    # Act
    result = sandbox.run(BAZEL_SANDBOX_DISK_CACHE="off")
    # Assert
    assert "--disk_cache=" in result.build_args()


def test_symlinked_disk_cache_is_refused(sandbox: Sandbox, tmp_path: Path) -> None:
    # Arrange
    elsewhere = tmp_path / "elsewhere-cache"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / "link-cache").symlink_to(elsewhere)
    # Act
    result = sandbox.run(BAZEL_SANDBOX_DISK_CACHE=str(tmp_path / "link-cache"))
    # Assert
    assert result.returncode == 77


def test_build_keeps_repository_cache_inside_the_output_root(sandbox: Sandbox) -> None:
    # Arrange
    pattern = re.compile(rf"^--repository_cache={re.escape(str(sandbox.root))}/[0-9a-f]{{16}}/out/cache/repos/v1$")
    # Act
    result = sandbox.run()
    # Assert
    assert any(pattern.match(arg) for arg in result.build_args())


DEPENDENCY_POLICY_FLAGS = ("--lockfile_mode", "--registry", "--override_module", "--enable_bzlmod", "--noenable_bzlmod")


@pytest.mark.parametrize("command", ["build", "test", "run", "query", "cquery", "fetch", "mod", "info"])
def test_wrapper_never_adds_dependency_policy_flags(sandbox: Sandbox, command: str) -> None:
    # Arrange
    # Act
    result = sandbox.run("task-branch", command, "//...")
    # Assert
    assert not any(arg.startswith(DEPENDENCY_POLICY_FLAGS) for arg in result.build_args())


def test_caller_lockfile_flag_passes_through_unchanged(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run("task-branch", "build", "--lockfile_mode=error", "//...")
    # Assert
    assert [a for a in result.build_args() if a.startswith("--lockfile_mode")] == ["--lockfile_mode=error"]


def test_generated_untracked_lockfile_retains_the_worktree(sandbox: Sandbox) -> None:
    # Arrange: the repository does not track MODULE.bazel.lock, so Bazel creates it.
    _git(sandbox.repo, "rm", "-q", "MODULE.bazel.lock")
    _git(sandbox.repo, "commit", "-q", "-m", "untrack lockfile")
    _git(sandbox.repo, "push", "-q", "origin", "main")
    # Act
    result = sandbox.run()
    # Assert
    assert result.audit()["outcome"] == "retained"


def test_generated_lockfile_is_kept_as_source_in_the_retained_worktree(sandbox: Sandbox) -> None:
    # Arrange
    _git(sandbox.repo, "rm", "-q", "MODULE.bazel.lock")
    _git(sandbox.repo, "commit", "-q", "-m", "untrack lockfile")
    _git(sandbox.repo, "push", "-q", "origin", "main")
    # Act
    result = sandbox.run()
    # Assert
    assert (result.task_children("src")[0] / "MODULE.bazel.lock").exists()


def test_non_build_command_gets_no_symlink_prefix(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run("task-branch", "query", "//...")
    # Assert
    assert "--symlink_prefix=/" not in result.build_args()


def test_bazelisk_is_preferred_over_bazel(sandbox: Sandbox) -> None:
    # Arrange
    _write_exe(sandbox.bin / "bazelisk", STUB_BAZEL)
    # Act
    result = sandbox.run()
    # Assert
    assert result.calls()[0].startswith("bazelisk ")


def test_bundle_makes_unpushed_worktree_removable(sandbox: Sandbox, tmp_path: Path) -> None:
    # Arrange
    bundle = tmp_path / "evidence" / "task.bundle"
    bundle.parent.mkdir()
    # Act
    result = sandbox.run(STUB_ACTION="commit", BAZEL_SANDBOX_BUNDLE=str(bundle))
    # Assert
    assert result.audit()["outcome"] == "removed"


# --------------------------------------------------------------- shared disk cache cap


def _run_counts() -> list[int]:
    rng = random.Random(SEED + 4)
    return sorted({3, *(rng.randrange(3, 7) for _ in range(2))})


def _cache_bytes(root: Path) -> int:
    return sum(_tree_bytes(root / "disk-cache" / sub) for sub in ("cas", "ac") if (root / "disk-cache" / sub).exists())


@pytest.mark.parametrize("runs", _run_counts())
def test_property_successive_runs_keep_disk_cache_under_cap(sandbox: Sandbox, runs: int) -> None:
    # Arrange: each stub build adds 1 MiB to the shared cache; the cap is 2 MiB.
    for index in range(runs - 1):
        sandbox.run(f"task-{index}", BAZEL_SANDBOX_DISK_CACHE_MAX="2M")
    # Act
    result = sandbox.run(f"task-{runs}", BAZEL_SANDBOX_DISK_CACHE_MAX="2M")
    # Assert
    assert _cache_bytes(result.root) <= 2 << 20


@pytest.mark.parametrize("runs", _run_counts())
def test_property_successive_runs_leave_no_task_bytes_behind(sandbox: Sandbox, runs: int) -> None:
    # Arrange: each run also fills a per-task repository cache inside its output root.
    for index in range(runs - 1):
        sandbox.run(f"task-{index}")
    # Act
    result = sandbox.run(f"task-{runs}")
    # Assert
    assert sum(_tree_bytes(p) for p in result.root.iterdir() if p.is_dir() and p.name != "disk-cache") == 0


def _fill_cache(root: Path, sub: str, count: int) -> list[Path]:
    base = root / "disk-cache" / sub / "aa"
    base.mkdir(parents=True, exist_ok=True)
    files = []
    for index in range(count):
        path = base / f"blob{index}"
        path.write_bytes(b"x" * (1 << 20))
        os.utime(path, (1_000_000 + index * 1000, 1_000_000 + index * 1000))
        files.append(path)
    return files


@pytest.fixture
def cached(sandbox: Sandbox) -> Sandbox:
    """A managed root after one run, with its shared cache directory in place."""
    sandbox.run()
    return sandbox


def test_gc_evicts_least_recently_used_entry_first(cached: Sandbox) -> None:
    # Arrange
    oldest, *_ = _fill_cache(cached.root, "cas", 3)
    # Act
    cached.invoke([str(SCRIPT), "--gc"], BAZEL_SANDBOX_DISK_CACHE_MAX="2M")
    # Assert
    assert not oldest.exists()


def test_gc_keeps_most_recently_used_entry(cached: Sandbox) -> None:
    # Arrange
    *_, newest = _fill_cache(cached.root, "ac", 3)
    # Act
    cached.invoke([str(SCRIPT), "--gc"], BAZEL_SANDBOX_DISK_CACHE_MAX="2M")
    # Assert
    assert newest.exists()


def test_gc_reports_bytes_above_and_below_cap(cached: Sandbox) -> None:
    # Arrange
    _fill_cache(cached.root, "cas", 3)
    # Act
    result = cached.invoke([str(SCRIPT), "--gc"], BAZEL_SANDBOX_DISK_CACHE_MAX="2M")
    # Assert
    stats = json.loads(result.stdout)
    assert (stats["bytes_before"] > 2 << 20, stats["bytes_after"] <= 2 << 20) == (True, True)


def test_gc_never_touches_files_outside_cas_and_ac(cached: Sandbox) -> None:
    # Arrange
    stray = cached.root / "disk-cache" / "gc" / "lock"
    stray.parent.mkdir(exist_ok=True)
    stray.write_bytes(b"x" * 4096)
    _fill_cache(cached.root, "cas", 1)
    # Act
    cached.invoke([str(SCRIPT), "--gc"], BAZEL_SANDBOX_DISK_CACHE_MAX="1K")
    # Assert
    assert stray.exists()


def test_deferred_gc_audits_its_actual_exit_code(cached: Sandbox) -> None:
    # Arrange: a running build holds the shared lock, so collection is deferred (exit 75).
    _fill_cache(cached.root, "cas", 2)
    lock = os.open(cached.root / "disk-cache.lock", os.O_RDWR)
    fcntl.flock(lock, fcntl.LOCK_SH)
    try:
        # Act
        result = cached.invoke(
            [str(SCRIPT), "--gc"], BAZEL_SANDBOX_DISK_CACHE_MAX="1M", BAZEL_SANDBOX_GC_LOCK_WAIT="0.5"
        )
    finally:
        os.close(lock)
    # Assert
    assert (result.returncode, result.audit()["exit_code"]) == (75, 75)


def _exit_paths() -> list[tuple[str, tuple[str, ...], dict[str, str]]]:
    rng = random.Random(SEED + 5)
    codes = sorted({0, 1, 37, *rng.sample(range(2, 256), 3)})
    paths: list[tuple[str, tuple[str, ...], dict[str, str]]] = [
        (f"bazel-{code}", ("task-branch",), {"STUB_EXIT": str(code)}) for code in codes
    ]
    paths += [
        ("usage", ("bad\nbranch",), {}),
        ("admission", ("task-branch",), {"FAKE_DF_KB": "1"}),
        ("unsafe-setting", ("task-branch",), {"BAZEL_SANDBOX_MIN_FREE": "0"}),
        ("reap", ("--reap",), {}),
        ("gc", ("--gc",), {}),
        ("gc-with-argument", ("--gc", "extra"), {}),
    ]
    return paths


@pytest.mark.parametrize(("name", "argv", "env"), _exit_paths(), ids=[p[0] for p in _exit_paths()])
def test_property_audit_exit_code_equals_process_exit_code(
    sandbox: Sandbox, name: str, argv: tuple[str, ...], env: dict[str, str]
) -> None:
    # Arrange
    command = sandbox.command(*argv) if argv[0] not in ("--reap", "--gc") else [str(SCRIPT), *argv]
    # Act
    result = sandbox.invoke(command, **env)
    # Assert
    assert result.audit()["exit_code"] == result.returncode


def test_gc_does_not_follow_symlinks_out_of_the_cache(cached: Sandbox, tmp_path: Path) -> None:
    # Arrange
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_bytes(b"x" * 8192)
    cas = cached.root / "disk-cache" / "cas"
    cas.mkdir(exist_ok=True)
    (cas / "linkdir").symlink_to(outside)
    (cas / "linkfile").symlink_to(outside / "precious")
    # Act
    cached.invoke([str(SCRIPT), "--gc"], BAZEL_SANDBOX_DISK_CACHE_MAX="1K")
    # Assert
    assert (outside / "precious").exists()


def test_gc_defers_while_a_build_holds_the_cache(cached: Sandbox) -> None:
    # Arrange: a running build holds the shared lock.
    oldest, *_ = _fill_cache(cached.root, "cas", 2)
    lock = os.open(cached.root / "disk-cache.lock", os.O_RDWR)
    fcntl.flock(lock, fcntl.LOCK_SH)
    try:
        # Act
        cached.invoke([str(SCRIPT), "--gc"], BAZEL_SANDBOX_DISK_CACHE_MAX="1M", BAZEL_SANDBOX_GC_LOCK_WAIT="0.5")
    finally:
        os.close(lock)
    # Assert
    assert oldest.exists()


# --------------------------------------------------------------- retention controls


def test_dirty_worktree_is_retained(sandbox: Sandbox) -> None:
    # Arrange: the stub leaves an untracked file in the worktree.
    # Act
    result = sandbox.run(STUB_ACTION="dirty")
    # Assert
    assert len(result.task_children("src")) == 1


def test_dirty_worktree_keeps_its_source(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="dirty")
    # Assert
    assert (result.task_children("src")[0] / "untracked.txt").read_text() == "scratch\n"


def test_dirty_worktree_audit_outcome_is_retained(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="dirty")
    # Assert
    assert result.audit()["outcome"] == "retained"


def test_dirty_worktree_still_removes_output_root(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="dirty")
    # Assert
    assert result.task_children("out") == []


def test_unpushed_commit_worktree_is_retained(sandbox: Sandbox) -> None:
    # Arrange: the stub commits locally; nothing is pushed and no bundle is configured.
    # Act
    result = sandbox.run(STUB_ACTION="commit")
    # Assert
    assert len(result.task_children("src")) == 1


def test_unpushed_commit_stays_recoverable_from_its_branch(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="commit")
    # Assert
    assert _git(result.repo, "log", "-1", "--format=%s", "task-branch").strip() == "local only"


def test_unpushed_commit_retention_names_the_reason(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="commit")
    # Assert
    assert "not reachable from a freshly fetched origin" in str(result.audit()["reason"])


# --------------------------------------------------------------- disk pressure


def test_low_disk_admission_is_refused(sandbox: Sandbox) -> None:
    # Arrange
    low = str(39 * GIB_KIB)
    # Act
    result = sandbox.run(FAKE_DF_KB=low)
    # Assert
    assert result.returncode == 75


def test_low_disk_admission_never_starts_bazel(sandbox: Sandbox) -> None:
    # Arrange
    low = str(39 * GIB_KIB)
    # Act
    result = sandbox.run(FAKE_DF_KB=low)
    # Assert
    assert result.calls() == []


def test_low_disk_admission_is_audited_as_blocked(sandbox: Sandbox) -> None:
    # Arrange
    low = str(39 * GIB_KIB)
    # Act
    result = sandbox.run(FAKE_DF_KB=low)
    # Assert
    assert result.audit()["outcome"] == "blocked"


def test_watchdog_aborts_build_when_free_space_drops(sandbox: Sandbox) -> None:
    # Arrange: admission sees plenty, every later probe sees 19 GiB.
    df = f"{PLENTY_KIB},{19 * GIB_KIB}"
    # Act
    result = sandbox.run(FAKE_DF_KB=df, STUB_SLEEP="30")
    # Assert
    assert result.returncode == 75 and "watchdog abort" in str(result.audit()["reason"])


def test_watchdog_abort_stops_the_bazel_server(sandbox: Sandbox) -> None:
    # Arrange
    df = f"{PLENTY_KIB},{19 * GIB_KIB}"
    # Act
    sandbox.run(FAKE_DF_KB=df, STUB_SLEEP="30")
    pids = sandbox.stub_pids()
    # Assert
    assert bool(pids) and all(_pid_gone(pid) for pid in pids)


def test_watchdog_abort_removes_output_root(sandbox: Sandbox) -> None:
    # Arrange
    df = f"{PLENTY_KIB},{19 * GIB_KIB}"
    # Act
    result = sandbox.run(FAKE_DF_KB=df, STUB_SLEEP="30")
    # Assert
    assert result.task_children("out") == []


def test_watchdog_aborts_when_free_space_is_unreadable(sandbox: Sandbox) -> None:
    # Arrange: admission reads a number, later probes read garbage.
    df = f"{PLENTY_KIB},unknown"
    # Act
    result = sandbox.run(FAKE_DF_KB=df, STUB_SLEEP="30")
    # Assert
    assert result.returncode == 75 and "watchdog abort" in str(result.audit()["reason"])


# --------------------------------------------------------------- crash, signals, shutdown safety


def test_bazel_crash_still_removes_output_root(sandbox: Sandbox) -> None:
    # Arrange: exit 37 is Bazel's internal-error code.
    # Act
    result = sandbox.run(STUB_EXIT="37")
    # Assert
    assert result.task_children("out") == []


def test_interrupt_is_forwarded_to_the_bazel_client(sandbox: Sandbox) -> None:
    # Arrange: the stub client records SIGINT and exits like an interrupted build.
    proc = sandbox.start(STUB_ACTION="trap-int")
    # Act
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=30)
    # Assert
    assert (sandbox.tmp / "int.mark").read_text() == "int\n"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_still_removes_output_root(sandbox: Sandbox, sig: signal.Signals) -> None:
    # Arrange
    proc = sandbox.start()
    built = sandbox.result().task_children("out") != []
    # Act
    proc.send_signal(sig)
    proc.wait(timeout=30)
    # Assert
    assert (built, sandbox.result().task_children("out")) == (True, [])


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_exit_status_is_128_plus_signal(sandbox: Sandbox, sig: signal.Signals) -> None:
    # Arrange
    proc = sandbox.start()
    # Act
    proc.send_signal(sig)
    returncode = proc.wait(timeout=30)
    # Assert
    assert returncode == 128 + sig


@pytest.mark.parametrize("mode", ["fail", "hang"])
def test_failed_shutdown_terminates_exact_server_before_deleting(sandbox: Sandbox, mode: str) -> None:
    # Arrange
    # Act
    sandbox.run(STUB_SHUTDOWN=mode)
    pids = sandbox.stub_pids()
    # Assert
    assert bool(pids) and all(_pid_gone(pid) for pid in pids)


def test_failed_shutdown_without_provable_server_retains_output_root(sandbox: Sandbox) -> None:
    # Arrange: shutdown fails and there is no server PID to prove terminated.
    # Act
    result = sandbox.run(STUB_SHUTDOWN="fail", STUB_NO_SERVER="1")
    # Assert
    assert len(result.task_children("out")) == 1


def test_failed_shutdown_without_provable_server_is_audited_blocked(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_SHUTDOWN="fail", STUB_NO_SERVER="1")
    # Assert
    assert result.audit()["outcome"] == "blocked"


def test_server_ignoring_sigterm_is_killed(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_SHUTDOWN="fail", STUB_SERVER_IGNORE_TERM="1")
    # Assert
    assert result.task_children("out") == []


def test_unknown_process_holding_output_root_blocks_deletion(sandbox: Sandbox) -> None:
    # Arrange: a non-server process keeps its cwd inside the output root.
    # Act
    result = sandbox.run(STUB_ACTION="holder")
    # Assert
    assert len(result.task_children("out")) == 1


def test_unknown_process_holding_output_root_is_audited_blocked(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="holder")
    # Assert
    assert result.audit()["outcome"] == "blocked"


def test_output_root_swapped_for_symlink_is_not_followed(sandbox: Sandbox, tmp_path: Path) -> None:
    # Arrange: the build replaces its output root with a symlink to outside data.
    # Act
    sandbox.run(STUB_ACTION="symlink-out", STUB_NO_SERVER="1")
    # Assert
    assert (tmp_path / "escape" / "precious").exists()


def test_task_directory_swapped_after_creation_is_refused(sandbox: Sandbox) -> None:
    # Arrange: the task directory is replaced, so its inode no longer matches the lease.
    # Act
    result = sandbox.run(STUB_ACTION="swap-task", STUB_NO_SERVER="1")
    # Assert
    assert "inode changed" in str(result.audit()["reason"])


# --------------------------------------------------------------- reaper


def test_reaper_removes_stale_lease_with_dead_owner_and_server(sandbox: Sandbox) -> None:
    # Arrange
    sandbox.orphan(kill_client=True, kill_server=True)
    # Act
    result = sandbox.reap()
    # Assert
    assert result.task_children("out") == []


def test_reaper_audits_removed_stale_lease(sandbox: Sandbox) -> None:
    # Arrange
    sandbox.orphan(kill_client=True, kill_server=True)
    # Act
    result = sandbox.reap()
    # Assert
    assert result.task_audit()["outcome"] == "removed"


def test_reaper_terminates_abandoned_server_and_removes_output_root(sandbox: Sandbox) -> None:
    # Arrange: the owner and client died; only this output root's own server survives.
    sandbox.orphan(kill_client=True, kill_server=False)
    # Act
    result = sandbox.reap()
    # Assert
    assert result.task_children("out") == []


def test_reaper_retains_orphan_whose_client_still_runs(sandbox: Sandbox) -> None:
    # Arrange: the owner died but its Bazel client still references the output root.
    sandbox.orphan(kill_client=False, kill_server=False)
    # Act
    result = sandbox.reap()
    # Assert
    assert len(result.task_children("out")) == 1


def test_reaper_leaves_server_running_while_client_still_runs(sandbox: Sandbox) -> None:
    # Arrange
    sandbox.orphan(kill_client=False, kill_server=False)
    server = sandbox.stub_pids()[1]
    # Act
    sandbox.reap()
    # Assert
    assert not _pid_gone(server)


def _forge_lease(sandbox: Sandbox, pid: int, uid: int) -> Path:
    """A task directory whose lease lock is free, as if its owner had exited."""
    sandbox.run()
    task = sandbox.root / "0123456789abcdef"
    (task / "out" / "base" / "server").mkdir(parents=True)
    (task / "out" / "base" / "server" / "server.pid.txt").write_text("999999999")
    (task / "lease.lock").touch()
    start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19] if pid else ""
    manifest = {
        "id": task.name,
        "uid": uid,
        "state": "active",
        "branch": "forged",
        "repo": str(sandbox.repo),
        "head": "",
        "worktree": str(task / "src"),
        "output_user_root": str(task / "out"),
        "pid": pid or 999_999_999,
        "pid_start": start,
        "sid": os.getsid(0),
        "task_ino": task.stat().st_ino,
        "out_ino": (task / "out").stat().st_ino,
        "created_branch": False,
        "created": "2000-01-01T00:00:00Z",
    }
    (task / "manifest.json").write_text(json.dumps(manifest))
    return task


def test_reaper_retains_lease_whose_owner_pid_is_alive(sandbox: Sandbox) -> None:
    # Arrange: the lock is free but the recorded owner (same PID and start time) still runs.
    owner = subprocess.Popen(["sleep", "30"])
    task = _forge_lease(sandbox, owner.pid, os.getuid())
    try:
        # Act
        sandbox.reap()
    finally:
        owner.kill()
        owner.wait()
    # Assert
    assert (task / "out").is_dir()


def test_reaper_refuses_lease_owned_by_another_uid(sandbox: Sandbox) -> None:
    # Arrange
    task = _forge_lease(sandbox, 0, os.getuid() + 1)
    # Act
    sandbox.reap()
    # Assert
    assert (task / "out").is_dir()


def test_reaper_resolves_forged_stale_lease_of_our_uid(sandbox: Sandbox) -> None:
    # Arrange: positive control for the two refusals above.
    task = _forge_lease(sandbox, 0, os.getuid())
    # Act
    sandbox.reap()
    # Assert
    assert not task.exists()


def test_reaper_retains_lease_held_by_live_process(sandbox: Sandbox) -> None:
    # Arrange: an old manifest mtime must not matter while the owner runs.
    proc = sandbox.start()
    for manifest in sandbox.root.glob("*/manifest.json"):
        os.utime(manifest, (1, 1))
    try:
        # Act
        result = sandbox.reap()
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    # Assert
    assert "lease held by live owner" in result.stdout


def test_reaper_refuses_unknown_ownership(sandbox: Sandbox) -> None:
    # Arrange: a task directory whose manifest claims a different location.
    sandbox.run()
    stranger = sandbox.root / "0123456789abcdef"
    (stranger / "out").mkdir(parents=True)
    (stranger / "manifest.json").write_text(json.dumps({"id": "other", "state": "active"}))
    # Act
    sandbox.reap()
    # Assert
    assert (stranger / "out").is_dir()


# --------------------------------------------------------------- unsafe roots and usage


def test_symlinked_root_is_refused(sandbox: Sandbox, tmp_path: Path) -> None:
    # Arrange
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    sandbox.root.parent.mkdir()
    sandbox.root.symlink_to(target)
    # Act
    result = sandbox.run()
    # Assert
    assert result.returncode == 77


def test_symlinked_root_target_is_left_untouched(sandbox: Sandbox, tmp_path: Path) -> None:
    # Arrange
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    sandbox.root.parent.mkdir()
    sandbox.root.symlink_to(target)
    # Act
    sandbox.run()
    # Assert
    assert list(target.iterdir()) == []


def test_symlinked_root_refusal_is_audited(sandbox: Sandbox, tmp_path: Path) -> None:
    # Arrange
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    sandbox.root.parent.mkdir()
    sandbox.root.symlink_to(target)
    # Act
    result = sandbox.run()
    # Assert
    assert "refusing symlinked managed root" in str(result.audit()["reason"])


def test_root_with_loose_mode_is_refused(sandbox: Sandbox) -> None:
    # Arrange
    sandbox.root.mkdir(parents=True, mode=0o755)
    sandbox.root.chmod(0o755)
    # Act
    result = sandbox.run()
    # Assert
    assert result.returncode == 77


@pytest.mark.parametrize("flag", ["--output_user_root=/tmp/x", "--output_base=/tmp/x"])
def test_caller_cannot_override_sandbox_output_location(sandbox: Sandbox, flag: str) -> None:
    # Arrange
    # Act
    result = sandbox.run("task-branch", flag, "build", "//...")
    # Assert
    assert result.returncode == 64


def test_usage_error_is_audited_with_control_characters_escaped(sandbox: Sandbox) -> None:
    # Arrange: newline and tab make the branch invalid and must survive JSON encoding.
    branch = "bad\nname\t"
    # Act
    result = sandbox.run(branch)
    # Assert
    assert (result.returncode, result.audit()["branch"]) == (64, branch)


@pytest.mark.parametrize(
    "env",
    [
        {"BAZEL_SANDBOX_ABORT_FREE": "40G"},
        {"BAZEL_SANDBOX_MIN_FREE": "0"},
        {"BAZEL_SANDBOX_WATCH_INTERVAL": "0"},
        {"BAZEL_SANDBOX_FETCH_TIMEOUT": "0"},
        {"BAZEL_SANDBOX_BUNDLE": "relative.bundle"},
    ],
)
def test_unsafe_settings_are_rejected(sandbox: Sandbox, env: dict[str, str]) -> None:
    # Arrange
    # Act
    result = sandbox.run(**env)
    # Assert
    assert result.returncode == 64


# --------------------------------------------------------------- immutable candidate


def _push_from_clone(sandbox: Sandbox, branch: str, message: str) -> str:
    """Push a new commit on `branch` from a separate clone, so the main repo has no fresh tracking ref."""
    clone = sandbox.tmp / f"clone-{message.replace(' ', '-')}"
    if not clone.exists():
        _git(sandbox.tmp, "clone", "-q", str(sandbox.tmp / "origin.git"), str(clone))
    _git(clone, "fetch", "-q", "origin")
    has_branch = _git(clone, "ls-remote", "--heads", "origin", branch).strip()
    _git(clone, "checkout", "-q", "-B", branch, f"origin/{branch}" if has_branch else "origin/main")
    _git(clone, "commit", "-q", "--allow-empty", "-m", message)
    _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return _git(clone, "rev-parse", "HEAD").strip()


def test_candidate_sha_is_built_exactly(sandbox: Sandbox) -> None:
    # Arrange
    sha = _push_from_clone(sandbox, "cand", "candidate")
    head_file = sandbox.tmp / "head.txt"
    # Act
    result = sandbox.run("cand", BAZEL_SANDBOX_COMMIT=sha, STUB_ACTION="record-head", STUB_HEAD=str(head_file))
    # Assert
    assert (result.returncode, head_file.read_text().strip()) == (0, sha)


def test_candidate_sha_mismatch_fails_before_bazel(sandbox: Sandbox) -> None:
    # Arrange
    _push_from_clone(sandbox, "cand", "candidate")
    wrong = _git(sandbox.repo, "rev-parse", "HEAD").strip()
    # Act
    result = sandbox.run("cand", BAZEL_SANDBOX_COMMIT=wrong)
    # Assert
    assert (result.returncode, result.calls()) == (65, [])


def test_absent_candidate_ref_fails_before_bazel(sandbox: Sandbox) -> None:
    # Arrange
    sha = _git(sandbox.repo, "rev-parse", "HEAD").strip()
    # Act
    result = sandbox.run("no-such-branch", BAZEL_SANDBOX_COMMIT=sha)
    # Assert
    assert (result.returncode, result.calls()) == (65, [])


def test_stale_tracking_ref_does_not_hide_a_newer_candidate(sandbox: Sandbox) -> None:
    # Arrange: the repo's origin/cand tracking ref is stale; the candidate is the newer remote tip.
    _push_from_clone(sandbox, "cand", "first")
    _git(sandbox.repo, "fetch", "-q", "origin")
    sha = _push_from_clone(sandbox, "cand", "second")
    # Act
    result = sandbox.run("cand", BAZEL_SANDBOX_COMMIT=sha)
    # Assert
    assert result.returncode == 0


def test_candidate_must_be_a_full_sha(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run("cand", BAZEL_SANDBOX_COMMIT="abc123")
    # Assert
    assert result.returncode == 64


def test_candidate_mismatch_is_audited_as_blocked_setup_failure(sandbox: Sandbox) -> None:
    # Arrange
    _push_from_clone(sandbox, "cand", "candidate")
    wrong = _git(sandbox.repo, "rev-parse", "HEAD").strip()
    # Act
    result = sandbox.run("cand", BAZEL_SANDBOX_COMMIT=wrong)
    # Assert
    assert result.audit()["outcome"] == "blocked"


# --------------------------------------------------------------- deletion gates


def _pushed_local_branch(sandbox: Sandbox, name: str) -> None:
    _git(sandbox.repo, "checkout", "-q", "-b", name)
    _git(sandbox.repo, "commit", "-q", "--allow-empty", "-m", f"work on {name}")
    _git(sandbox.repo, "push", "-q", "origin", name)
    _git(sandbox.repo, "checkout", "-q", "main")


def test_pushed_feature_branch_worktree_is_removed(sandbox: Sandbox) -> None:
    # Arrange: positive control for the stale-ref case below.
    _pushed_local_branch(sandbox, "feat")
    # Act
    result = sandbox.run("feat")
    # Assert
    assert result.audit()["outcome"] == "removed"


def test_stale_tracking_ref_is_not_proof_of_durability(sandbox: Sandbox) -> None:
    # Arrange: origin/feat is still in the repo, but the remote branch was deleted elsewhere.
    _pushed_local_branch(sandbox, "feat")
    clone = sandbox.tmp / "deleter"
    _git(sandbox.tmp, "clone", "-q", str(sandbox.tmp / "origin.git"), str(clone))
    _git(clone, "push", "-q", "origin", "--delete", "feat")
    # Act
    result = sandbox.run("feat")
    # Assert
    assert result.audit()["outcome"] == "retained"


def test_reset_away_commit_in_reflog_retains_the_worktree(sandbox: Sandbox) -> None:
    # Arrange: the stub commits and then resets HEAD back to the pushed base.
    # Act
    result = sandbox.run(STUB_ACTION="reset-away")
    # Assert
    assert result.audit()["outcome"] == "retained"


def test_live_process_in_worktree_blocks_its_removal(sandbox: Sandbox) -> None:
    # Arrange: a process keeps its cwd in the worktree.
    # Act
    result = sandbox.run(STUB_ACTION="wt-holder")
    # Assert
    assert len(result.task_children("src")) == 1


def test_live_process_in_worktree_is_audited_per_resource(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="wt-holder")
    # Assert
    assert result.audit()["resources"] == {"output_root": "removed", "worktree": "blocked"}


def test_registered_worktree_swapped_for_a_symlink_is_refused(sandbox: Sandbox) -> None:
    # Arrange: the registered worktree path now points outside the managed root.
    # Act
    result = sandbox.run(STUB_ACTION="symlink-src")
    # Assert
    assert "worktree containment refused" in str(result.audit()["reason"])


def test_worktree_behind_a_symlink_is_left_in_place(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    sandbox.run(STUB_ACTION="symlink-src")
    # Assert
    assert (sandbox.tmp / "escape" / "src-moved" / "MODULE.bazel").exists()


def test_unreleased_output_root_keeps_the_worktree(sandbox: Sandbox) -> None:
    # Arrange: a process holds the output root, so the worktree is not disposed either.
    # Act
    result = sandbox.run(STUB_ACTION="holder")
    # Assert
    assert len(result.task_children("src")) == 1


# --------------------------------------------------------------- cache paths and collection


@pytest.mark.parametrize(
    "args",
    [
        ("build", "--disk_cache=/elsewhere", "//..."),
        ("build", "--disk_cache", "/elsewhere", "//..."),
        ("build", "--repository_cache=/elsewhere", "//..."),
        ("query", "--output_base=/elsewhere", "//..."),
        ("--install_base=/elsewhere", "build", "//..."),
        ("--server_jvm_out=/elsewhere", "build", "//..."),
        ("--failure_detail_out=/elsewhere", "build", "//..."),
    ],
)
def test_caller_cannot_override_sandbox_cache_or_output_paths(sandbox: Sandbox, args: tuple[str, ...]) -> None:
    # Arrange
    # Act
    result = sandbox.run("task-branch", *args)
    # Assert
    assert result.returncode == 64


def test_arguments_for_the_run_target_are_not_inspected(sandbox: Sandbox) -> None:
    # Arrange: after `--`, arguments belong to the binary, not to Bazel.
    # Act
    result = sandbox.run("task-branch", "run", "//:t", "--", "--disk_cache=/binary-arg")
    # Assert
    assert result.returncode == 0


def test_sandbox_cache_paths_come_after_caller_options(sandbox: Sandbox) -> None:
    # Arrange: a --config expansion cannot override paths that come later on the command line.
    # Act
    args = sandbox.run("task-branch", "build", "--config=ci", "//...").build_args()
    # Assert
    assert args.index(f"--disk_cache={sandbox.root}/disk-cache") > args.index("--config=ci")


def test_sandbox_cache_paths_stay_before_run_target_arguments(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    args = sandbox.run("task-branch", "run", "//:t", "--", "x").build_args()
    # Assert
    assert args.index("--symlink_prefix=/") < args.index("--")


def test_non_build_command_keeps_repository_cache_inside_output_root(sandbox: Sandbox) -> None:
    # Arrange
    pattern = re.compile(rf"^--repository_cache={re.escape(str(sandbox.root))}/[0-9a-f]{{16}}/out/cache/repos/v1$")
    # Act
    result = sandbox.run("task-branch", "query", "//...")
    # Assert
    assert any(pattern.match(arg) for arg in result.build_args())


def test_version_command_gets_no_cache_flags(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run("task-branch", "version")
    # Assert
    assert not any(arg.startswith(("--disk_cache", "--repository_cache")) for arg in result.build_args())


def test_orphaned_lease_blocks_cache_collection(sandbox: Sandbox) -> None:
    # Arrange: a SIGKILLed owner leaves an active lease whose processes may still use the cache.
    sandbox.orphan(kill_client=True, kill_server=True)
    # Act
    result = sandbox.invoke([str(SCRIPT), "--gc"])
    # Assert
    assert "orphaned active lease" in str(result.audit()["reason"])


def test_blocked_teardown_defers_its_own_cache_collection(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="holder")
    cache_audit = result.audit()["cache"]
    # Assert
    assert isinstance(cache_audit, dict) and cache_audit["collected"] is False


def test_collection_runs_under_sustained_parallel_load(sandbox: Sandbox) -> None:
    # Arrange: four workers keep overlapping builds running; the collector must not starve.
    # End-to-end collection under overlapping builds, with a lock wait generous enough to resist host
    # load. It does not prove writer priority; the deterministic gate tests below do.
    env = {
        **sandbox.env,
        "STUB_SLEEP": "1.5",
        "BAZEL_SANDBOX_DISK_CACHE_MAX": "2M",
        "BAZEL_SANDBOX_GC_LOCK_WAIT": "120",
    }

    def worker(index: int) -> None:
        for run_index in range(3):
            branch = f"load-{index}-{run_index}"
            subprocess.run(sandbox.command(branch), cwd=sandbox.tmp, env=env, capture_output=True, timeout=120)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    # Act
    for thread in threads:
        thread.start()
        time.sleep(0.4)
    for thread in threads:
        thread.join()
    # Assert
    lines = [json.loads(line) for line in sandbox.audit_path.read_text().splitlines()]
    assert [bool(line["cache"].get("collected") or line["cache"].get("delegated")) for line in lines] == [True] * 12


def test_successive_load_leaves_disk_cache_under_cap(sandbox: Sandbox) -> None:
    # Arrange: overlapping builds each add 1 MiB; the cap is 2 MiB.
    env = {**sandbox.env, "STUB_SLEEP": "1.0", "BAZEL_SANDBOX_DISK_CACHE_MAX": "2M", "BAZEL_SANDBOX_GC_LOCK_WAIT": "4"}

    def worker(index: int) -> None:
        for run_index in range(2):
            subprocess.run(sandbox.command(f"cap-{index}-{run_index}"), cwd=sandbox.tmp, env=env, capture_output=True)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
        time.sleep(0.3)
    for thread in threads:
        thread.join()
    # Act
    sandbox.invoke([str(SCRIPT), "--gc"], BAZEL_SANDBOX_DISK_CACHE_MAX="2M")
    # Assert
    assert _cache_bytes(sandbox.root) <= 2 << 20


def test_cache_on_another_filesystem_is_checked_at_admission(sandbox: Sandbox) -> None:
    # Arrange
    shm = Path("/dev/shm")
    if not shm.is_dir() or not os.access(shm, os.W_OK):
        pytest.skip("no writable /dev/shm for a second filesystem")
    cache = shm / f"bes-{os.getpid()}-{time.monotonic_ns()}" / "cache"
    try:
        # Act
        result = sandbox.run(BAZEL_SANDBOX_DISK_CACHE=str(cache), FAKE_DF_LOW_MATCH=str(shm))
    finally:
        shutil.rmtree(cache.parent, ignore_errors=True)
    # Assert
    assert result.returncode == 75


def test_collection_error_does_not_replace_bazel_exit_status(cached: Sandbox) -> None:
    # Arrange: an unreadable directory inside the cache makes collection fail.
    blocked = cached.root / "disk-cache" / "cas" / "zz"
    blocked.mkdir(parents=True, exist_ok=True)
    blocked.chmod(0)
    try:
        # Act
        result = cached.run("second")
    finally:
        blocked.chmod(0o700)
    # Assert
    assert result.returncode == 0


# --------------------------------------------------------------- reaper identity and terminal states


def test_manifest_records_the_server_identity_while_running(sandbox: Sandbox) -> None:
    # Arrange
    proc = sandbox.start()
    server = sandbox.stub_pids()[1]
    deadline = time.monotonic() + 5
    manifest: dict[str, object] = {}
    while time.monotonic() < deadline and manifest.get("server_pid") != server:
        for path in sandbox.root.glob("*/manifest.json"):
            manifest = json.loads(path.read_text())
        time.sleep(0.1)
    # Act
    proc.terminate()
    proc.wait(timeout=30)
    # Assert
    assert manifest.get("server_pid") == server


def test_reaper_recovers_server_identity_without_pid_file(sandbox: Sandbox) -> None:
    # Arrange: owner and client die; the server survives and its PID file is gone.
    sandbox.orphan(kill_client=True, kill_server=False)
    server = sandbox.stub_pids()[1]
    for pid_file in sandbox.root.glob("*/out/base/server/server.pid.txt"):
        pid_file.unlink()
    # Act
    sandbox.reap()
    # Assert
    assert _pid_gone(server)


def test_retained_manifest_names_owner_reason_and_next_review(sandbox: Sandbox) -> None:
    # Arrange
    result = sandbox.run(STUB_ACTION="dirty")
    # Act
    manifest = json.loads(next(result.root.glob("*/manifest.json")).read_text())
    # Assert
    assert all(manifest.get(key) for key in ("owner", "reason", "next_action", "next_review"))


def test_reaper_reevaluates_a_blocked_lease(sandbox: Sandbox) -> None:
    # Arrange: teardown was blocked by a holder that has since exited.
    sandbox.run(STUB_ACTION="holder")
    sandbox.kill_stubs()
    time.sleep(0.3)
    # Act
    result = sandbox.reap()
    # Assert
    assert result.task_children("out") == []


def test_reap_summary_reports_retained_work_truthfully(sandbox: Sandbox) -> None:
    # Arrange
    sandbox.run(STUB_ACTION="dirty")
    # Act
    result = sandbox.reap()
    # Assert
    assert result.audit()["outcome"] == "retained"


# --------------------------------------------------------------- audit truthfulness


def test_setup_failure_is_audited_as_blocked(sandbox: Sandbox) -> None:
    # Arrange: an absent branch with a missing base ref makes git worktree add fail.
    # Act
    result = sandbox.run("absent-branch", BAZEL_SANDBOX_BASE="missing-base-ref")
    # Assert
    assert (result.returncode, result.audit()["outcome"]) == (70, "blocked")


def test_checked_out_caller_branch_runs_in_detached_worktree(sandbox: Sandbox) -> None:
    # Arrange: `main` is checked out in the caller's repository worktree.
    # Act
    result = sandbox.run("main")
    # Assert
    assert (result.returncode, result.audit()["outcome"], result.worktrees()) == (0, "removed", 1)


def test_unavailable_audit_log_refuses_before_bazel(sandbox: Sandbox) -> None:
    # Arrange
    blocker = sandbox.tmp / "not-a-dir"
    blocker.write_text("")
    # Act
    result = sandbox.run(BAZEL_SANDBOX_AUDIT=str(blocker / "audit.jsonl"))
    # Assert
    assert (result.returncode, result.calls()) == (70, [])


def test_bazel_killed_by_signal_exits_128_plus_signal(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION="self-kill")
    # Assert
    assert result.returncode == 137


# --------------------------------------------------------------- round-3 corrections (subprocess level)


def test_reaper_resolves_a_blocked_lease_that_never_recorded_a_server(sandbox: Sandbox) -> None:
    # Arrange: shutdown fails and Bazel never wrote a PID file, so the run is blocked.
    sandbox.run(STUB_SHUTDOWN="fail", STUB_NO_SERVER="1")
    # Act
    result = sandbox.reap()
    # Assert
    assert result.task_children("out") == []


def test_collection_resumes_after_reaping_a_serverless_blocked_lease(sandbox: Sandbox) -> None:
    # Arrange
    sandbox.run(STUB_SHUTDOWN="fail", STUB_NO_SERVER="1")
    sandbox.reap()
    # Act
    result = sandbox.invoke([str(SCRIPT), "--gc"])
    # Assert
    assert result.returncode == 0


def test_unreachable_remote_retains_unpushed_work(sandbox: Sandbox) -> None:
    # Arrange: the stub commits locally, then the remote disappears before teardown's fresh fetch.
    # Act
    result = sandbox.run(STUB_ACTION="commit-break-remote")
    # Assert
    assert result.audit()["outcome"] == "retained"


def test_candidate_fetch_failure_exits_75_not_65(sandbox: Sandbox) -> None:
    # Arrange: a fetch that fails (here: the remote is gone) is not proof that the candidate is absent.
    sha = _push_from_clone(sandbox, "cand", "candidate")
    (sandbox.tmp / "origin.git").rename(sandbox.tmp / "origin.git.gone")
    # Act
    result = sandbox.run("cand", BAZEL_SANDBOX_COMMIT=sha)
    # Assert
    assert result.returncode == 75


@pytest.mark.parametrize("action", ["ignored", "skip-worktree", "assume-unchanged"])
def test_ignored_and_hidden_index_state_retain_the_worktree(sandbox: Sandbox, action: str) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_ACTION=action)
    # Assert
    assert result.audit()["outcome"] == "retained"


def test_manifest_only_lease_defers_collection(cached: Sandbox) -> None:
    # Arrange: a task directory whose lease lock is gone is uncertain, not held.
    task = cached.root / "0123456789abcdef"
    task.mkdir(mode=0o700)
    (task / "manifest.json").write_text("{}")
    # Act
    result = cached.invoke([str(SCRIPT), "--gc"])
    # Assert
    assert "lease lock missing" in str(result.audit()["reason"])


# --------------------------------------------------------------- round-3 corrections (in process)


def _inproc_task(module: ModuleType, tmp_path: Path, reaping: bool = True) -> Any:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    env = {"HOME": str(tmp_path), "BAZEL_SANDBOX_ROOT": str(root), "BAZEL_SANDBOX_DISK_CACHE": "off"}
    env["BAZEL_SANDBOX_KILL_WAIT"] = "1"
    settings = module.Settings.from_env(env)
    task = module.Task(settings, Path(os.path.realpath(root)), "b", tmp_path, reaping=reaping, owner_sid=777)
    task.task_dir.mkdir(mode=0o700)
    task.task_ino = task.task_dir.stat().st_ino
    (task.out / "base" / "server").mkdir(parents=True)
    task.out_ino = task.out.stat().st_ino
    return task


def _fake_entry(
    proc: Path, pid: int, session: int, ppid: int = 1, readable_stat: bool = True, readable_cmdline: bool = False
) -> None:
    entry = proc / str(pid)
    (entry / "fd").mkdir(parents=True)
    (entry / "stat").write_text(f"{pid} (fake) S {ppid} {session} {session} " + "0 " * 20 + "\n")
    (entry / "cmdline").write_bytes(b"fake\0")
    if not readable_cmdline:
        (entry / "cmdline").chmod(0)
    if not readable_stat:
        (entry / "stat").chmod(0)


def test_serverless_reap_releases_root_after_a_clean_scan(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: positive control; nothing in /proc references the output root.
    task = _inproc_task(module, tmp_path)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc-empty")
    (tmp_path / "proc-empty").mkdir()
    # Act
    outcome = task.dispose_output_root()
    # Assert
    assert outcome is module.Outcome.REMOVED


def test_serverless_reap_keeps_root_with_an_unknown_holder_in_the_owner_session(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    task = _inproc_task(module, tmp_path)
    _fake_entry(tmp_path / "proc", FAKE_PID, session=777)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    # Act
    outcome = task.dispose_output_root()
    # Assert
    assert outcome is module.Outcome.BLOCKED


def test_unknown_holder_with_unreadable_session_retains_the_root(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    task = _inproc_task(module, tmp_path)
    _fake_entry(tmp_path / "proc", FAKE_PID, session=555, readable_stat=False)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    # Act
    outcome = task.dispose_output_root()
    # Assert
    assert outcome is module.Outcome.BLOCKED


def test_uninspectable_descendant_of_the_server_in_its_own_session_is_unknown(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: like a setsid'd sandboxed action, the child left the server's session but is its child.
    server = FAKE_PID - 1
    _fake_entry(tmp_path / "proc", server, session=server)
    _fake_entry(tmp_path / "proc", FAKE_PID, session=FAKE_PID, ppid=server)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}), frozenset({server}))
    # Assert
    assert FAKE_PID in unknown


def _spawn(argv: list[str], cwd: Path, executable: str | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, executable=executable)


def test_reused_server_pid_is_not_signalled(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: the recorded server PID now names an unrelated process with a different start time.
    task = _inproc_task(module, tmp_path)
    other = _spawn(["sleep", "30"], tmp_path)
    task.server, task.server_start = other.pid, "1"
    try:
        # Act
        task.server_terminated()
        alive = other.poll() is None
    finally:
        other.kill()
        other.wait()
    # Assert
    assert alive


def test_pid_file_naming_an_unrelated_process_is_not_signalled(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: its command line mentions the output root, but it holds nothing there.
    task = _inproc_task(module, tmp_path)
    other = _spawn(["bash", "-c", f'exec -a "tail {task.out}" sleep 30'], tmp_path)
    (task.out / "base" / "server" / "server.pid.txt").write_text(str(other.pid))
    try:
        # Act
        task.shutdown_server()
        alive = other.poll() is None
    finally:
        other.kill()
        other.wait()
    # Assert
    assert alive


def test_record_server_ignores_a_pid_file_naming_an_unrelated_process(module: ModuleType, tmp_path: Path) -> None:
    # Arrange
    task = _inproc_task(module, tmp_path)
    other = _spawn(["bash", "-c", f'exec -a "tail {task.out}" sleep 30'], tmp_path)
    (task.out / "base" / "server" / "server.pid.txt").write_text(str(other.pid))
    try:
        # Act
        task.record_server()
    finally:
        other.kill()
        other.wait()
    # Assert
    assert task.server == 0


def test_record_server_records_the_servers_own_session(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: the server runs in the output base; its session is not its PID, so sid == pid cannot pass.
    task = _inproc_task(module, tmp_path)
    server = _spawn(["sleep", "30"], task.out / "base")
    (task.out / "base" / "server" / "server.pid.txt").write_text(str(server.pid))
    try:
        # Act
        task.record_server()
        expected = os.getsid(server.pid)
    finally:
        server.kill()
        server.wait()
    # Assert
    assert task.server_sid == expected


def test_process_running_a_binary_from_the_root_is_a_holder(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: cwd is /, argv[0] is bare, no open files under the root; only /proc/<pid>/exe points inside it.
    binary = tmp_path / "out" / "bin" / "sleep"
    binary.parent.mkdir(parents=True)
    # Use the system executable and preserve its applet basename for multicall coreutils.
    shutil.copy2(Path("/bin/sleep").resolve(strict=True), binary)
    proc = subprocess.Popen(
        ["sleep", "30"], cwd="/", executable=str(binary), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        if proc.poll() is not None:
            pytest.fail(f"live executable fixture exited {proc.returncode}: {proc.communicate()[1].decode()}")
        if Path(f"/proc/{proc.pid}/exe").resolve() != binary:
            pytest.fail("live executable fixture did not start the copied system binary")
        # Act
        found, _ = module.holders(str(tmp_path / "out"))
        if proc.poll() is not None:
            pytest.fail(
                f"live executable fixture exited during scan {proc.returncode}: {proc.communicate()[1].decode()}"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    # Assert
    assert proc.pid in found


def test_lease_dir_removal_cleans_an_interrupted_manifest_write(module: ModuleType, tmp_path: Path) -> None:
    # Arrange
    task = _inproc_task(module, tmp_path)
    shutil.rmtree(task.out)
    for name in ("manifest.json", "lease.lock", "manifest.json.4242.tmp"):
        (task.task_dir / name).write_text("{}")
    # Act
    module.remove_lease_dir(task.root, task.id, task.task_ino)
    # Assert
    assert not task.task_dir.exists()


def test_teardown_error_still_ends_as_an_audited_blocked_lease(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    task = _inproc_task(module, tmp_path)

    def boom() -> None:
        raise RuntimeError("disk vanished")

    monkeypatch.setattr(task, "dispose_output_root", boom)
    # Act
    outcome = task.safe_teardown()
    # Assert
    assert outcome is module.Outcome.BLOCKED


def test_removal_refuses_to_cross_a_filesystem_boundary(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: pretend out/mnt is a mount point (a different device).
    task = _inproc_task(module, tmp_path)
    (task.out / "mnt").mkdir()
    real_stat = os.stat

    def fake_stat(
        path: int | str | os.PathLike[str], dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        st = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == "mnt":
            fields = list(st)
            fields[2] = st.st_dev + 1
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(module.os, "stat", fake_stat)
    # Act
    raised = pytest.raises(
        module.RefusedError, module.remove_tree, task.root, task.id, "out", task.task_ino, task.out_ino
    )
    # Assert
    assert "filesystem boundary" in str(raised.value)


# --------------------------------------------------------------- round-5 corrections


def _reap_task_line(sandbox: Sandbox) -> dict[str, object]:
    lines = [json.loads(line) for line in sandbox.audit_path.read_text().splitlines()]
    return [line for line in lines if line["mode"] == "reap" and line["id"]][-1]


def test_bundle_does_not_excuse_a_reset_away_commit(sandbox: Sandbox, tmp_path: Path) -> None:
    # Arrange: a bundle of HEAD cannot contain a commit that was reset away (not an ancestor of HEAD).
    bundle = tmp_path / "evidence" / "task.bundle"
    bundle.parent.mkdir()
    # Act
    result = sandbox.run(STUB_ACTION="reset-away", BAZEL_SANDBOX_BUNDLE=str(bundle))
    # Assert
    assert result.audit()["outcome"] == "retained"


def test_serverless_reap_states_its_truthful_reason(sandbox: Sandbox) -> None:
    # Arrange
    sandbox.run(STUB_SHUTDOWN="fail", STUB_NO_SERVER="1")
    # Act
    sandbox.reap()
    # Assert
    assert "no Bazel server was ever recorded" in str(_reap_task_line(sandbox)["reason"])


def test_build_pins_install_base_under_the_output_root(sandbox: Sandbox) -> None:
    # Arrange: the pin is passed on the command line (a .bazelrc startup line is not exercised here).
    pattern = re.compile(rf"^--install_base={re.escape(str(sandbox.root))}/[0-9a-f]{{16}}/out/install$")
    # Act
    result = sandbox.run()
    # Assert
    assert any(pattern.match(arg) for arg in result.build_args())


def test_reaper_retains_a_lease_whose_owner_cannot_be_inspected(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: the owner's /proc entry exists but is unreadable, so the owner is UNKNOWN, not gone.
    task = _inproc_task(module, tmp_path)
    task.owner_pid = FAKE_PID
    task.write_manifest("active")
    data = module.load_manifest(task.task_dir)
    # Its command line is readable and names nothing, so only the owner check can retain the lease.
    _fake_entry(tmp_path / "proc", FAKE_PID, session=777, readable_stat=False, readable_cmdline=True)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    # Act
    with module.AuditLog(tmp_path / "audit.jsonl") as audit:
        outcome = module.reap_claimed(task.settings, task.root, task.task_dir, data, audit)
    # Assert
    assert outcome is module.Outcome.RETAINED


def test_terminate_does_not_kill_a_reused_pid(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: the process ignores TERM, then its PID is reused (new start time) before KILL is due.
    starts = iter(["100", "100", "200"])
    sent: list[int] = []
    monkeypatch.setattr(module, "pid_start", lambda pid: next(starts, "200"))
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: sent.append(sig))
    # Act
    module.terminate(FAKE_PID, 0.2)
    # Assert
    assert sent == [signal.SIGTERM]


def test_failed_manifest_write_leaves_no_temporary_file(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    task = _inproc_task(module, tmp_path)

    def fail_replace(_src: object, _dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    # Act
    with contextlib.suppress(OSError):
        task.write_manifest("active")
    # Assert
    assert list(task.task_dir.glob("manifest.json.*.tmp")) == []


def test_lease_removal_with_a_stray_entry_keeps_the_manifest(module: ModuleType, tmp_path: Path) -> None:
    # Arrange
    task = _inproc_task(module, tmp_path)
    shutil.rmtree(task.out)
    for name in ("manifest.json", "lease.lock", "stray.txt"):
        (task.task_dir / name).write_text("{}")
    # Act
    with contextlib.suppress(module.RefusedError):
        module.remove_lease_dir(task.root, task.id, task.task_ino)
    # Assert
    assert (task.task_dir / "manifest.json").exists()


def test_pid_file_does_not_override_a_verified_recorded_server(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: the recorded server (PID + start time) has exited; the PID file now names another process
    # that happens to hold a handle under the output root (an operator reading a log there).
    task = _inproc_task(module, tmp_path)
    gone = _spawn(["true"], tmp_path)
    gone.wait()
    task.server, task.server_start = gone.pid, "1"
    reader = _spawn(["sleep", "30"], task.out / "base")
    (task.out / "base" / "server" / "server.pid.txt").write_text(str(reader.pid))
    try:
        # Act
        task.shutdown_server()
        alive = reader.poll() is None
    finally:
        reader.kill()
        reader.wait()
    # Assert
    assert alive


# --------------------------------------------------------------- round-6: deterministic cache locking


def _flock(path: Path, mode: int) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, mode)
    return fd


def _wait_until(predicate: object, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.05)
    return False


def _build_started(log: Path) -> bool:
    return log.exists() and any(" shutdown" not in line for line in log.read_text().splitlines())


def test_held_gate_keeps_a_new_build_from_starting_until_released(cached: Sandbox) -> None:
    # Arrange: the test holds <cache>.gate exclusively, as a pending collector does.
    log = cached.tmp / "gate-build.log"
    gate = _flock(cached.root / "disk-cache.gate", fcntl.LOCK_EX)
    env = {**cached.env, "STUB_LOG": str(log)}
    build = subprocess.Popen(cached.command("gate-branch"), cwd=cached.tmp, env=env, stdout=subprocess.DEVNULL)
    try:
        # Act
        started_while_held = _wait_until(lambda: _build_started(log), 2.0)
    finally:
        os.close(gate)
    rc = build.wait(timeout=60)
    # Assert
    assert (started_while_held, rc, _build_started(log)) == (False, 0, True)


def test_pending_collector_blocks_new_builds_until_it_has_collected(cached: Sandbox) -> None:
    # Arrange: a running build (held by the test) keeps the collector waiting with the gate taken.
    lock = _flock(cached.root / "disk-cache.lock", fcntl.LOCK_SH)
    env = {**cached.env, "BAZEL_SANDBOX_GC_LOCK_WAIT": "60"}
    collector = subprocess.Popen([str(SCRIPT), "--gc"], cwd=cached.tmp, env=env, stdout=subprocess.DEVNULL)
    gate_path = cached.root / "disk-cache.gate"

    def gate_taken() -> bool:
        fd = os.open(gate_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    log = cached.tmp / "late-build.log"
    late_env = {**cached.env, "STUB_LOG": str(log)}
    try:
        gate_was_taken = _wait_until(gate_taken, 10.0)
        build = subprocess.Popen(cached.command("late-branch"), cwd=cached.tmp, env=late_env, stdout=subprocess.DEVNULL)
        # Act
        started_while_pending = _wait_until(lambda: _build_started(log), 2.0)
    finally:
        os.close(lock)
    gc_rc, build_rc = collector.wait(timeout=60), build.wait(timeout=60)
    # Assert
    # gate_was_taken is the precondition: a load-induced timeout shows up there, not as 'build started'.
    assert (gate_was_taken, started_while_pending, gc_rc, build_rc) == (True, False, 0, 0)


def test_second_collector_delegates_without_blocking(cached: Sandbox) -> None:
    # Arrange: another collection is running (the test holds <cache>.gc).
    gc_lock = _flock(cached.root / "disk-cache.gc", fcntl.LOCK_EX)
    env = {**cached.env, "BAZEL_SANDBOX_GC_LOCK_WAIT": "60"}
    try:
        # Act
        second = subprocess.Popen([str(SCRIPT), "--gc"], cwd=cached.tmp, env=env, stdout=subprocess.PIPE, text=True)
        try:
            out, _ = second.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            second.kill()
            out = "blocked"
            second.communicate()
    finally:
        os.close(gc_lock)
    # Assert
    assert '"delegated": true' in out


# --------------------------------------------------------------- round-6: pins, scan order, reaper reason


@pytest.mark.parametrize(
    "flag",
    ["--server_jvm_out={out}/base/server/jvm.out", "--failure_detail_out={out}/base/failure_detail.rawproto"],
)
def test_startup_output_pins_follow_the_callers_startup_options(sandbox: Sandbox, flag: str) -> None:
    # Arrange
    caller_option = "--host_jvm_args=-Xmx512m"
    # Act
    args = sandbox.run("task-branch", caller_option, "build", "//...").build_args()
    out = re.search(r"--output_user_root=(\S+)", " ".join(args)).group(1)  # type: ignore[union-attr]
    expected = flag.format(out=out)
    # Assert
    assert expected in args[args.index(caller_option) + 1 :]


def test_lease_removal_checks_every_entry_before_unlinking_the_manifest(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: the directory listing yields the manifest first and the stray entry last.
    task = _inproc_task(module, tmp_path)
    shutil.rmtree(task.out)
    for name in ("manifest.json", "lease.lock", "stray.txt"):
        (task.task_dir / name).write_text("{}")
    real_scandir = os.scandir
    order = {"manifest.json": 0, "lease.lock": 1}
    monkeypatch.setattr(
        module.os, "scandir", lambda fd: iter(sorted(real_scandir(fd), key=lambda e: order.get(e.name, 2)))
    )
    # Act
    with contextlib.suppress(module.RefusedError):
        module.remove_lease_dir(task.root, task.id, task.task_ino)
    # Assert
    assert (task.task_dir / "manifest.json").exists()


def test_reaper_names_its_own_reason_when_a_recorded_server_is_unproven(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: the recorded server's /proc entry is unreadable, so it is not proven gone.
    task = _inproc_task(module, tmp_path)
    task.server, task.server_start = FAKE_PID, "5"
    _fake_entry(tmp_path / "proc", FAKE_PID, session=777, readable_stat=False)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    # Act
    task.dispose_output_root()
    # Assert
    assert any("the reaper cannot run bazel shutdown" in reason for reason in task.reasons)


# --------------------------------------------------------------- E1: exiting processes


def _fake_stat_entry(
    proc: Path, pid: int, flags: int, comm: str = "fake", state: str = "R", session: int = 777, stat_ok: bool = True
) -> Path:
    """A same-UID /proc entry whose fd directories are unreadable (EACCES) and cmdline names nothing."""
    entry = proc / str(pid)
    line = f"{pid} ({comm}) {state} 1 {session} {session} 0 0 {flags} " + "0 " * 17 + "\n"
    for base in (entry, entry / "task" / str(pid)):
        (base / "fd").mkdir(parents=True)
        (base / "stat").write_text(line)
        (base / "fd").chmod(0o300)  # no read permission: listing fails with EACCES, cleanup still works
        if not stat_ok:
            (base / "stat").chmod(0)
    (entry / "cmdline").write_bytes(b"fake\0")
    return entry


def test_exiting_process_with_unreadable_handles_is_not_a_holder(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: in the owner's session, PF_EXITING set, /proc/<pid>/fd refused.
    _fake_stat_entry(tmp_path / "proc", FAKE_PID, flags=0x4)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    monkeypatch.setattr(module, "EXIT_REPROBE_SECONDS", 0.3)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert (found, unknown) == ([], [])


def test_live_process_with_unreadable_handles_stays_unknown(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: not exiting, /proc/<pid>/fd refused.
    _fake_stat_entry(tmp_path / "proc", FAKE_PID, flags=0)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    monkeypatch.setattr(module, "EXIT_REPROBE_SECONDS", 0.3)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert unknown == [FAKE_PID]


def test_unreadable_stat_never_earns_the_exiting_exemption(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: the flags would say exiting, but stat cannot be read.
    _fake_stat_entry(tmp_path / "proc", FAKE_PID, flags=0x4, stat_ok=False)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    monkeypatch.setattr(module, "EXIT_REPROBE_SECONDS", 0.3)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert unknown == [FAKE_PID]


def test_flags_parse_after_the_last_parenthesis(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a command name that imitates the stat layout.
    _fake_stat_entry(tmp_path / "proc", FAKE_PID, flags=0x4, comm="evil) R 1 2 3 4 5 0 (x")
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    # Act
    flags = module.pid_flags(FAKE_PID)
    # Assert
    assert flags == 0x4  # PF_EXITING on Linux; kernel-verified, not taken from the module


def test_process_that_stops_exiting_and_holds_the_root_is_not_ignored(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: first seen exiting with unreadable handles; on re-probe it is a live process holding the root.
    proc = tmp_path / "proc"
    (proc / str(FAKE_PID)).mkdir(parents=True)
    exiting = iter([True, False])
    answers = iter([None, None, True])
    monkeypatch.setattr(module, "PROC", proc)
    monkeypatch.setattr(module, "group_exiting", lambda pid: next(exiting, False))
    monkeypatch.setattr(module, "references", lambda pid, target: next(answers, True))
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module, "related", lambda pid, sessions, ancestors: True)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert FAKE_PID in found + unknown


# --------------------------------------------------------------- round 7: thread groups, vanish, settle, bytes

LEADER_EXITS = """
import ctypes, os, threading, time
held = open(os.path.join(os.environ["HOLD_DIR"], "held.txt"), "w")
threading.Thread(target=time.sleep, args=(60,)).start()
ctypes.CDLL(None).pthread_exit(None)
"""


def _leader_zombie_with_live_thread(tmp_path: Path) -> subprocess.Popen[bytes]:
    """A process whose main thread called pthread_exit while another thread keeps a file open."""
    target = tmp_path / "root"
    target.mkdir(exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", LEADER_EXITS], env={**os.environ, "HOLD_DIR": str(target)})
    stat = Path(f"/proc/{proc.pid}/stat")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and stat.read_text().rsplit(")", 1)[1].split()[0] != "Z":
        time.sleep(0.05)
    return proc


def test_zombie_leader_with_a_live_thread_is_alive(module: ModuleType, tmp_path: Path) -> None:
    # Arrange
    proc = _leader_zombie_with_live_thread(tmp_path)
    try:
        # Act
        state = module.pid_state(proc.pid)
    finally:
        proc.kill()
        proc.wait()
    # Assert
    assert state is module.Probe.ALIVE


def test_zombie_leader_whose_thread_holds_the_root_is_a_holder(module: ModuleType, tmp_path: Path) -> None:
    # Arrange
    proc = _leader_zombie_with_live_thread(tmp_path)
    try:
        # Act
        found, unknown = module.holders(str(tmp_path / "root"))
    finally:
        proc.kill()
        proc.wait()
    # Assert
    assert proc.pid in found


def test_fake_zombie_leader_is_held_through_its_threads(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: leader Z + PF_EXITING with unreadable fds; a live thread has target/held.txt open.
    proc = tmp_path / "proc"
    leader = _fake_stat_entry(proc, FAKE_PID, flags=0x4, state="Z")
    (leader / "cmdline").write_bytes(b"")
    thread = leader / "task" / "4999998"
    (thread / "fd").mkdir(parents=True)
    (thread / "stat").write_text("4999998 (worker) S 1 777 777 0 0 0 " + "0 " * 17 + "\n")
    (thread / "fd" / "3").symlink_to("/some/target/held.txt")
    monkeypatch.setattr(module, "PROC", proc)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert found == [FAKE_PID]


def test_vanished_process_is_skipped_not_unknown(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: the entry exists when the scan lists it and vanishes before its handles are read.
    proc = tmp_path / "proc"
    entry = _fake_stat_entry(proc, FAKE_PID, flags=0)
    real_references = module.references

    def vanish(pid: int, target: str) -> object:
        for path in sorted(entry.rglob("*"), reverse=True):
            path.chmod(0o700)
        shutil.rmtree(entry)
        return real_references(pid, target)

    monkeypatch.setattr(module, "PROC", proc)
    monkeypatch.setattr(module, "references", vanish)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert unknown == []


def test_process_gone_before_it_is_counted_is_skipped(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: settle cannot decide, but the process is gone by the final re-check.
    proc = tmp_path / "proc"
    (proc / str(FAKE_PID)).mkdir(parents=True)
    states = iter([module.Probe.ALIVE, module.Probe.GONE])
    monkeypatch.setattr(module, "PROC", proc)
    monkeypatch.setattr(module, "references", lambda pid, target: None)
    monkeypatch.setattr(module, "pid_state", lambda pid: next(states, module.Probe.GONE))
    monkeypatch.setattr(module, "related", lambda pid, sessions, ancestors: True)
    monkeypatch.setattr(module, "settle", lambda pid, target, deadline: None)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert unknown == []


def test_ancestry_walk_restarts_when_a_parent_vanishes(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: pid's parent 42 exits mid-walk; re-read, pid has been reparented to init.
    stats = iter([["S", "42"] + ["0"] * 18, module.Probe.GONE, ["S", "1"] + ["0"] * 18])
    monkeypatch.setattr(module, "stat_fields", lambda pid, tid=None: next(stats))
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    # Act
    result = module.descends_from(FAKE_PID, frozenset({4242}))
    # Assert
    assert result is False


def test_settle_keeps_unknown_when_a_process_stops_exiting_but_stays_uninspectable(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    exiting = iter([True, False])
    monkeypatch.setattr(module, "EXIT_REPROBE_SECONDS", 0.3)
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module, "references", lambda pid, target: None)
    monkeypatch.setattr(module, "group_exiting", lambda pid: next(exiting, False))
    # Act
    answer = module.settle(FAKE_PID, "/some/target", time.monotonic() + 10)
    # Assert
    assert answer is None


def test_settle_keeps_unknown_when_exiting_cannot_be_read(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setattr(module, "EXIT_REPROBE_SECONDS", 0.3)
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module, "references", lambda pid, target: None)
    monkeypatch.setattr(module, "group_exiting", lambda pid: None)
    # Act
    answer = module.settle(FAKE_PID, "/some/target", time.monotonic() + 10)
    # Assert
    assert answer is None


def test_settle_keeps_re_probing_until_the_process_can_be_inspected(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: two more probes are needed before the handles become readable.
    answers = iter([None, None, True])
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module, "references", lambda pid, target: next(answers, True))
    monkeypatch.setattr(module, "group_exiting", lambda pid: True)
    # Act
    answer = module.settle(FAKE_PID, "/some/target", time.monotonic() + 10)
    # Assert
    assert answer is True


def test_re_probing_is_bounded_by_a_total_budget(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: five processes stuck exiting with unreadable handles.
    for offset in range(5):
        _fake_stat_entry(tmp_path / "proc", FAKE_PID - offset, flags=0x4)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    monkeypatch.setattr(module, "EXIT_REPROBE_SECONDS", 0.5)
    monkeypatch.setattr(module, "HOLDER_SETTLE_BUDGET", 0.6)
    started = time.monotonic()
    # Act
    module.holders("/some/target", frozenset({777}))
    # Assert
    assert time.monotonic() - started < 1.5


def test_non_utf8_command_name_does_not_break_probing(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    entry = tmp_path / "proc" / str(FAKE_PID)
    entry.mkdir(parents=True)
    (entry / "stat").write_bytes(f"{FAKE_PID} (".encode() + b"\xff\xfe" + b") S 1 777 777 0 0 0 " + b"0 " * 17)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    # Act
    state = module.pid_state(FAKE_PID)
    # Assert
    assert state is module.Probe.ALIVE


def _pending_lease(root: Path, age: float) -> Path:
    task = root / "0123456789abcdef"
    task.mkdir(mode=0o700)
    (task / "lease.lock.new").write_text("")
    os.utime(task, (time.time() - age, time.time() - age))
    return task


def test_reaper_removes_an_old_unpublished_lease(cached: Sandbox) -> None:
    # Arrange: the owner died between creating lease.lock.new and publishing it.
    task = _pending_lease(cached.root, 120)
    # Act
    cached.reap()
    # Assert
    assert not task.exists()


def test_reaper_keeps_an_unpublished_lease_whose_lock_is_held(cached: Sandbox) -> None:
    # Arrange
    task = _pending_lease(cached.root, 120)
    lock = _flock(task / "lease.lock.new", fcntl.LOCK_EX)
    try:
        # Act
        cached.reap()
        kept = task.exists()
    finally:
        os.close(lock)
    # Assert
    assert kept


def test_reaper_keeps_a_freshly_created_unpublished_lease(cached: Sandbox) -> None:
    # Arrange: inside the creation window, even with a free lock.
    task = _pending_lease(cached.root, 0)
    # Act
    cached.reap()
    # Assert
    assert task.exists()


# --------------------------------------------------------------- module units


def test_remove_tree_refuses_a_root_with_loose_mode(module: ModuleType, tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "r"
    (root / "t" / "out").mkdir(parents=True)
    root.chmod(0o755)
    ino = (root / "t").stat().st_ino
    # Act
    outcome = pytest.raises(
        module.RefusedError, module.remove_tree, root, "t", "out", ino, (root / "t/out").stat().st_ino
    )
    # Assert
    assert "mode" in str(outcome.value)


def test_holders_finds_process_by_cwd(module: ModuleType, tmp_path: Path) -> None:
    # Arrange
    proc = subprocess.Popen(["sleep", "30"], cwd=tmp_path)
    try:
        # Act
        pids = module.holders(str(tmp_path))[0]
    finally:
        proc.kill()
        proc.wait()
    # Assert
    assert proc.pid in pids


FAKE_PID = 4_999_999


def _fake_proc(tmp_path: Path, session: int, readable_stat: bool = True) -> Path:
    proc = tmp_path / "proc"
    entry = proc / str(FAKE_PID)
    (entry / "fd").mkdir(parents=True)
    (entry / "stat").write_text(f"{FAKE_PID} (fake) S 1 {session} {session} " + "0 " * 20 + "\n")
    (entry / "cmdline").write_bytes(b"fake\0")
    (entry / "cmdline").chmod(0)
    if not readable_stat:
        (entry / "stat").chmod(0)
    return proc


def test_unreadable_proc_entry_is_unknown_not_dead(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(module, "PROC", _fake_proc(tmp_path, 1, readable_stat=False))
    # Act
    state = module.pid_state(FAKE_PID)
    # Assert
    assert state is module.Probe.UNKNOWN


def test_missing_proc_entry_is_gone(module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setattr(module, "PROC", tmp_path / "empty-proc")
    # Act
    state = module.pid_state(FAKE_PID)
    # Assert
    assert state is module.Probe.GONE


def test_uninspectable_process_is_not_proven_terminated(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(module, "PROC", _fake_proc(tmp_path, 1, readable_stat=False))
    # Act
    proven = module.terminate(FAKE_PID, 0.2)
    # Assert
    assert proven is False


def test_uninspectable_process_in_task_session_is_unknown_holder(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(module, "PROC", _fake_proc(tmp_path, 777))
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert unknown == [FAKE_PID]


def test_uninspectable_process_in_unrelated_session_is_ignored(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: positive control for the documented session rule.
    monkeypatch.setattr(module, "PROC", _fake_proc(tmp_path, 555))
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert unknown == []


def test_terminate_proves_a_dead_process(module: ModuleType) -> None:
    # Arrange
    proc = subprocess.Popen(["sleep", "30"])
    # Act
    proven = module.terminate(proc.pid, 1.0) if proc.poll() is None else False
    proc.wait()
    # Assert
    assert proven


# --------------------------------------------------------------- properties (pinned seed)

_rng = random.Random(SEED)
EXIT_CODES = sorted({0, 1, 2, 3, 4, 8, 36, 37, 255, *_rng.sample(range(256), 6)})


@pytest.mark.parametrize("code", EXIT_CODES)
def test_property_exit_code_mirrors_bazel(sandbox: Sandbox, code: int) -> None:
    # Arrange
    # Act
    result = sandbox.run(STUB_EXIT=str(code))
    # Assert
    assert result.returncode == code


def _admission_cases() -> list[tuple[int, int]]:
    rng = random.Random(SEED + 1)
    cases = [(15 * GIB_KIB, 15), (15 * GIB_KIB - 1, 15), (0, 1), (1 * GIB_KIB, 1)]
    cases += [(rng.randrange(0, 40 * GIB_KIB), rng.randrange(1, 40)) for _ in range(6)]
    return cases


@pytest.mark.parametrize(("free_kib", "min_gib"), _admission_cases())
def test_property_admission_refuses_exactly_when_free_is_below_minimum(
    sandbox: Sandbox, free_kib: int, min_gib: int
) -> None:
    # Arrange
    expected_refused = free_kib < min_gib * GIB_KIB
    # Act
    result = sandbox.run(FAKE_DF_KB=str(free_kib), BAZEL_SANDBOX_MIN_FREE=f"{min_gib}G", BAZEL_SANDBOX_ABORT_FREE="1")
    # Assert
    assert (result.returncode == 75) == expected_refused


def _invalid_sizes() -> list[str]:
    rng = random.Random(SEED + 2)
    alphabet = "0123456789KMGTkmgtPB.- _"
    generated = ["".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 6))) for _ in range(40)]
    # An empty value means "use the default", so it is not in the invalid space.
    boundary = ["G", "-1G", "1.5G", "15g", "1P", " 15G", "15G ", "1234567890123G", "0", "0G"]
    valid = re.compile(r"^0*[1-9][0-9]{0,11}[KMGT]?$")
    return sorted({value for value in generated + boundary if value and not valid.fullmatch(value)})[:20]


@pytest.mark.parametrize("value", _invalid_sizes())
def test_property_invalid_size_settings_fail_closed(sandbox: Sandbox, value: str) -> None:
    # Arrange
    # Act
    result = sandbox.run(BAZEL_SANDBOX_MIN_FREE=value)
    # Assert
    assert result.returncode == 64


def _branch_names() -> list[str]:
    rng = random.Random(SEED + 3)
    alphabet = "abcxyz019-_/.\"#%&+=@'ü"
    names = ["".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 12))) for _ in range(60)]
    valid = [
        name
        for name in names
        if subprocess.run([GIT, "check-ref-format", "--branch", name], capture_output=True).returncode == 0
        and name != "main"
    ]
    return sorted(set(valid))[:8]


@pytest.mark.parametrize("branch", _branch_names())
def test_property_audit_line_round_trips_any_valid_branch_name(sandbox: Sandbox, branch: str) -> None:
    # Arrange
    # Act
    result = sandbox.run(branch)
    # Assert
    assert result.audit()["branch"] == branch


# --------------------------------------------------------------- skill document and entry point


def test_skill_links_lifecycle_contract() -> None:
    # Arrange
    text = (SKILL / "SKILL.md").read_text()
    # Act
    linked = "../ephemeral-workspace-lifecycle/SKILL.md" in text
    # Assert
    assert linked


@pytest.mark.parametrize("path", [SCRIPT, MODULE])
def test_entry_points_are_executable(path: Path) -> None:
    # Arrange
    mode = path.stat().st_mode
    # Act
    executable = bool(mode & 0o100)
    # Assert
    assert executable


def test_shell_entry_is_shellcheck_clean() -> None:
    # Arrange
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    # Act
    result = subprocess.run([shellcheck, str(SCRIPT)], capture_output=True, text=True)
    # Assert
    assert result.returncode == 0, result.stdout


# --------------------------------------------------------------- round-8 review hardening tests


def test_group_exiting_rejects_live_non_exiting_worker_thread(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: leader has PF_EXITING (0x4), worker thread has flags=0.
    proc = tmp_path / "proc"
    leader = _fake_stat_entry(proc, FAKE_PID, flags=0x4)
    worker = leader / "task" / str(FAKE_PID + 1)
    (worker / "fd").mkdir(parents=True)
    (worker / "stat").write_text(f"{FAKE_PID + 1} (worker) S 1 777 777 0 0 0 " + "0 " * 17 + "\n")
    monkeypatch.setattr(module, "PROC", proc)
    # Act
    exiting = module.group_exiting(FAKE_PID)
    # Assert
    assert exiting is False


def test_holders_do_not_exempt_non_exiting_nonzero_flags(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: flags=0x400000 (PF_FORKNOEXEC) is non-zero without PF_EXITING (0x4).
    _fake_stat_entry(tmp_path / "proc", FAKE_PID, flags=0x400000)
    monkeypatch.setattr(module, "PROC", tmp_path / "proc")
    monkeypatch.setattr(module, "EXIT_REPROBE_SECONDS", 0.2)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert (found, unknown) == ([], [FAKE_PID])


def test_zombie_leader_with_unreadable_fd_and_clean_worker_is_not_unknown(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: dead Z leader with unreadable fd dir; live worker thread has clean readable fd dir.
    proc = tmp_path / "proc"
    leader = _fake_stat_entry(proc, FAKE_PID, flags=0x4, state="Z")
    worker = leader / "task" / str(FAKE_PID + 1)
    (worker / "fd").mkdir(parents=True)
    (worker / "stat").write_text(f"{FAKE_PID + 1} (worker) S 1 777 777 0 0 0 " + "0 " * 17 + "\n")
    monkeypatch.setattr(module, "PROC", proc)
    # Act
    found, unknown = module.holders("/some/target", frozenset({777}))
    # Assert
    assert (found, unknown) == ([], [])


def test_settle_returns_unknown_when_budget_already_exhausted(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: budget_deadline already elapsed before any re-probe wait.
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module, "references", lambda pid, target: None)
    monkeypatch.setattr(module, "group_exiting", lambda pid: True)
    # Act
    answer = module.settle(FAKE_PID, "/some/target", time.monotonic() - 1.0)
    # Assert
    assert answer is None


def test_watchdog_survives_fewer_than_three_unreadable_df_probes(sandbox: Sandbox) -> None:
    # Arrange: admission succeeds, then two transient unreadable df probes recover on the third probe.
    df = f"{PLENTY_KIB},unknown,unknown,{PLENTY_KIB}"
    # Act
    result = sandbox.run(FAKE_DF_KB=df, BAZEL_SANDBOX_WATCH_INTERVAL="0.1", STUB_SLEEP="0.45")
    # Assert
    assert result.returncode == 0


def test_two_token_startup_option_is_refused(sandbox: Sandbox) -> None:
    # Arrange
    custom_rc = str(sandbox.tmp / "custom.bazelrc")
    # Act
    result = sandbox.run("task-branch", "--bazelrc", custom_rc, "build", "//...")
    # Assert
    assert result.returncode == 64


def test_unary_startup_flag_without_equals_is_allowed(sandbox: Sandbox) -> None:
    # Arrange
    # Act
    result = sandbox.run("task-branch", "--nobatch", "build", "//...")
    # Assert
    assert result.returncode == 0


def test_gc_exits_77_on_unsafe_disk_cache(cached: Sandbox) -> None:
    # Arrange
    cache_dir = cached.root / "disk-cache"
    cache_dir.chmod(0o755)
    # Act
    result = cached.invoke([str(SCRIPT), "--gc"])
    cache_dir.chmod(0o700)
    # Assert
    assert result.returncode == 77


def test_gc_exits_75_when_unlink_fails_above_cap(
    module: ModuleType, cached: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(module, "_unlink_beneath", lambda cfd, parts, dev: False)
    record = module.AuditRecord(mode="gc")
    env = {**cached.env, "BAZEL_SANDBOX_DISK_CACHE_MAX": "1"}
    (cached.root / "disk-cache" / "cas").mkdir(parents=True, exist_ok=True)
    (cached.root / "disk-cache" / "cas" / "aabb").write_bytes(b"x" * 16)
    # Act
    code = module.gc(env, record)
    # Assert
    assert (code, record.cache["collected"]) == (75, False)


def test_reap_claimed_recorded_server_wins_over_pid_file(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: recorded task.server has exited, while server.pid.txt names a different live PID referencing out.
    task = _inproc_task(module, tmp_path)
    gone = _spawn(["true"], tmp_path)
    gone.wait()
    task.server, task.server_start = gone.pid, "1"
    task.write_manifest("terminal-blocked")
    data = module.load_manifest(task.task_dir)
    other = _spawn(["sleep", "30"], task.out / "base")
    (task.out / "base" / "server" / "server.pid.txt").write_text(str(other.pid))
    try:
        # Act
        with module.AuditLog(tmp_path / "audit.jsonl") as audit:
            outcome = module.reap_claimed(task.settings, task.root, task.task_dir, data, audit)
    finally:
        other.kill()
        other.wait()
    # Assert
    assert outcome is module.Outcome.RETAINED


def test_bundle_inside_managed_root_via_parent_traversal_is_refused(sandbox: Sandbox) -> None:
    # Arrange: bundle path escapes a sibling directory back into managed root via '..'.
    sibling = sandbox.tmp / "outside"
    sibling.mkdir()
    traversal = sibling / ".." / sandbox.root.name / "escaped.bundle"
    # Act
    result = sandbox.run(STUB_ACTION="commit", BAZEL_SANDBOX_BUNDLE=str(traversal))
    # Assert
    assert (result.audit()["outcome"], traversal.exists()) == ("retained", False)


# --------------------------------------------------------------- round 9: race-window hardening


def test_thread_references_returns_unknown_when_threads_churn(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: sibling tids churn across every rescan pass so _thread_references must fail closed.
    proc = tmp_path / "proc"
    scans = iter([[FAKE_PID, 101], [FAKE_PID, 102], [FAKE_PID, 103], [FAKE_PID, 104], [FAKE_PID, 105], [FAKE_PID, 106]])
    monkeypatch.setattr(module, "PROC", proc)
    monkeypatch.setattr(module, "_task_ids", lambda pid: next(scans))
    monkeypatch.setattr(module, "_links_answer", lambda base, target: False)
    # Act
    answer = module._thread_references(FAKE_PID, "/some/target")
    # Assert
    assert answer is None


def test_thread_references_rescans_when_new_thread_holds_target(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: pass 1 sees clean exited tid 101; rescan catches sibling tid 102 holding target.
    proc = tmp_path / "proc"
    scans = iter([[101], [102], [102]])
    monkeypatch.setattr(module, "PROC", proc)
    monkeypatch.setattr(module, "_task_ids", lambda pid: next(scans))
    monkeypatch.setattr(module, "_links_answer", lambda base, target: base.name == "102")
    # Act
    answer = module._thread_references(FAKE_PID, "/some/target")
    # Assert
    assert answer is True


def test_settle_requires_two_consecutive_exiting_probes_before_exemption(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: only the final post-sleep probe sees PF_EXITING; pre-sleep probe saw False.
    exiting = iter([False, True])
    ticks = iter([0.0, 0.01, 0.03])
    slept: list[float] = []
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module, "references", lambda pid, target: None)
    monkeypatch.setattr(module, "group_exiting", lambda pid: next(exiting, True))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks, 10.0))
    monkeypatch.setattr(module.time, "sleep", slept.append)
    # Act
    answer = module.settle(FAKE_PID, "/some/target", 0.02)
    # Assert
    assert (len(slept), answer) == (1, None)


def _capture_terminate(recorded: list[tuple[int, str]]) -> Callable[[int, float, str], bool]:
    def _fake(pid: int, _wait: float, expected_start: str = "") -> bool:
        recorded.append((pid, expected_start))
        return True

    return _fake


def test_server_terminated_brackets_unrecorded_pid_with_start_time(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: server_start is empty; verified start time "42" must be forwarded into terminate().
    task = _inproc_task(module, tmp_path)
    task.server, task.server_start = FAKE_PID, ""
    recorded: list[tuple[int, str]] = []
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module, "pid_start", lambda pid: "42")
    monkeypatch.setattr(module, "handle_references", lambda pid, target: True)
    monkeypatch.setattr(module, "terminate", _capture_terminate(recorded))
    # Act
    done = task.server_terminated()
    # Assert
    assert (done, recorded) == (True, [(FAKE_PID, "42")])


def test_server_terminated_detects_pid_reuse_during_handle_references(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: pid_start changes between pre-check and post-check around handle_references.
    task = _inproc_task(module, tmp_path)
    task.server, task.server_start = FAKE_PID, ""
    starts = iter(["42", "99"])
    recorded: list[tuple[int, str]] = []
    monkeypatch.setattr(module, "pid_state", lambda pid: module.Probe.ALIVE)
    monkeypatch.setattr(module, "pid_start", lambda pid: next(starts))
    monkeypatch.setattr(module, "handle_references", lambda pid, target: True)
    monkeypatch.setattr(module, "terminate", _capture_terminate(recorded))
    # Act
    done = task.server_terminated()
    # Assert
    assert (done, recorded) == (True, [])


def test_reap_claimed_adds_pid_file_server_before_holder_scan(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: dead owner, server_pid=0, server.pid.txt names live verified server whose child needs ancestor check.
    task = _inproc_task(module, tmp_path)
    task.owner_pid = FAKE_PID - 1
    (task.out / "base" / "server" / "server.pid.txt").write_text(str(FAKE_PID))
    task.write_manifest("active")
    data = module.load_manifest(task.task_dir)
    if data is None:
        raise AssertionError("expected manifest")
    seen_ancestors: list[frozenset[int]] = []
    monkeypatch.setattr(
        module, "pid_state", lambda pid: module.Probe.GONE if pid == FAKE_PID - 1 else module.Probe.ALIVE
    )
    monkeypatch.setattr(module, "pid_start", lambda pid: "77" if pid == FAKE_PID else "")
    monkeypatch.setattr(module, "pid_session", lambda pid: 999 if pid == FAKE_PID else None)
    monkeypatch.setattr(module, "handle_references", lambda pid, target: pid == FAKE_PID)

    def _fake_holders(target: str, sessions: frozenset[int], ancestors: frozenset[int]) -> tuple[list[int], list[int]]:
        seen_ancestors.append(ancestors)
        return ([FAKE_PID], [FAKE_PID + 1] if FAKE_PID in ancestors else [])

    monkeypatch.setattr(module, "holders", _fake_holders)
    # Act
    with module.AuditLog(tmp_path / "audit.jsonl") as audit:
        outcome = module.reap_claimed(task.settings, task.root, task.task_dir, data, audit)
    # Assert
    assert (outcome, FAKE_PID in seen_ancestors[0]) == (module.Outcome.RETAINED, True)


def test_reap_one_retains_locked_lease_before_manifest_exists(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: live owner renamed lease.lock into place and holds LOCK_EX before writing manifest.json.
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    task_dir = root / "0123456789abcdef"
    task_dir.mkdir(mode=0o700)
    lock_fd = _flock(task_dir / "lease.lock", fcntl.LOCK_EX)
    settings = module.Settings.from_env(
        {"HOME": str(tmp_path), "BAZEL_SANDBOX_ROOT": str(root), "BAZEL_SANDBOX_DISK_CACHE": "off"}
    )
    try:
        # Act
        with module.AuditLog(tmp_path / "audit.jsonl") as audit:
            outcome = module.reap_one(settings, root, task_dir, audit)
    finally:
        os.close(lock_fd)
    # Assert
    assert outcome is module.Outcome.RETAINED


def test_reap_cache_target_rejects_loose_mode_cache(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: unsafe 0o755 cache dir must not get sidecar .gate/.lock files created next to it.
    cache = tmp_path / "disk-cache"
    cache.mkdir(mode=0o755)
    cache.chmod(0o755)
    # Act
    target = module._reap_cache_target(cache)
    # Assert
    assert target is None


def test_reap_one_blocks_on_fifo_lease_lock(module: ModuleType, tmp_path: Path) -> None:
    # Arrange: a non-regular FIFO at lease.lock must be rejected without blocking or locking.
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    task_dir = root / "0123456789abcdef"
    task_dir.mkdir(mode=0o700)
    os.mkfifo(task_dir / "lease.lock", 0o600)
    settings = module.Settings.from_env(
        {"HOME": str(tmp_path), "BAZEL_SANDBOX_ROOT": str(root), "BAZEL_SANDBOX_DISK_CACHE": "off"}
    )
    # Act
    with module.AuditLog(tmp_path / "audit.jsonl") as audit:
        outcome = module.reap_one(settings, root, task_dir, audit)
    # Assert
    assert outcome is module.Outcome.BLOCKED


def test_reset_away_commit_beyond_200_reflog_entries_retains_worktree(sandbox: Sandbox) -> None:
    # Arrange: two published commits conceal an older unpublished commit in a long HEAD reflog.
    _git(sandbox.repo, "commit", "--allow-empty", "-q", "-m", "second published baseline")
    _git(sandbox.repo, "push", "-q", "origin", "main")
    # Act
    result = sandbox.run(STUB_ACTION="long-reflog", STUB_RESET_SHA=str(sandbox.tmp / "reset.sha"))
    # Assert
    assert result.audit()["outcome"] == "retained"


def test_output_directory_replaced_with_dirty_source_is_preserved(sandbox: Sandbox) -> None:
    # Arrange: a same-owner, same-filesystem plain directory replaces the original output root.
    # Act
    result = sandbox.run(STUB_ACTION="swap-output-directory", STUB_NO_SERVER="1")
    # Assert
    assert any(path.exists() for path in result.root.glob("*/out/precious.txt"))


def test_failed_reflog_probe_does_not_authorize_worktree_removal(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a failed probe has no complete history even if the current HEAD is published.
    task = _inproc_task(module, tmp_path)
    task.head = "a" * 40
    monkeypatch.setattr(module, "git", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "probe failed"))
    monkeypatch.setattr(module.Task, "uncovered", lambda self, commits: [])
    # Act
    durable = task.head_is_durable()
    # Assert
    assert not durable


@pytest.mark.parametrize("failed_probe", [False, True])
def test_hidden_modified_source_survives_an_index_probe_failure(sandbox: Sandbox, failed_probe: bool) -> None:
    # Arrange: all other Git operations stay native; only the hidden-index probe can fail.
    if failed_probe:
        _write_exe(
            sandbox.bin / "git",
            f'#!/usr/bin/env bash\nfor arg in "$@"; do [[ $arg != ls-files ]] || exit 128; done\nexec {GIT} "$@"\n',
        )
    # Act
    result = sandbox.run(STUB_ACTION="hidden-dirty")
    files = [path for path in result.root.glob("*/src/MODULE.bazel") if "precious" in path.read_text()]
    # Assert
    assert (result.audit()["outcome"], len(files)) == ("retained", 1)


@pytest.mark.parametrize("recorded_server", [False, True])
@pytest.mark.parametrize("kind", ["fifo", "malformed", "oversized", "symlink"])
def test_public_reaper_refuses_untrusted_pid_file_without_hanging(
    sandbox: Sandbox, recorded_server: bool, kind: str
) -> None:
    # Arrange: a real orphaned lease and native Git; only the build-written PID file is replaced.
    sandbox.orphan(kill_client=True, kill_server=True)
    manifest_path = next(sandbox.root.glob("*/manifest.json"))
    data = json.loads(manifest_path.read_text())
    if not recorded_server:
        data.update(server_pid=0, server_start="", server_sid=0)
        manifest_path.write_text(json.dumps(data))
    pid_file = manifest_path.parent / "out" / "base" / "server" / "server.pid.txt"
    pid_file.unlink()
    if kind == "fifo":
        os.mkfifo(pid_file, 0o600)
    elif kind == "symlink":
        foreign = sandbox.tmp / "foreign-pid"
        foreign.write_text("999999")
        pid_file.symlink_to(foreign)
    else:
        pid_file.write_text("9" * 33 if kind == "oversized" else "not-a-pid")
    # Act: timeout kills/reaps only this disposable CLI if the regression blocks.
    proc = subprocess.run(
        [str(SCRIPT), "--reap"], cwd=sandbox.tmp, env=sandbox.env, capture_output=True, text=True, timeout=5
    )
    result = sandbox.result(proc.returncode)
    state = json.loads(manifest_path.read_text())["state"]
    # Assert: both resources and a terminal audit survive; the global lock is released on exit.
    assert (result.task_audit()["outcome"], state, pid_file.exists()) == ("blocked", "terminal-blocked", True)


@pytest.mark.parametrize("kind", ["regular", "fifo", "oversized"])
def test_public_reaper_bounds_and_validates_manifest_before_reading(sandbox: Sandbox, kind: str) -> None:
    # Arrange: the same valid orphaned lease is removable only with readable, bounded metadata.
    sandbox.orphan(kill_client=True, kill_server=True)
    manifest = next(sandbox.root.glob("*/manifest.json"))
    if kind == "fifo":
        manifest.unlink()
        os.mkfifo(manifest, 0o600)
    elif kind == "oversized":
        with manifest.open("a") as stream:
            stream.write(" " * (2 * 1024 * 1024))
    # Act: the public CLI must return and release its lock even with a FIFO at the manifest path.
    proc = subprocess.run(
        [str(SCRIPT), "--reap"], cwd=sandbox.tmp, env=sandbox.env, capture_output=True, text=True, timeout=5
    )
    result = sandbox.result(proc.returncode)
    # Assert
    assert (result.audit()["outcome"], manifest.parent.exists()) == (
        ("removed", False) if kind == "regular" else ("blocked", True)
    )
