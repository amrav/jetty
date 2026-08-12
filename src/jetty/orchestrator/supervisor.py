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

from . import console, procfs
from .config import OrchestratorConfig, service_bin_refs, start_order
from .containment import Containment
from .gates import GateSet
from .ports import PortError, allocate_ports
from .registry import Registry, new_run_logs_dir
from .render import (
    RenderedService,
    build_context,
    render_gate_argv,
    render_service,
    render_str,
    render_argv,
    resolve_config_path,
)
from .resolvers import Resolvers
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
        quiet: bool = False,
    ):
        self._quiet = quiet
        self._config = config
        self._root = root
        self._containment = containment
        self._config_path = config_path
        #: Relative config paths anchor here (and are confined to it).
        self._config_dir = (
            str(Path(config_path).resolve().parent) if config_path else os.getcwd()
        )
        self._services: dict[str, Service] = {}
        self._ports: dict[str, int] = {}
        self._failure: str | None = None
        self._created = 0.0
        self._registry: Registry | None = None
        self._resolvers: Resolvers | None = None
        #: Per service: the resolver generations its live incarnation was
        #: spawned from. The pinned-group invariant is enforced against this:
        #: two services sharing a multi-binary resolver must not run on
        #: different generations of it.
        self._spawn_gens: dict[str, dict[str, int]] = {}
        self._bounce_tasks: set[asyncio.Task] = set()
        self._logs_dir: Path | None = None
        self._state = "starting"

    async def run(self) -> int:
        cfg = self._config
        name = cfg.instance.name
        inst_dir = self._root / "instances" / name
        inst_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = new_run_logs_dir(name)
        self._logs_dir = logs_dir
        self._created = time.time()
        self._registry = Registry(self._root)

        try:
            self._ports = allocate_ports(cfg.ports)
        except PortError as e:
            _note(str(e))
            return 1

        ctx = build_context(name, self._ports, str(inst_dir), str(logs_dir))
        workdir = self._config_dir
        if cfg.instance.workdir is not None:
            workdir = resolve_config_path(
                render_str(cfg.instance.workdir, ctx),
                self._config_dir,
                "instance.workdir",
            )
        # The foreground view: every service's — and resolver's — output on
        # OUR stdout, each line under its coloured [name] prefix (the log
        # files stay the durable copy). Line-buffered per name so output
        # never interleaves mid-line.
        echo_for = None
        if not self._quiet:
            console_names = list(cfg.services) + [
                f"resolver-{n}" for n in cfg.resolvers
            ]
            prefixer = console.Prefixer(
                console_names, console.want_color(sys.stdout)
            )
            buffers = {n: console.LineBuffer() for n in console_names}

            def echo_for(cname: str):  # noqa: F811
                def echo(chunk: bytes) -> None:
                    for line in buffers[cname].feed(chunk):
                        print(prefixer.format(cname, line), flush=True)

                return echo

        gates = GateSet(
            {
                n: (g, render_gate_argv(g, ctx, self._config_dir))
                for n, g in cfg.gates.items()
            },
            cwd=workdir,
        )
        self._resolvers = Resolvers(
            cfg.resolvers,
            {
                n: render_argv(r.cmd, ctx, self._config_dir, f"resolvers.{n} cmd")
                for n, r in cfg.resolvers.items()
            },
            cwd=workdir,
            logs_dir=logs_dir,
            echo=(
                (lambda rname, chunk: echo_for(f"resolver-{rname}")(chunk))
                if echo_for is not None
                else None
            ),
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

        def renderer(sname: str, svc_cfg, bins: set[str]):
            async def render() -> RenderedService:
                bin_ctx = {}
                if bins:
                    bin_ctx = await self._resolvers.context_for(bins)
                    self._enforce_pinning(sname, bins)
                return render_service(
                    svc_cfg, {**ctx, **bin_ctx}, self._config_dir, default_cwd=workdir
                )

            return render

        for sname in start_order(cfg.services):
            svc_cfg = cfg.services[sname]
            bins = service_bin_refs(svc_cfg)
            # A service depends on its own gates plus those of every resolver
            # it uses: a resolver that needs credentials must park its
            # consumers as `blocked`, not crash-loop them into resolution
            # failures.
            requires = list(svc_cfg.requires)
            for rname in sorted(self._resolvers.resolver_names(bins)):
                for gate in cfg.resolvers[rname].requires:
                    if gate not in requires:
                        requires.append(gate)
            self._services[sname] = Service(
                sname,
                svc_cfg,
                renderer(sname, svc_cfg, bins),
                self._containment,
                gates,
                logs_dir / f"{sname}.log",
                [self._services[d].ready_event for d in svc_cfg.after],
                extra_env,
                notify,
                fail,
                requires=requires,
                echo=echo_for(sname) if echo_for is not None else None,
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

    def _enforce_pinning(self, sname: str, bins: set[str]) -> None:
        """Called as `sname` spawns. If it just came up on a NEW generation of
        a resolver that pins several binaries together, every sibling still
        running an older generation is bounced (a budget-free restart) so the
        group can never run split across releases. Keyed on generation — the
        counter moves only when the resolution's result changed — so a crash
        loop on an unchanged release never touches healthy siblings."""
        gens = self._resolvers.generations_for(bins)
        self._spawn_gens[sname] = gens
        for rname, gen in gens.items():
            resolver = self._config.resolvers[rname]
            if resolver.refresh != "spawn" or len(resolver.provides) < 2:
                continue
            for other, other_gens in self._spawn_gens.items():
                if other == sname or other_gens.get(rname, gen) == gen:
                    continue
                task = asyncio.create_task(
                    self._services[other].restart(
                        f"binaries pinned by resolver {rname!r} moved to a new "
                        f"release ({sname!r} spawned on it)"
                    ),
                    name=f"bounce:{other}",
                )
                self._bounce_tasks.add(task)
                task.add_done_callback(self._bounce_tasks.discard)

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
                "logs_dir": str(self._logs_dir),
                "state": state,
                "services": {n: s.status() for n, s in self._services.items()},
                "resolvers": self._resolvers.state if self._resolvers else {},
            }
        )
