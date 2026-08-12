"""The per-instance supervisor: owns ports, gates, services and teardown.

Lifecycle: allocate ports → render templates → set up containment → start
services in dependency order → wait for a shutdown cause (signal, or a
service failing the instance) → stop everything in reverse dependency waves →
sweep the containment as a backstop.

Exit codes: 0 clean (including operator-requested stop), 1 instance failure.
Config problems never reach here — the CLI exits 2 before a Supervisor is
constructed (jetty core's convention).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from . import procfs
from .config import OrchestratorConfig, start_order
from .containment import Containment
from .gates import GateSet
from .ports import PortError, allocate_ports
from .registry import Registry
from .render import build_context, render_gate_argv, render_service
from .service import Service


def _note(msg: str) -> None:
    print(f"jetty-orc: {msg}", file=sys.stderr)


def service_extra_env(instance: str, containment: Containment) -> dict[str, str]:
    """Environment every service receives beyond its own `env` table.

    JETTY_ORC_CGROUP_ROOT is the instance's cgroup directory (cgroup
    containment only): a service that reports resource usage — a dashboard,
    a health endpoint — can account and enumerate the *whole instance* from
    it, instead of just its own subgroup. Reading `/proc/self/cgroup` from
    inside a service sees only that service's leaf, which silently
    undercounts once siblings hold the interesting processes.
    """
    extra = {"JETTY_ORC_INSTANCE": instance}
    if containment.kind == "cgroup" and containment.root:
        extra["JETTY_ORC_CGROUP_ROOT"] = containment.root
    return extra


class Supervisor:
    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        root: Path,
        containment: Containment,
        config_path: str | None = None,
    ):
        self._config = config
        self._root = root
        self._containment = containment
        self._config_path = config_path
        self._services: dict[str, Service] = {}
        self._ports: dict[str, int] = {}
        self._failure: str | None = None
        self._created = 0.0
        self._registry: Registry | None = None
        self._state = "starting"

    async def run(self) -> int:
        cfg = self._config
        name = cfg.instance.name
        inst_dir = self._root / "instances" / name
        logs_dir = inst_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._created = time.time()
        self._registry = Registry(self._root)

        try:
            self._ports = allocate_ports(cfg.ports)
        except PortError as e:
            _note(str(e))
            return 1

        ctx = build_context(name, self._ports, str(inst_dir), str(logs_dir))
        gates = GateSet(
            {n: (g, render_gate_argv(g, ctx)) for n, g in cfg.gates.items()}
        )
        self._containment.setup(list(cfg.services))

        shutdown = asyncio.Event()

        def fail(reason: str, _svc: Service) -> None:
            if self._failure is None:
                self._failure = reason
                shutdown.set()

        def notify(_svc: Service) -> None:
            self._write_record()

        extra_env = service_extra_env(name, self._containment)
        for sname in start_order(cfg.services):
            svc_cfg = cfg.services[sname]
            self._services[sname] = Service(
                sname,
                svc_cfg,
                render_service(svc_cfg, ctx),
                self._containment,
                gates,
                logs_dir / f"{sname}.log",
                [self._services[d].ready_event for d in svc_cfg.after],
                extra_env,
                notify,
                fail,
            )

        loop = asyncio.get_running_loop()
        signals_seen = 0

        def on_signal(signum: int) -> None:
            nonlocal signals_seen
            signals_seen += 1
            if signals_seen == 1:
                _note(f"got {signal.Signals(signum).name}, stopping instance {name!r}")
                shutdown.set()
            else:
                _note("second signal — hard-killing every service group")
                self._containment.kill_all()

        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, on_signal, signum)

        ports_desc = (
            " ".join(f"{k}={v}" for k, v in self._ports.items()) or "(none)"
        )
        _note(
            f"instance {name!r} up: containment={self._containment.kind} "
            f"ports: {ports_desc} logs: {logs_dir}"
        )

        tasks = [
            asyncio.create_task(svc.run(), name=f"svc:{n}")
            for n, svc in self._services.items()
        ]
        self._write_record()

        try:
            await shutdown.wait()
            if self._failure:
                self._state = "failing"
                _note(f"instance {name!r} failed: {self._failure}")
                _note("stopping remaining services")
            await self._stop_all()
            done, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5)
        finally:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)
            self._containment.kill_all()  # backstop; no-op when already clean
            self._containment.close()

        if self._failure:
            self._state = "failed"
            self._write_record()  # leave a post-mortem for `ls`
            _note(f"instance {name!r} stopped after failure (exit 1)")
            return 1
        self._registry.remove(name)
        _note(f"instance {name!r} stopped cleanly")
        return 0

    async def _stop_all(self) -> None:
        """Reverse dependency waves: a service is stopped only once everything
        that declared `after` it is already down."""
        dependents: dict[str, set[str]] = {n: set() for n in self._services}
        for sname, svc_cfg in self._config.services.items():
            for dep in svc_cfg.after:
                dependents[dep].add(sname)
        stopped: set[str] = set()
        remaining = set(self._services)
        while remaining:
            wave = sorted(
                n for n in remaining if dependents[n] <= stopped
            )
            await asyncio.gather(*(self._services[n].stop() for n in wave))
            stopped.update(wave)
            remaining.difference_update(wave)

    def _write_record(self) -> None:
        if self._registry is None:
            return
        state = self._state
        if state == "failed" and self._failure:
            state = f"failed: {self._failure.splitlines()[0]}"
        elif state == "starting":
            state = "running"
        self._registry.write(
            {
                "name": self._config.instance.name,
                "created": self._created,
                "config_path": self._config_path,
                "supervisor_pid": os.getpid(),
                "supervisor_start_ticks": procfs.start_ticks(os.getpid()),
                "containment": {
                    "kind": self._containment.kind,
                    "root": self._containment.root,
                },
                "ports": self._ports,
                "state_dir": str(self._root / "instances" / self._config.instance.name),
                "state": state,
                "services": {n: s.status() for n, s in self._services.items()},
            }
        )
