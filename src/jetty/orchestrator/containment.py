"""Process containment: making "kill the instance" mean the whole tree.

The problem this solves: services spawn helpers that spawn helpers, and pid
bookkeeping can never enumerate a tree that double-forks. So containment is
delegated to the kernel, at the strongest level the environment offers:

  cgroup  — we own a delegated cgroup2 directory (e.g. running under a systemd
            unit with `Delegate=yes`, or inside a container that owns its
            cgroup namespace). One subgroup per service; enumeration is
            `cgroup.procs`, teardown is `cgroup.kill`. Nothing escapes.
  scope   — not in an owned cgroup, but a systemd user manager is reachable:
            re-exec ourselves under `systemd-run --user --scope -p
            Delegate=yes`, which lands us in case `cgroup`.
  pgroup  — plain process groups/sessions. Each service child is a session
            leader; enumeration walks /proc by session id, teardown is
            killpg + per-pid sweep. A grandchild that itself calls setsid()
            escapes — this is the fallback, not the goal.

Every backend also sets PR_SET_PDEATHSIG on direct children, so even a
SIGKILL'd supervisor takes its immediate children down.
"""

from __future__ import annotations

import ctypes
import dataclasses
import os
import shutil
import signal
import sys
from pathlib import Path

from . import procfs

#: Set in the environment across the scope re-exec so we don't loop.
SCOPE_MARKER_ENV = "JETTY_ORC_SCOPED"

_PR_SET_PDEATHSIG = 1


class ContainmentError(RuntimeError):
    pass


def _set_pdeathsig() -> None:
    """Runs in the child between fork and exec. Ask the kernel for SIGTERM
    when the parent dies; if the parent is already gone (reparented to init),
    the signal will never come — bail out instead of running orphaned."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() == 1:
        os._exit(1)


@dataclasses.dataclass
class ServiceStats:
    mem_bytes: int | None
    nprocs: int


class Containment:
    """Backend interface. `setup` before any spawn; `preexec` supplies the
    child-side hook for Popen; `register` records the direct child pid."""

    kind: str
    root: str | None = None  # cgroup dir, for the registry / external readers

    def setup(self, services: list[str]) -> None:
        raise NotImplementedError

    def preexec(self, service: str):
        raise NotImplementedError

    def register(self, service: str, pid: int) -> None:
        raise NotImplementedError

    def pids(self, service: str) -> list[int]:
        raise NotImplementedError

    def sweep(self, service: str, sig: int) -> None:
        """Deliver `sig` to every process in the service's group."""
        raise NotImplementedError

    def kill_all(self) -> None:
        for service in self._services:
            self.sweep(service, signal.SIGKILL)

    def stats(self, service: str) -> ServiceStats:
        raise NotImplementedError

    def close(self) -> None:
        pass

    _services: list[str] = []


class CgroupBackend(Containment):
    kind = "cgroup"

    def __init__(self, root_dir: Path):
        self._root = root_dir
        self.root = str(root_dir)
        self._services = []

    def _svc_dir(self, service: str) -> Path:
        return self._root / f"svc-{service}"

    def setup(self, services: list[str]) -> None:
        self._services = list(services)
        # The "no internal processes" rule: a cgroup cannot both hold
        # processes and enable controllers for children. So move ourselves
        # into a leaf first, then enable controllers on the root, then create
        # the service leaves.
        sup = self._root / "supervisor"
        sup.mkdir(exist_ok=True)
        (sup / "cgroup.procs").write_text("0")
        try:
            available = (self._root / "cgroup.controllers").read_text().split()
        except OSError:
            available = []
        for ctrl in ("cpu", "memory", "pids"):
            if ctrl in available:
                try:
                    (self._root / "cgroup.subtree_control").write_text(f"+{ctrl}")
                except OSError:
                    # Accounting is best-effort; enumeration and cgroup.kill
                    # work regardless of controllers.
                    pass
        for service in services:
            self._svc_dir(service).mkdir(exist_ok=True)

    def preexec(self, service: str):
        procs_path = str(self._svc_dir(service) / "cgroup.procs")

        def _enter() -> None:
            _set_pdeathsig()
            # Writing "0" moves the calling process. Doing this in the child
            # pre-exec (rather than the parent post-fork) means there is no
            # window in which the service could spawn a grandchild outside
            # its cgroup.
            fd = os.open(procs_path, os.O_WRONLY)
            try:
                os.write(fd, b"0")
            finally:
                os.close(fd)

        return _enter

    def register(self, service: str, pid: int) -> None:
        pass  # preexec already placed the child

    def pids(self, service: str) -> list[int]:
        return procfs.cgroup_procs(self._svc_dir(service))

    def sweep(self, service: str, sig: int) -> None:
        if sig == signal.SIGKILL:
            try:
                (self._svc_dir(service) / "cgroup.kill").write_text("1")
                return
            except OSError:
                pass  # pre-5.14 kernel: fall through to per-pid
        for pid in self.pids(service):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def stats(self, service: str) -> ServiceStats:
        d = self._svc_dir(service)
        return ServiceStats(
            mem_bytes=procfs.cgroup_mem_bytes(d),
            nprocs=len(procfs.cgroup_procs(d)),
        )

    def close(self) -> None:
        # Leaves can only be removed once empty; the enclosing scope (if any)
        # is collected by systemd when its last process exits. Best-effort.
        for service in self._services:
            try:
                self._svc_dir(service).rmdir()
            except OSError:
                pass


class PgroupBackend(Containment):
    kind = "pgroup"

    def __init__(self) -> None:
        self._sids: dict[str, int] = {}
        self._services = []

    def setup(self, services: list[str]) -> None:
        self._services = list(services)

    def preexec(self, service: str):
        return _set_pdeathsig  # start_new_session=True does the rest

    def register(self, service: str, pid: int) -> None:
        # The child is a session (and process-group) leader; the sid outlives
        # the leader as long as any member does.
        self._sids[service] = pid

    def pids(self, service: str) -> list[int]:
        sid = self._sids.get(service)
        return procfs.session_members({sid}) if sid else []

    def sweep(self, service: str, sig: int) -> None:
        sid = self._sids.get(service)
        if not sid:
            return
        try:
            os.killpg(sid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        # Catch members that moved to their own process group but kept the
        # session (e.g. shells doing job control).
        for pid in procfs.session_members({sid}):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def stats(self, service: str) -> ServiceStats:
        pids = self.pids(service)
        return ServiceStats(
            mem_bytes=procfs.rss_bytes(pids) if pids else 0, nprocs=len(pids)
        )


# --- backend selection ---


def _owned_cgroup() -> CgroupBackend | None:
    """Usable iff we can write it, we are alone in it (killing by cgroup must
    never catch a process we didn't start — from an interactive shell the
    session scope holds the shell and every sibling job), and we can create
    subgroups."""
    d = procfs.cgroup_self_dir()
    if d is None or not os.access(d, os.W_OK):
        return None
    if set(procfs.cgroup_procs(d, recursive=False)) - {os.getpid()}:
        return None
    probe = d / "jetty-orc-probe"
    try:
        probe.mkdir()
        probe.rmdir()
    except OSError:
        return None
    return CgroupBackend(d)


def _systemd_user_available() -> bool:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return bool(
        shutil.which("systemd-run")
        and runtime
        and (Path(runtime) / "systemd").exists()
    )


def acquire(mode: str, instance: str, self_argv: list[str]) -> Containment:
    """Pick (or arrange) a backend. May re-exec the current process under
    `systemd-run --user --scope` — in that case this function never returns
    and the fresh process re-enters with SCOPE_MARKER_ENV set."""
    if mode == "pgroup":
        return PgroupBackend()

    scoped = os.environ.pop(SCOPE_MARKER_ENV, None) is not None
    owned = _owned_cgroup()
    if owned is not None:
        return owned
    if mode == "cgroup":
        raise ContainmentError(
            "containment = \"cgroup\" but the current cgroup is not usable "
            "(not writable, shared with other processes, or subgroups cannot "
            "be created); run under a delegated unit (systemd Delegate=yes) "
            "or use \"scope\"/\"auto\""
        )
    if scoped:
        # We already re-exec'd into a scope and still cannot own our cgroup;
        # don't loop.
        if mode == "scope":
            raise ContainmentError(
                "re-exec'd into a systemd scope but the resulting cgroup is "
                "not usable; is Delegate= honoured on this host?"
            )
        print(
            "jetty-orc: warning: scope cgroup not usable, falling back to "
            "process groups",
            file=sys.stderr,
        )
        return PgroupBackend()
    if _systemd_user_available():
        argv = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "-p",
            "Delegate=yes",
            "--unit",
            f"jetty-orc-{instance}-{os.getpid()}",
            *self_argv,
        ]
        env = dict(os.environ, **{SCOPE_MARKER_ENV: "1"})
        os.execvpe(argv[0], argv, env)  # never returns
    if mode == "scope":
        raise ContainmentError(
            "containment = \"scope\" but no systemd user manager is reachable "
            "(need systemd-run and $XDG_RUNTIME_DIR/systemd)"
        )
    print(
        "jetty-orc: warning: no owned cgroup and no systemd user manager; "
        "falling back to process groups (setsid'ing grandchildren can escape)",
        file=sys.stderr,
    )
    return PgroupBackend()


def external_kill(kind: str, root: str | None, service_pids: list[int]) -> None:
    """Force-kill an instance from outside its supervisor (`jetty-orc kill
    --force`). For cgroup containment this also kills the supervisor — that is
    the point: it is the path of last resort for a wedged instance."""
    if kind == "cgroup" and root:
        try:
            (Path(root) / "cgroup.kill").write_text("1")
            return
        except OSError:
            pass
    sids = {pid for pid in service_pids if pid}
    for pid in procfs.session_members(sids):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for sid in sids:
        try:
            os.killpg(sid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
