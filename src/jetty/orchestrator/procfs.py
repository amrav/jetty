"""Read-only /proc and cgroup2 helpers (Linux only).

Everything here is best-effort by construction: a pid can vanish between
listing and reading, a cgroup file can be absent because a controller is not
enabled. Callers get None / empty rather than exceptions for those cases.
"""

from __future__ import annotations

import os
from pathlib import Path

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
CLK_TCK = os.sysconf("SC_CLK_TCK")

CGROUP_FS = Path("/sys/fs/cgroup")


def _stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat AFTER the comm — comm may itself contain
    spaces and parentheses, so split on the last ')'."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return raw.rsplit(")", 1)[1].split()


def alive(pid: int) -> bool:
    """Existing and not a zombie. The zombie case matters: a supervisor whose
    parent hasn't reaped it yet is dead for every practical purpose (nothing
    is supervising any more), and `kill` must not wait on it forever."""
    fields = _stat_fields(pid)
    return fields is not None and fields[0] != "Z"


def start_ticks(pid: int) -> int | None:
    """Kernel start time of the process — with the pid, a reuse-proof identity."""
    fields = _stat_fields(pid)
    return int(fields[19]) if fields else None


def identity_matches(pid: int, ticks: int | None) -> bool:
    if not alive(pid):
        return False
    return ticks is None or start_ticks(pid) == ticks


def session_id(pid: int) -> int | None:
    fields = _stat_fields(pid)
    return int(fields[3]) if fields else None


def all_pids() -> list[int]:
    return [int(e) for e in os.listdir("/proc") if e.isdigit()]


def session_members(sids: set[int]) -> list[int]:
    """Every live process whose session id is in `sids`. This is how the
    pgroup backend enumerates a service's tree: each service child is spawned
    as a session leader, and descendants inherit the session unless they
    actively call setsid() themselves."""
    if not sids:
        return []
    return [pid for pid in all_pids() if session_id(pid) in sids]


def rss_bytes(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            resident = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
        except (OSError, IndexError, ValueError):
            continue
        total += resident * PAGE_SIZE
    return total


def cpu_ticks(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        fields = _stat_fields(pid)
        if fields:
            total += int(fields[11]) + int(fields[12])  # utime + stime
    return total


# --- cgroup v2 ---


def cgroup_self_dir() -> Path | None:
    """The calling process's cgroup directory on the v2 unified hierarchy."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return CGROUP_FS / line[3:].lstrip("/")
    except OSError:
        pass
    return None


def cgroup_procs(directory: Path, recursive: bool = True) -> list[int]:
    pids: list[int] = []
    dirs = [directory]
    while dirs:
        d = dirs.pop()
        try:
            pids += [int(x) for x in (d / "cgroup.procs").read_text().split()]
        except OSError:
            continue
        if recursive:
            try:
                dirs += [e for e in d.iterdir() if e.is_dir()]
            except OSError:
                pass
    return pids


def cgroup_mem_bytes(directory: Path) -> int | None:
    try:
        return int((directory / "memory.current").read_text())
    except (OSError, ValueError):
        return None


def cgroup_cpu_usec(directory: Path) -> int | None:
    try:
        for line in (directory / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except OSError:
        pass
    return None
