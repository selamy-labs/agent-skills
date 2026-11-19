#!/usr/bin/env python3
"""Run one Bazel invocation in an ephemeral per-task sandbox.

Usage:
  bazel-sandbox <branch> -- [bazel startup options] <command> [args...]
  bazel-sandbox --reap
  bazel-sandbox --gc

Each task gets a linked Git worktree and a private native --output_user_root
under a 0700 managed root, a free-space admission gate and watchdog, a durable
lease, and one terminal teardown that removes only what it can prove is safe.
The only shared state is an optional disk cache owned by this user. Builds hold
it under a shared flock; an LRU collector runs after each build under the
exclusive flock, never while a lease is orphaned or uncertain, and touches only
regular files under the cache's cas/ and ac/. See ../SKILL.md for the contract.

Requires Linux, Python 3.11+, git, df, and bazelisk or bazel.
"""

from __future__ import annotations

import contextlib
import enum
import fcntl
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType

EXIT_USAGE = 64
EXIT_DATAERR = 65
EXIT_SOFTWARE = 70
EXIT_TEMPFAIL = 75
EXIT_NOPERM = 77
BUILD_COMMANDS = frozenset({"build", "test", "run", "coverage", "cquery", "aquery"})
NO_CACHE_COMMANDS = frozenset({"shutdown", "version", "help"})
OWNED_FLAGS = (
    "--output_user_root",
    "--output_base",
    "--install_base",
    "--server_jvm_out",
    "--failure_detail_out",
    "--disk_cache",
    "--repository_cache",
)
TWO_TOKEN_STARTUP_FLAGS = frozenset(
    {
        "--bazelrc",
        "--blazerc",
        "--connect_timeout_secs",
        "--digest_function",
        "--host_jvm_args",
        "--host_jvm_profile",
        "--io_nice_level",
        "--local_startup_timeout_secs",
        "--macos_qos_class",
        "--max_idle_secs",
        "--unix_digest_hash_attribute_name",
        "--windows_style_arg_quoting",
    }
)
FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LEASE_FILES = ("manifest.json", "lease.lock", "lease.lock.new")
ANCESTRY_LIMIT = 64
PF_EXITING = 0x4
EXIT_REPROBE_SECONDS = 1.0
HOLDER_SETTLE_BUDGET = 2.0
PENDING_LEASE_MIN_AGE = 60.0
TASK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
CACHE_SUBDIRS = ("cas", "ac")
SIZE_RE = re.compile(r"^([0-9]{1,15})([KMGT]?)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SIZE_SHIFT = {"": 0, "K": 10, "M": 20, "G": 30, "T": 40}
TICK = 0.1
PROC = Path("/proc")


class Outcome(enum.StrEnum):
    REMOVED = "removed"
    RETAINED = "retained"
    BLOCKED = "blocked"
    ABSENT = "absent"


class Probe(enum.Enum):
    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"


class SandboxError(Exception):
    """A refusal that ends the invocation with a specific exit code."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code


class RefusedError(Exception):
    """A deletion safety check failed; nothing further is removed."""


def now(offset: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def owner_name() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def log(message: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        print(f"bazel-sandbox: {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- settings


def parse_size(name: str, value: str) -> int:
    match = SIZE_RE.fullmatch(value)
    if not match:
        raise SandboxError(EXIT_USAGE, f"invalid {name}")
    count, unit = int(match.group(1)), match.group(2)
    if count == 0 or count > (1 << 62) >> SIZE_SHIFT[unit]:
        raise SandboxError(EXIT_USAGE, f"{name} must be positive and below 4 EiB")
    return count << SIZE_SHIFT[unit]


def parse_seconds(name: str, value: str, maximum: float = 86400) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    if not 0 < seconds < maximum:
        raise SandboxError(EXIT_USAGE, f"{name} must be a positive number of seconds below {maximum:g}")
    return seconds


def audit_path(env: Mapping[str, str]) -> Path:
    if env.get("BAZEL_SANDBOX_AUDIT"):
        return Path(env["BAZEL_SANDBOX_AUDIT"])
    state = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME", "/nonexistent")) / ".local" / "state")
    return Path(state) / "bazel-sandboxes" / "audit.jsonl"


def _absolute(name: str, value: str) -> Path:
    if not value.startswith("/") or any(ord(ch) < 32 for ch in value):
        raise SandboxError(EXIT_USAGE, f"{name} must be an absolute path")
    return Path(value)


def _commit(value: str) -> str:
    if value and not SHA_RE.fullmatch(value):
        raise SandboxError(EXIT_USAGE, "BAZEL_SANDBOX_COMMIT must be a full 40-hex commit SHA")
    return value


@dataclass(frozen=True)
class Settings:
    root: Path
    min_free: int
    abort_free: int
    watch_interval: float
    kill_wait: float
    shutdown_timeout: float
    fetch_timeout: float
    remote: str
    base: str
    bundle: Path | None
    repo_hint: str
    task: str
    commit: str
    disk_cache: Path | None
    disk_cache_max: int
    gc_lock_wait: float
    gc_log: Path | None
    retain_review: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def get(name: str, default: str) -> str:
            return env.get(name) or default

        def seconds(name: str, default: str, maximum: float = 86400) -> float:
            return parse_seconds(name, get(name, default), maximum)

        cache_home = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME", "/nonexistent")) / ".cache")
        bundle = env.get("BAZEL_SANDBOX_BUNDLE")
        root_text = get("BAZEL_SANDBOX_ROOT", f"{cache_home}/bazel-sandboxes")
        cache_text = get("BAZEL_SANDBOX_DISK_CACHE", f"{root_text}/disk-cache")
        gc_log = env.get("BAZEL_SANDBOX_GC_LOG")
        settings = cls(
            root=_absolute("BAZEL_SANDBOX_ROOT", root_text),
            min_free=parse_size("BAZEL_SANDBOX_MIN_FREE", get("BAZEL_SANDBOX_MIN_FREE", "40G")),
            abort_free=parse_size("BAZEL_SANDBOX_ABORT_FREE", get("BAZEL_SANDBOX_ABORT_FREE", "20G")),
            watch_interval=seconds("BAZEL_SANDBOX_WATCH_INTERVAL", "15"),
            kill_wait=seconds("BAZEL_SANDBOX_KILL_WAIT", "10"),
            shutdown_timeout=seconds("BAZEL_SANDBOX_SHUTDOWN_TIMEOUT", "120"),
            fetch_timeout=seconds("BAZEL_SANDBOX_FETCH_TIMEOUT", "60"),
            remote=get("BAZEL_SANDBOX_REMOTE", "origin"),
            base=get("BAZEL_SANDBOX_BASE", "main"),
            bundle=_absolute("BAZEL_SANDBOX_BUNDLE", bundle) if bundle else None,
            repo_hint=get("BAZEL_SANDBOX_REPO", "."),
            task=env.get("BAZEL_SANDBOX_TASK", ""),
            commit=_commit(env.get("BAZEL_SANDBOX_COMMIT", "")),
            disk_cache=None if cache_text == "off" else _absolute("BAZEL_SANDBOX_DISK_CACHE", cache_text),
            disk_cache_max=parse_size("BAZEL_SANDBOX_DISK_CACHE_MAX", get("BAZEL_SANDBOX_DISK_CACHE_MAX", "20G")),
            gc_lock_wait=seconds("BAZEL_SANDBOX_GC_LOCK_WAIT", "60"),
            gc_log=_absolute("BAZEL_SANDBOX_GC_LOG", gc_log) if gc_log else None,
            retain_review=seconds("BAZEL_SANDBOX_RETAIN_REVIEW", "604800", 366 * 86400),
        )
        if settings.abort_free >= settings.min_free:
            raise SandboxError(EXIT_USAGE, "BAZEL_SANDBOX_ABORT_FREE must be below BAZEL_SANDBOX_MIN_FREE")
        return settings


# ---------------------------------------------------------------- audit


@dataclass
class AuditRecord:
    mode: str
    ts: str = ""
    id: str = ""
    branch: str = ""
    head: str = ""
    outcome: str = Outcome.BLOCKED.value
    resources: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    exit_code: int = 0
    bytes_reclaimed: int = 0
    df_before: int | None = None
    df_after: int | None = None
    worktree: str = ""
    output_user_root: str = ""
    server_pid: int = 0
    shutdown_rc: int | None = None
    server_gone: bool | None = None
    cache: dict[str, object] = field(default_factory=dict)

    def add_reason(self, reason: str) -> None:
        self.reason = f"{self.reason}; {reason}" if self.reason else reason


class AuditLog:
    """Append-only JSON Lines sink, opened for the whole invocation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> AuditLog:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
            self.fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            log(f"audit log unavailable: {self.path}: {exc.strerror}")
        return self

    def write(self, record: AuditRecord) -> None:
        record.ts = now()
        if self.fd is None:
            return
        try:
            os.write(self.fd, (json.dumps(asdict(record), separators=(",", ":")) + "\n").encode())
        except OSError as exc:
            log(f"audit write failed: {self.path}: {exc.strerror}")

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)


# ---------------------------------------------------------------- signals


class SignalForwarder:
    """Record INT/TERM/HUP, forward them to the running Bazel client, never raise."""

    def __init__(self) -> None:
        self.received: int | None = None
        self.received_at = 0.0
        self.child: subprocess.Popen[bytes] | None = None
        self.previous: dict[int, object] = {}

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self.received is None:
            self.received, self.received_at = signum, time.monotonic()
        if self.child is not None and self.child.poll() is None:
            self.child.send_signal(signum)

    def __enter__(self) -> SignalForwarder:
        for sig in FORWARDED_SIGNALS:
            self.previous[sig] = signal.signal(sig, self._handle)
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]

    def exit_code(self) -> int | None:
        return None if self.received is None else 128 + self.received


# ---------------------------------------------------------------- host probes


def free_bytes(path: Path) -> int | None:
    """Free bytes on path's filesystem via df, or None when unreadable."""
    try:
        out = subprocess.run(["df", "-Pk", "--", str(path)], capture_output=True, text=True, timeout=30).stdout
        kib = out.splitlines()[1].split()[3]
    except (OSError, IndexError, subprocess.SubprocessError):
        return None
    return int(kib) * 1024 if kib.isdigit() else None


def _proc_bytes(path: Path) -> bytes | Probe:
    """A /proc file's bytes; Probe.GONE when the entry vanished (ENOENT/ESRCH), Probe.UNKNOWN when unreadable."""
    try:
        return path.read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return Probe.GONE
    except OSError:
        return Probe.UNKNOWN


def stat_fields(pid: int, tid: int | None = None) -> list[str] | Probe:
    """Fields of /proc/<pid>/stat (or task/<tid>/stat) after the command name.

    Read as bytes and split after the LAST ')', so a command name with ') R (' or non-UTF-8 bytes can
    neither shift the fields nor raise. Probe.GONE when the entry vanished, Probe.UNKNOWN when unreadable.
    """
    data = _proc_bytes(PROC / str(pid) / ("stat" if tid is None else f"task/{tid}/stat"))
    if isinstance(data, Probe):
        return data
    _, sep, tail = data.rpartition(b")")
    fields = tail.decode("ascii", errors="surrogateescape").split()
    return fields if sep and len(fields) > 6 else Probe.UNKNOWN


def _task_ids(pid: int) -> list[int] | Probe:
    try:
        return [int(t.name) for t in (PROC / str(pid) / "task").iterdir() if t.name.isdigit()]
    except (FileNotFoundError, ProcessLookupError):
        return Probe.GONE
    except OSError:
        return Probe.UNKNOWN


def _task_list_unchanged(pid: int, seen: list[int]) -> bool:
    after = _task_ids(pid)
    return after is Probe.GONE or (isinstance(after, list) and set(after).issubset(seen))


def _group_state_pass(pid: int, tids: list[int]) -> Probe:
    for tid in tids:
        fields = stat_fields(pid, tid)
        if fields is Probe.GONE:
            continue
        if isinstance(fields, Probe):
            return Probe.UNKNOWN
        if fields[0] not in ("Z", "X"):
            return Probe.ALIVE
    return Probe.GONE


def _group_state(pid: int) -> Probe:
    """The leader is a zombie: the thread group is ALIVE while any other thread is not a zombie."""
    for _ in range(3):
        tids = _task_ids(pid)
        if isinstance(tids, Probe):
            return tids
        verdict = _group_state_pass(pid, tids)
        if verdict is not Probe.GONE:
            return verdict
        if _task_list_unchanged(pid, tids):
            return Probe.GONE
    return Probe.UNKNOWN


def pid_state(pid: int) -> Probe:
    """ALIVE, GONE, or UNKNOWN when /proc cannot be read: never guess 'dead'.

    GONE means the entry vanished, or the leader is a zombie and so is every thread of its group. A zombie
    leader whose threads still run (the main thread called pthread_exit) is ALIVE.
    """
    fields = stat_fields(pid)
    if isinstance(fields, Probe):
        return fields
    return _group_state(pid) if fields[0] in ("Z", "X") else Probe.ALIVE


def pid_start(pid: int) -> str:
    """Kernel start time, so a reused PID is not mistaken for the original process."""
    fields = stat_fields(pid)
    return fields[19] if isinstance(fields, list) and len(fields) > 19 else ""


def pid_flags(pid: int) -> int | None:
    """The leader's kernel flags word (field 9 of /proc/<pid>/stat), or None when stat cannot be read."""
    fields = stat_fields(pid)
    return int(fields[6]) if isinstance(fields, list) and fields[6].isdigit() else None


def _group_exiting_pass(pid: int, tids: list[int]) -> bool | None:
    for tid in tids:
        fields = stat_fields(pid, tid)
        if fields is Probe.GONE:
            continue
        if isinstance(fields, Probe) or not fields[6].isdigit():
            return None
        if fields[0] not in ("Z", "X") and not int(fields[6]) & PF_EXITING:
            return False
    return True


def group_exiting(pid: int) -> bool | None:
    """True when every live thread of pid's group has PF_EXITING set; None when that cannot be read."""
    for _ in range(3):
        tids = _task_ids(pid)
        if isinstance(tids, Probe):
            return None
        verdict = _group_exiting_pass(pid, tids)
        if verdict is not True or _task_list_unchanged(pid, tids):
            return verdict
    return None


def _link_references(link: Path, target: str) -> bool | None:
    try:
        dest = os.readlink(link)
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        return None
    return dest == target or dest.startswith(target + "/")


def _links_answer(base: Path, target: str) -> bool | None | Probe:
    """cwd, root, exe and open files of one /proc process or task directory."""
    try:
        links = [base / "cwd", base / "root", base / "exe", *(base / "fd").iterdir()]
    except (FileNotFoundError, ProcessLookupError):
        return Probe.GONE
    except OSError:
        return None
    answers = [_link_references(link, target) for link in links]
    if True in answers:
        return True
    return None if None in answers else False


def _thread_is_dead(pid: int, tid: int) -> bool:
    fields = stat_fields(pid, tid)
    return isinstance(fields, list) and fields[0] in ("Z", "X")


def _thread_references_pass(pid: int, tids: list[int], target: str) -> bool | None:
    unknown = False
    for tid in tids:
        answer = _links_answer(PROC / str(pid) / "task" / str(tid), target)
        if answer is True:
            return True
        if answer is None and not _thread_is_dead(pid, tid):
            unknown = True
    return None if unknown else False


def _thread_references(pid: int, target: str) -> bool | None:
    """The leader's handles are unreadable (e.g. it exited while threads run): ask every live thread."""
    for _ in range(3):
        tids = _task_ids(pid)
        if tids is Probe.GONE:
            return False
        if isinstance(tids, Probe):
            return None
        verdict = _thread_references_pass(pid, tids, target)
        if verdict is not False or _task_list_unchanged(pid, tids):
            return verdict
    return None


def handle_references(pid: int, target: str) -> bool | None:
    """Whether a process of our UID has its cwd, root, executable or an open file under target.

    None when it cannot be told; False for processes of any other UID (root included), which cannot open
    paths inside a 0700 directory we own and whose handles we cannot read. When the leader's handles are
    unreadable, the handles of every live thread of the group are checked.
    """
    base = PROC / str(pid)
    try:
        if base.stat().st_uid != os.getuid():
            return False
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        return None
    answer = _links_answer(base, target)
    if answer is Probe.GONE:
        return False
    if answer is None:
        return _thread_references(pid, target)
    if isinstance(answer, Probe):
        return None
    return answer


def references(pid: int, target: str) -> bool | None:
    """Whether pid references target by command line or by handle (see handle_references); None if unknown.

    The command line is checked for every UID; handles only for our own UID.
    """
    cmdline = _proc_bytes(PROC / str(pid) / "cmdline")
    if cmdline is Probe.GONE:
        return False
    if isinstance(cmdline, bytes) and os.fsencode(target) in cmdline:
        return True
    answer = handle_references(pid, target)
    return None if cmdline is Probe.UNKNOWN and answer is not True else answer


def pid_session(pid: int) -> int | None:
    """Session id from /proc/<pid>/stat, which stays readable even for non-dumpable processes."""
    fields = stat_fields(pid)
    return int(fields[3]) if isinstance(fields, list) and fields[3].isdigit() else None


def _walk_ancestry(pid: int, ancestors: frozenset[int]) -> bool | None | Probe:
    """True/False, None when unreadable, Probe.GONE when a link vanished mid-walk."""
    current = pid
    for _ in range(ANCESTRY_LIMIT):
        fields = stat_fields(current)
        if fields is Probe.GONE:
            return Probe.GONE
        if isinstance(fields, Probe) or not fields[1].isdigit():
            return None
        parent = int(fields[1])
        if parent in ancestors:
            return True
        if parent <= 1:
            return False
        current = parent
    return None


def descends_from(pid: int, ancestors: frozenset[int]) -> bool | None:
    """Whether pid's parent chain reaches one of `ancestors`; None when the chain cannot be read.

    A link that vanishes mid-walk means a parent exited and pid was reparented: the walk restarts.
    """
    for _ in range(3):
        result = _walk_ancestry(pid, ancestors)
        if result is not Probe.GONE:
            return None if isinstance(result, Probe) else result
        if pid_state(pid) is Probe.GONE:
            return False
    return None


def related(pid: int, sessions: frozenset[int], ancestors: frozenset[int]) -> bool:
    """Whether an uninspectable process could belong to the task (fail closed when unsure)."""
    if not sessions and not ancestors:
        return True
    fields = stat_fields(pid)
    if fields is Probe.GONE:
        return False
    if isinstance(fields, Probe) or not fields[3].isdigit() or int(fields[3]) in sessions:
        return True
    return descends_from(pid, ancestors) is not False


def _probe_once(pid: int, target: str) -> tuple[bool, bool | None]:
    if pid_state(pid) is Probe.GONE:
        return True, False
    answer = references(pid, target)
    if answer is not None:
        return True, answer
    return False, group_exiting(pid)


def _settle_timed_out(pid: int, slept: bool, exiting_streak: int) -> bool | None:
    if pid_state(pid) is Probe.GONE:
        return False
    return False if slept and exiting_streak >= 2 else None


def settle(pid: int, target: str, budget_deadline: float) -> bool | None:
    """Re-probe a related process whose references could not be determined.

    Bounded by EXIT_REPROBE_SECONDS per process and by the caller's total budget. Returns False when it is
    gone, or when every live thread stayed exiting across at least one re-probe window; the answer itself
    once it can be inspected; None (UNKNOWN) otherwise, including when budget was already exhausted.
    """
    deadline = min(time.monotonic() + EXIT_REPROBE_SECONDS, budget_deadline)
    slept = False
    exiting_streak = 0
    while True:
        decided, value = _probe_once(pid, target)
        if decided:
            return value
        exiting_streak = exiting_streak + 1 if value is True else 0
        if time.monotonic() >= deadline:
            return _settle_timed_out(pid, slept, exiting_streak)
        slept = True
        time.sleep(TICK / 2)


def _classify_pid_holder(
    pid: int, target: str, sessions: frozenset[int], ancestors: frozenset[int], budget_deadline: float
) -> str:
    answer = references(pid, target)
    if answer is None:
        if pid_state(pid) is Probe.GONE or not related(pid, sessions, ancestors):
            return "skip"
        answer = settle(pid, target, budget_deadline)
    if answer is None and pid_state(pid) is not Probe.GONE:
        return "unknown"
    return "found" if answer else "skip"


def holders(
    target: str, sessions: frozenset[int] = frozenset(), ancestors: frozenset[int] = frozenset()
) -> tuple[list[int], list[int]]:
    """(PIDs referencing target, PIDs whose references could not be determined)."""
    me = os.getpid()
    found: list[int] = []
    unknown: list[int] = []
    budget_deadline = time.monotonic() + HOLDER_SETTLE_BUDGET
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        verdict = _classify_pid_holder(pid, target, sessions, ancestors, budget_deadline)
        if verdict == "unknown":
            unknown.append(pid)
        elif verdict == "found":
            found.append(pid)
    return sorted(found), sorted(unknown)


def _wait_termination(pid: int, start: str, budget: float) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if pid_state(pid) is Probe.GONE:
            return True
        cur = pid_start(pid)
        if cur and cur != start:
            return True
        time.sleep(TICK)
    return False


def terminate(pid: int, wait: float, expected_start: str = "") -> bool:
    """TERM, bounded wait, KILL; True only when the exact pid is proven gone.

    The start time is re-checked before each signal and during waits, so a reused PID is never signalled.
    """
    start = expected_start or pid_start(pid)
    if not start:
        return pid_state(pid) is Probe.GONE
    for sig, budget in ((signal.SIGTERM, wait), (signal.SIGKILL, 5.0)):
        current = pid_start(pid)
        if not current:
            return pid_state(pid) is Probe.GONE
        if current != start:
            return True  # the original process exited and its PID now names another one
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)
        if _wait_termination(pid, start, budget):
            return True
    return pid_state(pid) is Probe.GONE


# ---------------------------------------------------------------- contained removal


def _open_dir(name: str | Path, dir_fd: int | None = None) -> int:
    try:
        return os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RefusedError(f"{name}: not a plain directory ({exc.strerror})") from exc


def _check(fd: int, uid: int, dev: int | None = None, ino: int | None = None) -> os.stat_result:
    st = os.fstat(fd)
    if st.st_uid != uid:
        raise RefusedError("owner mismatch")
    if dev is not None and st.st_dev != dev:
        raise RefusedError("filesystem boundary")
    if ino is not None and st.st_ino != ino:
        raise RefusedError("inode changed since creation")
    return st


@contextlib.contextmanager
def opened_task(root: Path, task_id: str, task_ino: int) -> Iterator[tuple[int, int, int]]:
    """Open root and task dir by descriptor, re-proving canonical path, owner, mode and inode."""
    uid = os.getuid()
    if os.path.realpath(root) != str(root):
        raise RefusedError("managed root is not canonical")
    with contextlib.ExitStack() as stack:
        rfd = _open_dir(root)
        stack.callback(os.close, rfd)
        rst = _check(rfd, uid)
        if stat.S_IMODE(rst.st_mode) != 0o700:
            raise RefusedError("managed root mode is not 0700")
        tfd = _open_dir(task_id, rfd)
        stack.callback(os.close, tfd)
        _check(tfd, uid, rst.st_dev, task_ino)
        yield rfd, tfd, rst.st_dev


def clear_dir(dfd: int, dev: int) -> int:
    """Remove everything below an open directory; return allocated bytes of single-link files unlinked."""
    freed = 0
    uid = os.getuid()
    for entry in os.scandir(dfd):
        st = os.stat(entry.name, dir_fd=dfd, follow_symlinks=False)
        if st.st_dev != dev:
            raise RefusedError(f"filesystem boundary at {entry.name}")
        if not stat.S_ISDIR(st.st_mode):
            os.unlink(entry.name, dir_fd=dfd)
            freed += st.st_blocks * 512 if st.st_nlink == 1 else 0
            continue
        cfd = _open_dir(entry.name, dfd)
        try:
            _check(cfd, uid, dev, st.st_ino)
            os.fchmod(cfd, 0o700)
            freed += clear_dir(cfd, dev)
        finally:
            os.close(cfd)
        os.rmdir(entry.name, dir_fd=dfd)
    return freed


def allocated_bytes(dfd: int, dev: int, strict: bool = False) -> int:
    """Allocated bytes of single-link regular files below an open directory, without following symlinks."""
    total = 0
    for entry in os.scandir(dfd):
        st = os.stat(entry.name, dir_fd=dfd, follow_symlinks=False)
        if st.st_dev != dev:
            if strict:
                raise RefusedError(f"filesystem boundary at {entry.name}")
            continue
        if stat.S_ISDIR(st.st_mode):
            cfd = _open_dir(entry.name, dfd)
            try:
                _check(cfd, os.getuid(), dev, st.st_ino)
                total += allocated_bytes(cfd, dev, strict=strict)
            finally:
                os.close(cfd)
        elif st.st_nlink == 1:
            total += st.st_blocks * 512
    return total


def remove_tree(root: Path, task_id: str, name: str, task_ino: int, resource_ino: int) -> int:
    with opened_task(root, task_id, task_ino) as (_, tfd, dev):
        try:
            nfd = _open_dir(name, tfd)
        except FileNotFoundError:
            return 0
        try:
            _check(nfd, os.getuid(), dev, resource_ino)
            os.fchmod(nfd, 0o700)
            freed = clear_dir(nfd, dev)
        finally:
            os.close(nfd)
        os.rmdir(name, dir_fd=tfd)
        return freed


def measure_child(root: Path, task_id: str, name: str, task_ino: int, strict: bool = False) -> int:
    """Prove <root>/<id>/<name> is a contained plain directory on the root filesystem; return its size."""
    with opened_task(root, task_id, task_ino) as (_, tfd, dev):
        nfd = _open_dir(name, tfd)
        try:
            _check(nfd, os.getuid(), dev)
            return allocated_bytes(nfd, dev, strict=strict)
        finally:
            os.close(nfd)


def _lease_leftover(name: str) -> bool:
    return name in LEASE_FILES or (name.startswith("manifest.json.") and name.endswith(".tmp"))


def remove_lease_dir(root: Path, task_id: str, task_ino: int) -> None:
    """Remove the lease files (including an interrupted manifest write) and the empty task directory."""
    with opened_task(root, task_id, task_ino) as (rfd, tfd, _):
        names = []
        for entry in os.scandir(tfd):
            st = os.stat(entry.name, dir_fd=tfd, follow_symlinks=False)
            if not (_lease_leftover(entry.name) and stat.S_ISREG(st.st_mode)):
                # Checked before any unlink, so a stray entry cannot leave a manifest-only or lock-only lease.
                raise RefusedError(f"task directory holds an unexpected entry: {entry.name}")
            names.append(entry.name)
        for name in names:
            os.unlink(name, dir_fd=tfd)
        os.rmdir(task_id, dir_fd=rfd)


def open_managed_root(root: Path) -> Path:
    if root.is_symlink():
        raise SandboxError(EXIT_NOPERM, f"refusing symlinked managed root: {root}")
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        root.mkdir(mode=0o700)
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SandboxError(EXIT_NOPERM, f"managed root is not a directory owned by {os.getuid()}: {root}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise SandboxError(EXIT_NOPERM, f"managed root must have mode 0700: {root}")
    return Path(os.path.realpath(root))


# ---------------------------------------------------------------- shared disk cache


def open_owned_cache(cache: Path) -> int:
    """Create or open the cache directory: owned by us, mode 0700, not a symlink."""
    with contextlib.suppress(FileExistsError):
        cache.mkdir(mode=0o700, parents=True)
    try:
        fd = _open_dir(cache)
    except RefusedError as exc:
        raise SandboxError(EXIT_NOPERM, f"refusing disk cache: {exc}") from exc
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise SandboxError(EXIT_NOPERM, f"disk cache must be a 0700 directory owned by {os.getuid()}: {cache}")
    return fd


def _lock_file(path: str) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _flock_within(fd: int, mode: int, deadline: float) -> bool:
    """Non-blocking attempts with bounded exponential backoff until deadline."""
    delay = TICK / 2
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 1.0)


@contextlib.contextmanager
def cache_use(cache: Path | None) -> Iterator[None]:
    """Shared use of the cache for one build.

    The gate lock gives a waiting collector priority: a new build passes the gate (shared) before taking
    the cache lock (shared), so once a collector holds the gate, only builds already running drain.
    """
    if cache is None:
        yield
        return
    gate, lock = _lock_file(f"{cache}.gate"), _lock_file(f"{cache}.lock")
    try:
        fcntl.flock(gate, fcntl.LOCK_SH)
        fcntl.flock(lock, fcntl.LOCK_SH)
        fcntl.flock(gate, fcntl.LOCK_UN)
        yield
    finally:
        os.close(lock)
        os.close(gate)


@contextlib.contextmanager
def cache_exclusive(cache: Path, wait: float) -> Iterator[bool]:
    """Exclusive collector access: gate first (stops new builds), then the cache lock, both bounded."""
    gate, lock = _lock_file(f"{cache}.gate"), _lock_file(f"{cache}.lock")
    try:
        deadline = time.monotonic() + wait
        yield _flock_within(gate, fcntl.LOCK_EX, deadline) and _flock_within(lock, fcntl.LOCK_EX, deadline)
    finally:
        os.close(lock)
        os.close(gate)


def _lease_uncertainty(task_dir: Path) -> str | None:
    if not is_lease_dir(task_dir):
        return None
    state = lease_state(task_dir)
    if state == "held":
        return None
    if state != "free":
        return f"{task_dir.name}: lease lock {state}"
    data = load_manifest(task_dir)
    if data is None:
        return f"{task_dir.name}: unreadable lease"
    if data.get("state") == "active":
        return f"{task_dir.name}: orphaned active lease"
    if os.path.lexists(task_dir / "out"):
        return f"{task_dir.name}: {data.get('state')} with its output root still present"
    return None


def uncertain_leases(root: Path) -> list[str]:
    """Leases whose processes might still use the cache: orphaned active leases and retained output roots."""
    reasons = (_lease_uncertainty(task_dir) for task_dir in sorted(root.iterdir()))
    return [reason for reason in reasons if reason is not None]


def is_lease_dir(path: Path) -> bool:
    """A task directory: a real directory holding a lease lock or a manifest (foreign ones included)."""
    if not path.is_dir() or path.is_symlink():
        return False
    return os.path.lexists(path / "lease.lock") or os.path.lexists(path / "manifest.json")


def lease_state(task_dir: Path) -> str:
    """'held' (a live owner), 'free', 'missing' or 'unprobeable'."""
    try:
        fd = os.open(task_dir / "lease.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unprobeable"
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return "free"
    except BlockingIOError:
        return "held"
    except OSError:
        return "unprobeable"
    finally:
        os.close(fd)


CacheEntry = tuple[float, int, tuple[str, ...]]


def _collect(dfd: int, prefix: tuple[str, ...], dev: int, found: list[CacheEntry]) -> None:
    for entry in os.scandir(dfd):
        st = os.stat(entry.name, dir_fd=dfd, follow_symlinks=False)
        if st.st_dev != dev or st.st_uid != os.getuid():
            continue
        if stat.S_ISREG(st.st_mode):
            found.append((max(st.st_atime, st.st_mtime), st.st_size, (*prefix, entry.name)))
        elif stat.S_ISDIR(st.st_mode):
            cfd = _open_dir(entry.name, dfd)
            try:
                _collect(cfd, (*prefix, entry.name), dev, found)
            finally:
                os.close(cfd)


def _unlink_beneath(base_fd: int, parts: tuple[str, ...], dev: int) -> bool:
    fds: list[int] = []
    uid = os.getuid()
    try:
        dfd = base_fd
        for part in parts[:-1]:
            dfd = _open_dir(part, dfd)
            fds.append(dfd)
            _check(dfd, uid, dev)
        st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
        if st.st_dev != dev or st.st_uid != uid or not stat.S_ISREG(st.st_mode):
            return False
        os.unlink(parts[-1], dir_fd=dfd)
        return True
    except (OSError, RefusedError):
        return False
    finally:
        for fd in fds:
            os.close(fd)


def collect_cache(settings: Settings, root: Path) -> dict[str, object]:
    """LRU-evict regular files under cas/ and ac/ until their total apparent size is at most the cap.

    Age is max(atime, mtime), which Bazel refreshes on cache hits. Runs only with the gate and the cache
    lock held exclusively and no orphaned or uncertain lease; otherwise the collection is deferred.
    Errors are reported in the result, never raised, so they cannot replace Bazel's exit status.
    """
    cache = settings.disk_cache
    if cache is None:
        return {"enabled": False}
    try:
        return _collect_locked(settings, root, cache)
    except (OSError, RefusedError, SandboxError) as exc:
        return {"enabled": True, "collected": False, "reason": f"collection failed: {exc}"}


def _collect_locked(settings: Settings, root: Path, cache: Path) -> dict[str, object]:
    os.close(open_owned_cache(cache))
    collector = _lock_file(f"{cache}.gc")
    try:
        try:
            fcntl.flock(collector, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A running collector scans only after this finished build released its shared lock, so if it
            # completes it covers this build's entries; if it defers, the next run or --gc collects.
            return {
                "enabled": True,
                "collected": False,
                "delegated": True,
                "reason": "delegated to a running collection (unverified: if it defers, a later run collects)",
            }
        return _collect_exclusive(settings, root, cache)
    finally:
        os.close(collector)


def _collect_exclusive(settings: Settings, root: Path, cache: Path) -> dict[str, object]:
    with cache_exclusive(cache, settings.gc_lock_wait) as locked:
        if not locked:
            return {"enabled": True, "collected": False, "reason": "deferred: cache in use by running builds"}
        uncertain = uncertain_leases(root)
        if uncertain:
            return {"enabled": True, "collected": False, "reason": "deferred: " + "; ".join(uncertain)}
        cfd = open_owned_cache(cache)
        try:
            dev = os.fstat(cfd).st_dev
            found: list[CacheEntry] = []
            for sub in CACHE_SUBDIRS:
                with contextlib.suppress(FileNotFoundError):
                    sfd = _open_dir(sub, cfd)
                    try:
                        _collect(sfd, (sub,), dev, found)
                    finally:
                        os.close(sfd)
            return _evict(cfd, dev, sorted(found), settings)
        finally:
            os.close(cfd)


def _write_gc_log(gc_log_path: Path | None, evicted: list[dict[str, object]]) -> None:
    if gc_log_path is None or not evicted:
        return
    with contextlib.suppress(OSError), gc_log_path.open("a") as gc_log:
        for item in evicted:
            gc_log.write(json.dumps(item) + "\n")


def _evict(cfd: int, dev: int, entries: list[CacheEntry], settings: Settings) -> dict[str, object]:
    before = total = sum(size for _, size, _ in entries)
    evicted: list[dict[str, object]] = []
    for age, size, parts in entries:
        if total <= settings.disk_cache_max:
            break
        if _unlink_beneath(cfd, parts, dev):
            total -= size
            evicted.append({"entry": "/".join(parts), "bytes": size, "age": age})
    _write_gc_log(settings.gc_log, evicted)
    under_cap = total <= settings.disk_cache_max
    result: dict[str, object] = {
        "enabled": True,
        "collected": under_cap,
        "cap_bytes": settings.disk_cache_max,
        "bytes_before": before,
        "bytes_after": total,
        "entries_before": len(entries),
        "evicted_entries": len(evicted),
        "evicted_bytes": before - total,
    }
    if not under_cap:
        result["reason"] = f"cache remains above cap ({total} > {settings.disk_cache_max} bytes) after failed unlinks"
    return result


# ---------------------------------------------------------------- git


def git(cwd: Path, *args: str, probe: bool = True, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run git; probes disable system/global config, fsmonitor and hooks."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    prefix = ["git", "-c", "core.hooksPath=/dev/null"]
    if probe:
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        prefix += ["-c", "core.fsmonitor=false"]
    try:
        return subprocess.run(
            [*prefix, "-C", str(cwd), *args], env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timed out")


def git_ok(cwd: Path, *args: str, **kwargs: object) -> bool:
    return git(cwd, *args, **kwargs).returncode == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------- task lifecycle


@dataclass
class Task:
    settings: Settings
    root: Path
    branch: str
    repo: Path
    startup: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    head: str = ""
    task_ino: int = 0
    out_ino: int = 0
    created_branch: bool = False
    owner_pid: int = field(default_factory=os.getpid)
    owner_start: str = ""
    owner_sid: int = 0
    created: str = field(default_factory=now)
    bazel: str | None = None
    child: subprocess.Popen[bytes] | None = None
    setup_failed: bool = False
    reasons: list[str] = field(default_factory=list)
    resources: dict[str, str] = field(default_factory=dict)
    bytes_reclaimed: int = 0
    server: int = 0
    server_start: str = ""
    server_sid: int = 0
    reaping: bool = False
    shutdown_rc: int | None = None
    server_gone: bool | None = None
    next_review: str = ""

    @property
    def task_dir(self) -> Path:
        return self.root / self.id

    @property
    def worktree(self) -> Path:
        return self.task_dir / "src"

    @property
    def out(self) -> Path:
        return self.task_dir / "out"

    def write_manifest(self, state: str) -> None:
        terminal = state.startswith("terminal-")
        if terminal and not self.next_review:
            self.next_review = now(self.settings.retain_review)
        data = {
            "id": self.id,
            "uid": os.getuid(),
            "owner": owner_name(),
            "task": self.settings.task,
            "state": state,
            "reason": "; ".join(self.reasons),
            "next_action": NEXT_ACTIONS.get(state, ""),
            "next_review": self.next_review if terminal else "",
            "resources": self.resources,
            "repo": str(self.repo),
            "branch": self.branch,
            "head": self.head,
            "worktree": str(self.worktree),
            "output_user_root": str(self.out),
            "pid": self.owner_pid,
            "pid_start": self.owner_start,
            "sid": self.owner_sid,
            "server_pid": self.server,
            "server_start": self.server_start,
            "server_sid": self.server_sid,
            "task_ino": self.task_ino,
            "out_ino": self.out_ino,
            "created_branch": self.created_branch,
            "created": self.created,
            "updated": now(),
        }
        tmp = self.task_dir / f"manifest.json.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                os.write(fd, (json.dumps(data, indent=1) + "\n").encode())
            finally:
                os.close(fd)
            os.replace(tmp, self.task_dir / "manifest.json")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ---- setup

    def fetch_ref(self, name: str) -> tuple[str | None, str | None]:
        """Fetch refs/heads/<name> explicitly into a private ref (no tracking refs).

        Returns (sha, None) when fetched, (None, None) when the remote has no such branch, and
        (None, reason) when the fetch itself failed or timed out.
        """
        private = f"refs/bazel-sandbox/{self.id}/{name}"
        spec = f"+refs/heads/{name}:{private}"
        timeout = self.settings.fetch_timeout
        done = git(self.repo, "fetch", "--quiet", "--no-tags", self.settings.remote, spec, probe=False, timeout=timeout)
        sha = ""
        if done.returncode == 0:
            sha = git(self.repo, "rev-parse", "--verify", "--quiet", f"{private}^{{commit}}").stdout.strip()
        git(self.repo, "update-ref", "-d", private)
        if sha:
            return sha, None
        if done.returncode != 0 and "couldn't find remote ref" not in done.stderr.lower():
            detail = done.stderr.strip().splitlines()[-1:] or ["no output"]
            return None, f"fetch of {name} from {self.settings.remote} failed (rc {done.returncode}): {detail[0][:200]}"
        return None, None

    def add_worktree(self) -> None:
        expected = self.settings.commit
        fetched, error = self.fetch_ref(self.branch)
        if expected and error:
            raise SandboxError(EXIT_TEMPFAIL, error)
        if error:
            log(f"{error}; using local refs")
        if expected and fetched is None:
            raise SandboxError(EXIT_DATAERR, f"candidate refs/heads/{self.branch} is absent on {self.settings.remote}")
        if expected and fetched != expected:
            raise SandboxError(
                EXIT_DATAERR,
                f"candidate mismatch: {self.settings.remote} {self.branch} is {fetched}, expected {expected}",
            )
        self.git_worktree_add(self.worktree_args(expected, fetched))
        self.head = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        if expected and self.head != expected:
            raise SandboxError(EXIT_DATAERR, f"worktree HEAD {self.head} is not the candidate {expected}")
        self.write_manifest("active")

    def worktree_args(self, expected: str, fetched: str | None) -> list[str]:
        if expected:
            return ["--detach", str(self.worktree), expected]
        if git_ok(self.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{self.branch}"):
            return ["--detach", str(self.worktree), f"refs/heads/{self.branch}"]
        self.created_branch = True
        if fetched:
            return ["--no-track", "-b", self.branch, str(self.worktree), fetched]
        start = self.fetch_ref(self.settings.base)[0] or self.settings.base
        log(f"branch {self.branch} is absent locally and on {self.settings.remote}; creating it from {start}")
        return ["--no-track", "-b", self.branch, str(self.worktree), start]

    def git_worktree_add(self, args: list[str]) -> None:
        added = git(self.repo, "worktree", "add", *args, probe=False)
        if added.returncode != 0:
            self.created_branch = False
            self.setup_failed = True
            raise SandboxError(EXIT_SOFTWARE, f"git worktree add failed: {added.stderr.strip()[:200]}")

    def startup_argv(self) -> list[str]:
        """Bazel plus startup options pinning every output path under the output root.

        Command-line startup options override `.bazelrc` `startup` lines, and the caller may not set these.
        """
        assert self.bazel is not None
        out = self.out
        return [
            self.bazel,
            f"--output_user_root={out}",
            f"--output_base={out}/base",
            *self.startup,
            f"--install_base={out}/install",
            f"--server_jvm_out={out}/base/server/jvm.out",
            f"--failure_detail_out={out}/base/failure_detail.rawproto",
        ]

    def bazel_argv(self, command: str, args: list[str]) -> list[str]:
        """Pinned output paths; sandbox-owned cache paths go after the caller's options so nothing overrides them."""
        argv = [*self.startup_argv(), command]
        split = args.index("--") if "--" in args else len(args)
        owned: list[str] = []
        if command not in NO_CACHE_COMMANDS:
            owned += [f"--disk_cache={self.settings.disk_cache or ''}", f"--repository_cache={self.out}/cache/repos/v1"]
        if command in BUILD_COMMANDS:
            owned.append("--symlink_prefix=/")
        return [*argv, *args[:split], *owned, *args[split:]]

    # ---- supervise

    def prepare_output_root(self) -> None:
        """Bind the output directory incarnation before any build-controlled code runs."""
        with opened_task(self.root, self.id, self.task_ino) as (_, tfd, dev):
            os.mkdir("out", mode=0o700, dir_fd=tfd)
            fd = _open_dir("out", tfd)
            try:
                self.out_ino = _check(fd, os.getuid(), dev).st_ino
            finally:
                os.close(fd)
        self.write_manifest("active")

    def verify_output_root(self) -> None:
        if not self.out_ino:
            raise RefusedError("original output root inode is not recorded; retained")
        with opened_task(self.root, self.id, self.task_ino) as (_, tfd, dev):
            fd = _open_dir("out", tfd)
            try:
                _check(fd, os.getuid(), dev, self.out_ino)
            finally:
                os.close(fd)

    def run_bazel(self, command: str, args: list[str], signals: SignalForwarder) -> int:
        self.bazel = shutil.which("bazelisk") or shutil.which("bazel")
        if self.bazel is None:
            raise SandboxError(EXIT_SOFTWARE, "neither bazelisk nor bazel is on PATH")
        if Path(self.bazel).name == "bazel" and (self.worktree / ".bazelversion").exists():
            log("bazelisk not found; .bazelversion is not enforced by plain bazel")
        self.prepare_output_root()
        self.child = subprocess.Popen(self.bazel_argv(command, args), cwd=self.worktree)
        signals.child = self.child
        if signals.received is not None:
            self.child.send_signal(signals.received)
        rc = self.supervise(signals)
        return 128 - rc if rc < 0 else rc

    def _verify_server_pid(self, pid: int) -> tuple[int, str, int] | None:
        start = pid_start(pid)
        if not start or handle_references(pid, str(self.out)) is not True:
            return None
        sid = pid_session(pid) or 0
        return (pid, start, sid) if pid_start(pid) == start else None

    def adopt_pid_file_server(self) -> None:
        if self.server:
            return
        pid_file = self.server_pid_file()
        if pid_file is not None and (verified := self._verify_server_pid(pid_file)) is not None:
            self.server, self.server_start, self.server_sid = verified

    def record_server(self) -> None:
        """Persist the server's PID, start time and session once Bazel writes its PID file.

        The PID file is written by the build, so the PID is recorded only when that process (of our UID)
        has its cwd, executable or an open file under the output root; a command line alone is not enough.
        """
        pid = self.server_pid_file()
        if pid is None or pid == self.server:
            return
        verified = self._verify_server_pid(pid)
        if verified is None:
            return
        self.server, self.server_start, self.server_sid = verified
        with contextlib.suppress(OSError):
            self.write_manifest("active")

    def low_space(self) -> int | None:
        """The lowest free space across the backing filesystems, or None if any probe fails."""
        values = [free_bytes(path) for path in backing_paths(self.settings, self.root)]
        return None if None in values else min(v for v in values if v is not None)

    def _watchdog_tripped(self, failures: int) -> tuple[int, bool]:
        free = self.low_space()
        failures = 0 if free is not None else failures + 1
        if failures >= 3 or (free is not None and free < self.settings.abort_free):
            self.reasons.append(f"watchdog abort: free space {free} below {self.settings.abort_free} bytes")
            self.stop_child()
            return failures, True
        return failures, False

    def supervise(self, signals: SignalForwarder) -> int:
        assert self.child is not None
        next_probe, failures = time.monotonic() + self.settings.watch_interval, 0
        while (rc := self._wait_tick()) is None:
            self.record_server()
            if signals.received is not None and time.monotonic() - signals.received_at > self.settings.kill_wait:
                self.child.kill()
            if time.monotonic() < next_probe:
                continue
            next_probe = time.monotonic() + self.settings.watch_interval
            failures, tripped = self._watchdog_tripped(failures)
            if tripped:
                return EXIT_TEMPFAIL
        self.record_server()
        return signals.exit_code() or rc

    def _wait_tick(self) -> int | None:
        assert self.child is not None
        try:
            return self.child.wait(timeout=TICK)
        except subprocess.TimeoutExpired:
            return None

    def stop_child(self) -> None:
        if self.child is None or self.child.poll() is not None:
            return
        self.child.terminate()
        try:
            self.child.wait(timeout=self.settings.kill_wait)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()

    # ---- teardown

    def safe_teardown(self) -> Outcome:
        """teardown() that never raises, so the audit record and resource outcomes are always written."""
        try:
            return self.teardown()
        except Exception as exc:  # noqa: BLE001 - any failure must still end as an audited, retained lease
            self.reasons.append(f"teardown error: {exc!r}")
            self.resources.setdefault("output_root", "blocked" if os.path.lexists(self.out) else "absent")
            self.resources.setdefault("worktree", "blocked" if os.path.lexists(self.worktree) else "absent")
            with contextlib.suppress(Exception):
                self.write_manifest(f"terminal-{Outcome.BLOCKED}")
            return Outcome.BLOCKED

    def teardown(self) -> Outcome:
        """The single terminal lifecycle for success, failure, signals and the reaper."""
        self.stop_child()
        out_state = self.dispose_output_root()
        if out_state is Outcome.BLOCKED and os.path.lexists(self.worktree):
            self.reasons.append("worktree kept with its unreleased output root")
            wt_state = Outcome.RETAINED
        else:
            wt_state = self.dispose_worktree()
        self.resources = {"output_root": out_state.value, "worktree": wt_state.value}
        outcome = overall(out_state, wt_state, self.setup_failed)
        nothing_left = out_state is Outcome.ABSENT and wt_state is Outcome.ABSENT
        if outcome is Outcome.REMOVED or nothing_left:
            try:
                remove_lease_dir(self.root, self.id, self.task_ino)
                return outcome
            except (RefusedError, OSError) as exc:
                self.reasons.append(f"task directory removal refused: {exc}")
                outcome = Outcome.BLOCKED
        with contextlib.suppress(OSError):
            self.write_manifest(f"terminal-{outcome}")
        log(f"{outcome} {self.task_dir}: {'; '.join(self.reasons) or 'see manifest'}")
        return outcome

    def server_pid_file(self) -> int | None:
        try:
            fd = os.open(self.out / "base" / "server" / "server.pid.txt", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            text = os.read(fd, 32).decode(errors="replace").strip()
        finally:
            os.close(fd)
        return int(text) if text.isdigit() else None

    def scoped_shutdown(self) -> bool:
        if not (self.bazel and not self.worktree.is_symlink() and self.worktree.is_dir()):
            return False
        argv = self.startup_argv()
        try:
            done = subprocess.run(
                [*argv, "shutdown"],
                cwd=self.worktree,
                timeout=self.settings.shutdown_timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            log("bazel shutdown timed out")
            return False
        self.shutdown_rc = done.returncode
        return done.returncode == 0

    def shutdown_server(self) -> bool:
        """Scoped `bazel shutdown`, then prove the exact server process is gone; False means retain."""
        pre_pid_file = self.server_pid_file()
        shutdown_ok = self.scoped_shutdown()
        pid_file = self.server_pid_file() or pre_pid_file
        if pid_file is not None and pid_file != self.server and not self.server_start:
            # No verified identity was recorded: take the PID file's, proven later only by a same-UID handle
            # reference. A recorded identity (PID + start time) is never overridden by the build-writable file.
            self.server, self.server_start, self.server_sid = pid_file, "", pid_session(pid_file) or 0
        if not self.server:
            # No server was ever recorded. The reaper cannot run `bazel shutdown`; dispose_output_root
            # then releases the root only after a complete holder scan finds nothing referencing it.
            self.server_gone = shutdown_ok or None
            return shutdown_ok or self.reaping
        self.server_gone = self.server_terminated()
        return self.server_gone

    def _unrecorded_server_matches(self) -> tuple[bool | None, str]:
        start = pid_start(self.server)
        if not start:
            return (False if pid_state(self.server) is Probe.GONE else None), ""
        same = handle_references(self.server, str(self.out))
        if same is not True:
            return same, ""
        if pid_start(self.server) != start:
            return False, ""
        return True, start

    def server_terminated(self) -> bool:
        """True only when the recorded server process is proven gone (or its PID now names another process)."""
        state = pid_state(self.server)
        if state is not Probe.ALIVE:
            return state is Probe.GONE
        if self.server_start:
            current = pid_start(self.server)
            if not current:
                return pid_state(self.server) is Probe.GONE
            same: bool | None = current == self.server_start
            target_start = self.server_start
        else:
            same, target_start = self._unrecorded_server_matches()
        if same is None:
            return False
        if not same:
            return True
        log(f"terminating Bazel server {self.server}")
        return terminate(self.server, self.settings.kill_wait, target_start)

    def sessions(self) -> frozenset[int]:
        return frozenset(sid for sid in (self.owner_sid, self.server_sid) if sid > 0)

    def ancestors(self) -> frozenset[int]:
        return frozenset(pid for pid in (self.owner_pid, self.server) if pid > 1)

    def no_holders(self, path: Path, what: str) -> bool:
        try:
            found, unknown = holders(str(path), self.sessions(), self.ancestors())
        except OSError as exc:
            self.reasons.append(f"process scan failed for the {what}: {exc}")
            return False
        if found:
            self.reasons.append(f"processes still hold the {what}: {' '.join(map(str, found))}")
        if unknown:
            self.reasons.append(f"processes could not be inspected for the {what}: {' '.join(map(str, unknown))}")
        return not found and not unknown

    def dispose_output_root(self) -> Outcome:
        if not os.path.lexists(self.out):
            return Outcome.ABSENT
        try:
            self.verify_output_root()
        except (RefusedError, OSError) as exc:
            self.reasons.append(f"output root identity refused: {exc}")
            return Outcome.BLOCKED
        if not self.shutdown_server():
            self.reasons.append(
                "the reaper cannot run bazel shutdown and the recorded server is not proven gone; output root retained"
                if self.reaping
                else "bazel shutdown failed and server termination is not proven; output root retained"
            )
            return Outcome.BLOCKED
        if not self.no_holders(self.out, "output root"):
            return Outcome.BLOCKED
        if self.reaping and not self.server:
            self.reasons.append(
                "no Bazel server was ever recorded and no shutdown was attempted by the reaper; "
                "released after a complete process scan found nothing referencing the output root"
            )
        try:
            self.bytes_reclaimed += remove_tree(self.root, self.id, "out", self.task_ino, self.out_ino)
        except (RefusedError, OSError) as exc:
            self.reasons.append(f"output root removal refused: {exc}")
            return Outcome.BLOCKED
        return Outcome.REMOVED

    def worktree_is_clean(self) -> bool:
        status = git(self.worktree, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=traditional")
        if status.returncode != 0:
            self.reasons.append("status probe failed")
            return False
        if status.stdout:
            self.reasons.append("worktree has modified, untracked or ignored files")
            return False
        hidden = [ln for ln in git(self.worktree, "ls-files", "-v").stdout.splitlines() if re.match(r"^([a-z]|S) ", ln)]
        if hidden:
            self.reasons.append("worktree has assume-unchanged or skip-worktree entries")
            return False
        return True

    def write_bundle(self) -> bool:
        bundle = self.settings.bundle
        if bundle is None:
            return False
        real_bundle = Path(os.path.realpath(bundle.parent)) / bundle.name
        if (
            bundle.is_symlink()
            or os.path.lexists(bundle)
            or os.path.lexists(real_bundle)
            or bundle.is_relative_to(self.root)
            or real_bundle.is_relative_to(self.root)
        ):
            self.reasons.append(f"bundle path exists or is inside the managed root: {bundle}")
            return False
        created = git_ok(self.worktree, "bundle", "create", str(bundle), "HEAD") and git_ok(
            self.worktree, "bundle", "verify", str(bundle)
        )
        heads = git(self.worktree, "bundle", "list-heads", str(bundle)).stdout if created else ""
        if any(line.startswith(f"{self.head} ") for line in heads.splitlines()):
            return True
        with contextlib.suppress(OSError):
            os.unlink(bundle)
        self.reasons.append("bundle creation or verification failed")
        return False

    def local_commits(self) -> list[str] | None:
        """HEAD plus the complete HEAD reflog; a failed probe cannot prove durability."""
        reflog = git(self.worktree, "reflog", "--format=%H", "HEAD")
        if reflog.returncode != 0:
            self.reasons.append("HEAD reflog probe failed; complete commit history is unknown")
            return None
        return list(dict.fromkeys([self.head, *reflog.stdout.split()]))

    def uncovered(self, commits: list[str]) -> list[str] | None:
        """Commits not reachable from a fresh explicit fetch of the branch or base (None: fetch failed)."""
        names = list(dict.fromkeys([self.branch, self.settings.base]))
        refs = []
        timeout = self.settings.fetch_timeout
        try:
            for name in names:
                private = f"refs/bazel-sandbox/{self.id}/durable/{name}"
                spec = f"+refs/heads/{name}:{private}"
                if git_ok(
                    self.repo, "fetch", "--quiet", "--no-tags", self.settings.remote, spec, probe=False, timeout=timeout
                ):
                    refs.append(private)
            if not refs:
                self.reasons.append(f"fresh fetch of {', '.join(names)} from {self.settings.remote} failed")
                return None
            return [c for c in commits if not any(git_ok(self.repo, "merge-base", "--is-ancestor", c, r) for r in refs)]
        finally:
            for ref in refs:
                git(self.repo, "update-ref", "-d", ref)

    def head_is_durable(self) -> bool:
        commits = self.local_commits()
        if commits is None:
            return False
        missing = self.uncovered(commits)
        if missing == []:
            return True
        pending = commits if missing is None else missing
        in_head = all(git_ok(self.worktree, "merge-base", "--is-ancestor", c, "HEAD") for c in pending)
        if in_head and self.write_bundle():
            return True
        self.reasons.append(
            f"HEAD {self.head or 'unknown'} or a commit in its reflog is not reachable from a freshly fetched "
            f"{self.settings.remote} {self.branch} or {self.settings.base}, and no bundle covers it"
        )
        return False

    def worktree_contained(self) -> int | None:
        """Containment like the output root: canonical root, task inode, plain dir, same filesystem."""
        try:
            return measure_child(self.root, self.id, "src", self.task_ino, strict=True)
        except (RefusedError, OSError) as exc:
            self.reasons.append(f"worktree containment refused: {exc}")
            return None

    def dispose_worktree(self) -> Outcome:
        if not os.path.lexists(self.worktree):
            return Outcome.ABSENT
        size = self.worktree_contained()
        listing = git(self.repo, "worktree", "list", "--porcelain").stdout.splitlines()
        if size is None or f"worktree {self.worktree}" not in listing:
            self.reasons.append(f"worktree is not a contained, registered worktree of {self.repo}")
            return Outcome.BLOCKED
        if not self.no_holders(self.worktree, "worktree"):
            return Outcome.BLOCKED
        self.head = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        if not (self.worktree_is_clean() and self.head_is_durable() and self.worktree_is_clean()):
            return Outcome.RETAINED
        if self.worktree_contained() is None or not git_ok(self.repo, "worktree", "remove", str(self.worktree)):
            self.reasons.append("git worktree remove refused")
            return Outcome.BLOCKED
        self.bytes_reclaimed += size
        if self.created_branch and not git_ok(self.repo, "branch", "-d", self.branch):
            self.reasons.append(f"kept unmerged local branch {self.branch}")
        return Outcome.REMOVED


NEXT_ACTIONS = {
    "terminal-retained": "push or bundle the retained work (or commit/discard dirty files), then run --reap",
    "terminal-blocked": "resolve the recorded reason (live process, unproven server, swapped path), then run --reap",
}


def overall(out_state: Outcome, wt_state: Outcome, setup_failed: bool) -> Outcome:
    states = (out_state, wt_state)
    if setup_failed or Outcome.BLOCKED in states:
        return Outcome.BLOCKED
    if Outcome.RETAINED in states:
        return Outcome.RETAINED
    return Outcome.REMOVED


def backing_paths(settings: Settings, root: Path) -> list[Path]:
    """The managed root plus the disk cache when it lives on another filesystem."""
    paths = [root]
    cache = settings.disk_cache
    if cache is not None and cache.exists() and cache.stat().st_dev != root.stat().st_dev:
        paths.append(cache)
    return paths


@contextlib.contextmanager
def lease(task: Task) -> Iterator[None]:
    """Create the task directory and hold an exclusive lease lock for the owner's lifetime."""
    try:
        task.task_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SandboxError(EXIT_SOFTWARE, f"task directory already exists: {task.task_dir}") from exc
    task.task_ino = os.lstat(task.task_dir).st_ino
    task.owner_start = pid_start(task.owner_pid)
    task.owner_sid = os.getsid(0)
    pending = task.task_dir / "lease.lock.new"
    fd = os.open(pending, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        # Lock first, then publish: an observer never sees an unlocked lease.lock for a live owner.
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.rename(pending, task.task_dir / "lease.lock")
        task.write_manifest("active")
        yield
    finally:
        os.close(fd)


def fill_record(record: AuditRecord, task: Task, outcome: Outcome) -> None:
    record.id, record.head, record.outcome = task.id, task.head, outcome.value
    record.resources = dict(task.resources)
    record.server_pid, record.shutdown_rc, record.server_gone = task.server, task.shutdown_rc, task.server_gone
    record.bytes_reclaimed = task.bytes_reclaimed
    record.worktree, record.output_user_root = str(task.worktree), str(task.out)
    for reason in task.reasons:
        record.add_reason(reason)


# ---------------------------------------------------------------- commands


def owned_flag(arg: str) -> bool:
    return any(arg == flag or arg.startswith(flag + "=") for flag in OWNED_FLAGS)


def _validate_run_flags(startup: list[str], bazel_args: list[str]) -> None:
    for arg in startup:
        if arg in TWO_TOKEN_STARTUP_FLAGS:
            raise SandboxError(EXIT_USAGE, f"supply startup option {arg} as {arg}=VALUE")
    for arg in [*startup, *bazel_args]:
        if owned_flag(arg):
            raise SandboxError(EXIT_USAGE, f"the sandbox owns {arg.split('=')[0]}; do not set it")


def parse_run_args(argv: list[str]) -> tuple[str, list[str], str, list[str]]:
    if len(argv) < 3 or argv[1] != "--":
        raise SandboxError(
            EXIT_USAGE, "usage: bazel-sandbox <branch> -- [startup options] <command> [args...] | --reap | --gc"
        )
    rest = argv[2:]
    startup = []
    while rest and rest[0].startswith("-"):
        startup.append(rest.pop(0))
    if not rest:
        raise SandboxError(EXIT_USAGE, "missing Bazel command")
    command, args = rest[0], rest[1:]
    bazel_args = args[: args.index("--")] if "--" in args else args
    _validate_run_flags(startup, bazel_args)
    return argv[0], startup, command, args


def admit(settings: Settings, root: Path, record: AuditRecord) -> None:
    free = [free_bytes(path) for path in backing_paths(settings, root)]
    record.df_before = None if None in free else min(f for f in free if f is not None)
    if record.df_before is None or record.df_before < settings.min_free:
        raise SandboxError(
            EXIT_TEMPFAIL, f"admission refused: free {record.df_before} bytes < minimum {settings.min_free} bytes"
        )


def run(argv: list[str], env: Mapping[str, str], record: AuditRecord, signals: SignalForwarder) -> int:
    branch, startup, command, args = parse_run_args(argv)
    record.branch = branch
    if subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True).returncode != 0:
        raise SandboxError(EXIT_USAGE, "invalid branch name")
    settings = Settings.from_env(env)
    top = git(Path(settings.repo_hint), "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise SandboxError(EXIT_USAGE, "not inside a Git repository (set BAZEL_SANDBOX_REPO)")
    root = open_managed_root(settings.root)
    if settings.disk_cache is not None:
        os.close(open_owned_cache(settings.disk_cache))
    admit(settings, root, record)
    task = Task(settings, root, branch, Path(top.stdout.strip()), startup)
    # Lock cache outside lease so teardown closes lease.lock (making terminal state visible) before disk-cache.lock.
    with cache_use(settings.disk_cache), lease(task):
        rc = EXIT_SOFTWARE
        try:
            task.add_worktree()
            rc = signals.exit_code() or task.run_bazel(command, args, signals)
        except SandboxError:
            task.setup_failed = task.setup_failed or task.child is None
            raise
        finally:
            fill_record(record, task, task.safe_teardown())
    # After the lease is released, so this task's own retained or blocked output root also defers collection.
    record.cache = collect_cache(settings, root)
    return signals.exit_code() or rc


def load_manifest(task_dir: Path) -> dict[str, object] | None:
    try:
        fd = os.open(task_dir / "manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        while piece := os.read(fd, 65536):
            chunks.append(piece)
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    expected = {
        "id": task_dir.name,
        "uid": os.getuid(),
        "worktree": str(task_dir / "src"),
        "output_user_root": str(task_dir / "out"),
    }
    return data if isinstance(data, dict) and all(data.get(k) == v for k, v in expected.items()) else None


def task_from_manifest(settings: Settings, root: Path, data: dict[str, object]) -> Task:
    return Task(
        settings,
        root,
        str(data["branch"]),
        Path(str(data["repo"])),
        id=str(data["id"]),
        head=str(data["head"]),
        task_ino=int(str(data["task_ino"])),
        out_ino=int(str(data.get("out_ino") or 0)),
        created_branch=bool(data["created_branch"]),
        owner_pid=int(str(data["pid"])),
        owner_start=str(data["pid_start"]),
        owner_sid=int(str(data.get("sid") or 0)),
        created=str(data["created"]),
        server=int(str(data.get("server_pid") or 0)),
        server_start=str(data.get("server_start") or ""),
        server_sid=int(str(data.get("server_sid") or 0)),
        reaping=True,
        next_review=str(data.get("next_review") or ""),
    )


def _reap_cache_target(cache: Path | None) -> Path | None:
    if cache is None or not os.path.lexists(cache):
        return None
    try:
        os.close(open_owned_cache(cache))
    except SandboxError:
        return None
    return cache


def _reap_missing_lock(task_dir: Path) -> Outcome:
    msg = (
        "lease lock missing"
        if load_manifest(task_dir) is not None
        else "missing, malformed or foreign manifest (unknown ownership)"
    )
    print(f"blocked {task_dir}: {msg}", flush=True)
    return Outcome.BLOCKED


def _reap_locked(settings: Settings, root: Path, task_dir: Path, fd: int, log_: AuditLog) -> Outcome:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        return _reap_missing_lock(task_dir)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        data = load_manifest(task_dir)
        pid = data.get("pid", "unknown") if data is not None else "unknown"
        print(f"retained {task_dir}: lease held by live owner pid {pid}", flush=True)
        return Outcome.RETAINED
    fresh = load_manifest(task_dir)
    if fresh is None:
        print(f"blocked {task_dir}: missing, malformed or foreign manifest (unknown ownership)", flush=True)
        return Outcome.BLOCKED
    return reap_claimed(settings, root, task_dir, fresh, log_)


def reap_one(settings: Settings, root: Path, task_dir: Path, log_: AuditLog) -> Outcome:
    """Re-evaluate one lease whose owner is gone; every decision is printed and every teardown audited."""
    with cache_use(_reap_cache_target(settings.disk_cache)):
        try:
            fd = os.open(task_dir / "lease.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError:
            return _reap_missing_lock(task_dir)
        try:
            return _reap_locked(settings, root, task_dir, fd, log_)
        finally:
            os.close(fd)


def _owner_is_active(task: Task, task_dir: Path) -> bool:
    owner = pid_state(task.owner_pid)
    if owner is Probe.UNKNOWN:
        print(f"retained {task_dir}: owner pid {task.owner_pid} cannot be inspected", flush=True)
        return True
    if owner is Probe.ALIVE:
        cur_start = pid_start(task.owner_pid)
        if not cur_start or not task.owner_start or cur_start == task.owner_start:
            print(f"retained {task_dir}: owner pid {task.owner_pid} is alive or cannot be inspected", flush=True)
            return True
    return False


def reap_claimed(settings: Settings, root: Path, task_dir: Path, data: dict[str, object], log_: AuditLog) -> Outcome:
    task = task_from_manifest(settings, root, data)
    if _owner_is_active(task, task_dir):
        return Outcome.RETAINED
    task.adopt_pid_file_server()
    try:
        found, unknown = holders(str(task.out), task.sessions(), task.ancestors())
    except OSError as exc:
        print(f"blocked {task_dir}: process scan failed: {exc}", flush=True)
        return Outcome.BLOCKED
    others = [pid for pid in found if pid != task.server]
    if others or unknown:
        print(
            f"retained {task_dir}: live or uninspectable processes reference {task.out}: {others + unknown}", flush=True
        )
        return Outcome.RETAINED
    record = AuditRecord(mode="reap", branch=task.branch)
    outcome = task.safe_teardown()
    fill_record(record, task, outcome)
    log_.write(record)
    print(f"{outcome} {task_dir}{': ' + record.reason if record.reason else ''}", flush=True)
    return outcome


def gc(env: Mapping[str, str], record: AuditRecord) -> int:
    settings = Settings.from_env(env)
    root = open_managed_root(settings.root)
    if settings.disk_cache is not None:
        os.close(open_owned_cache(settings.disk_cache))
    record.cache = collect_cache(settings, root)
    print(json.dumps(record.cache), flush=True)
    if record.cache.get("collected", True):
        record.outcome = Outcome.REMOVED.value
        return 0
    record.outcome = Outcome.RETAINED.value
    record.add_reason(str(record.cache.get("reason", "collection deferred")))
    return EXIT_TEMPFAIL


def reap(env: Mapping[str, str], log_: AuditLog, record: AuditRecord) -> int:
    settings = Settings.from_env(env)
    root = open_managed_root(settings.root)
    outcomes: list[Outcome] = []
    lock = _lock_file(str(root / "reap.lock"))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for task_dir in sorted(root.iterdir()):
            if is_pending_lease(task_dir):
                outcomes.append(reap_pending_lease(root, task_dir))
                continue
            if not is_lease_dir(task_dir):
                continue
            try:
                outcomes.append(reap_one(settings, root, task_dir, log_))
            except (KeyError, ValueError, TypeError, OSError, RefusedError) as exc:
                print(f"blocked {task_dir}: unreadable lease ({exc})", flush=True)
                outcomes.append(Outcome.BLOCKED)
    finally:
        os.close(lock)
    record.cache = collect_cache(settings, root)
    print(json.dumps(record.cache), flush=True)
    record.outcome = overall(Outcome.REMOVED, worst(outcomes), False).value
    record.resources = {state.value: str(outcomes.count(state)) for state in set(outcomes)}
    return 0


def is_pending_lease(path: Path) -> bool:
    """A task directory whose owner died between creating and publishing its lease lock."""
    if not TASK_ID_RE.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
        return False
    try:
        return sorted(e.name for e in os.scandir(path)) == ["lease.lock.new"]
    except OSError:
        return False


def reap_pending_lease(root: Path, task_dir: Path) -> Outcome:
    """Remove an unpublished lease only when it is provably ours and no owner holds its lock.

    No Bazel ever ran for it: the lease is published before the worktree or any Bazel process exists.
    """
    try:
        st = os.lstat(task_dir)
        if time.time() - st.st_mtime < PENDING_LEASE_MIN_AGE:
            # Not the authority (the free lock is), only a guard against the creation window.
            raise BlockingIOError("lease is being created")
        ino = st.st_ino
        with opened_task(root, task_dir.name, ino) as (rfd, tfd, dev):
            names = sorted(e.name for e in os.scandir(tfd))
            if names != ["lease.lock.new"]:
                raise RefusedError(f"unexpected entries {names}")
            fd = os.open("lease.lock.new", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=tfd)
            try:
                _check(fd, os.getuid(), dev)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.unlink("lease.lock.new", dir_fd=tfd)
                os.rmdir(task_dir.name, dir_fd=rfd)
            finally:
                os.close(fd)
    except BlockingIOError:
        print(f"retained {task_dir}: its owner is still creating the lease", flush=True)
        return Outcome.RETAINED
    except (OSError, RefusedError) as exc:
        print(f"blocked {task_dir}: unpublished lease not removed ({exc})", flush=True)
        return Outcome.BLOCKED
    print(f"removed {task_dir}: unpublished lease (its owner died while creating it)", flush=True)
    return Outcome.REMOVED


def worst(outcomes: list[Outcome]) -> Outcome:
    for state in (Outcome.BLOCKED, Outcome.RETAINED):
        if state in outcomes:
            return state
    return Outcome.REMOVED


def dispatch(
    argv: list[str], env: Mapping[str, str], record: AuditRecord, audit: AuditLog, signals: SignalForwarder
) -> int:
    if audit.fd is None:
        raise SandboxError(EXIT_SOFTWARE, f"refusing to run without an audit log: {audit.path}")
    if record.mode != "run" and len(argv) != 1:
        raise SandboxError(EXIT_USAGE, f"--{record.mode} takes no arguments")
    if record.mode == "reap":
        return reap(env, audit, record)
    if record.mode == "gc":
        return gc(env, record)
    return run(argv, env, record, signals)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    os.umask(0o077)
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    modes = {"--reap": "reap", "--gc": "gc"}
    record = AuditRecord(mode=modes.get(argv[0], "run") if argv else "run")
    with AuditLog(audit_path(env)) as audit, SignalForwarder() as signals:
        code = EXIT_SOFTWARE
        try:
            code = dispatch(argv, env, record, audit, signals)
        except SandboxError as exc:
            code = exc.code
            record.add_reason(str(exc))
            log(str(exc))
        except Exception as exc:  # noqa: BLE001 - top-level boundary: audit and exit with the same code
            code = EXIT_SOFTWARE
            record.add_reason(f"internal error: {exc!r}")
            log(f"internal error: {exc!r}")
        finally:
            # Single source of truth: the audited exit code is the code this process returns.
            record.exit_code = code
            if record.mode == "run":
                with contextlib.suppress(SandboxError, OSError):
                    root = Settings.from_env(env).root
                    record.df_after = free_bytes(root) if root.is_dir() else None
            audit.write(record)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
