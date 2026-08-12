"""Registry of running instances: one JSON file per instance.

The supervisor writes its record on every service state change and removes it
on clean shutdown; a record whose supervisor pid is dead (pid + kernel start
ticks, so a reused pid cannot impersonate a supervisor) is stale. Failed
instances leave their final record behind on purpose — `ls` shows the
post-mortem until `kill <name>` clears it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import procfs


def default_root() -> Path:
    env = os.environ.get("JETTY_ORC_ROOT")
    if env:
        return Path(env)
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state"
    )
    return Path(state_home) / "jetty-orc"


def log_root() -> Path:
    env = os.environ.get("JETTY_ORC_LOG_ROOT")
    return Path(env) if env else Path.home() / ".jetty" / "logs"


def new_run_logs_dir(instance: str) -> Path:
    """One directory per `up` invocation — `<instance>-<timestamp>` — so the
    logs of separate runs of the same instance never interleave. A same-second
    collision (a supervisor crash-looping under an outer process manager) gets
    a numeric suffix rather than appending into the previous run's files."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = log_root()
    candidate = base / f"{instance}-{stamp}"
    n = 2
    while candidate.exists():
        candidate = base / f"{instance}-{stamp}-{n}"
        n += 1
    candidate.mkdir(parents=True)
    return candidate


class Registry:
    def __init__(self, root: Path):
        self.dir = root / "registry"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def write(self, record: dict) -> None:
        tmp = self.path(record["name"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=1))
        os.replace(tmp, self.path(record["name"]))

    def remove(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)

    def load(self, name: str) -> dict | None:
        try:
            return json.loads(self.path(name).read_text())
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None

    def load_all(self) -> list[dict]:
        records = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return records


def supervisor_alive(record: dict) -> bool:
    pid = record.get("supervisor_pid")
    if not pid:
        return False
    return procfs.identity_matches(pid, record.get("supervisor_start_ticks"))
