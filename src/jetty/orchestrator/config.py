"""Orchestrator configuration: TOML in, validated model out.

Same philosophy as jetty core config: anything that would otherwise surface as
a confusing runtime failure is rejected at load time — unknown keys, dangling
references, dependency cycles, duplicate fixed ports. A config that loads is a
config that can at least be *attempted*.
"""

from __future__ import annotations

import re
import string
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Names end up in cgroup directories, unit names, filenames and log lines,
#: so keep them boring.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class Strict(BaseModel):
    """Reject unknown keys everywhere. A typo'd `max_restart` must not
    silently mean `max_restarts = 3`."""

    model_config = ConfigDict(extra="forbid")


class GateConfig(Strict):
    """An external condition: credentials valid, VPN up, disk mounted.

    `check` is an argv; exit 0 means satisfied. A service that lists this gate
    in `requires` and dies while the gate is failing is parked in `blocked`
    instead of burning its restart budget — the crash is the environment's
    fault, not the service's — and is restarted once the gate passes again.
    """

    check: list[str] = Field(min_length=1)
    recheck_seconds: float = Field(default=15.0, gt=0)
    timeout_seconds: float = Field(default=20.0, gt=0)


class ResolverConfig(Strict):
    """A script that maps names to binary paths, so "which binary" can change
    between releases without editing the config.

    `cmd` runs from the config file's directory (like every path a config
    describes); exit 0 with the paths on stdout. A resolver providing ONE name may print just the path; one
    providing SEVERAL prints `name=path` lines (any order — order-dependence
    would let a reordered echo silently swap binaries). Names bound by one
    invocation are pinned together: they always come from the same run of the
    script, so a release manifest can never be read half-old, half-new.

    Services reference the results as `{bin.<name>}` anywhere a placeholder
    renders. A resolver failure (non-zero exit, timeout, malformed output, a
    path that does not exist) is a spawn failure of the service that needed
    it — the ordinary restart budget and backoff apply, which is exactly the
    right behaviour while a release is mid-publish.
    """

    cmd: list[str] = Field(min_length=1)
    #: Names this resolver binds. Empty = the resolver's own name.
    provides: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30.0, gt=0)
    #: "spawn": re-run before a service (re)spawn, so a restart picks up a
    #: new release — running processes are never touched. "instance": resolve
    #: once at startup and pin for the instance's whole life.
    refresh: Literal["spawn", "instance"] = "spawn"
    #: One resolution is shared by every spawn within this window, so pinned
    #: services starting together cannot straddle a release.
    cache_seconds: float = Field(default=5.0, ge=0)
    #: Copy resolved binaries into ~/.jetty/bin before use — for sources on
    #: mounts that can disappear (a respawn happens exactly when the mount is
    #: gone). Copies are cached by source path (+ size/mtime), reused as a
    #: fallback when the source is unreachable, and cleaned up once unused
    #: for `copy_keep_days`. (Config key `copy`; the attribute is aliased
    #: because pydantic's BaseModel already has a .copy() method.)
    copy_binaries: bool = Field(default=False, alias="copy")
    copy_keep_days: float = Field(default=7.0, gt=0)
    #: Gates this resolver depends on (e.g. the release feed needs valid
    #: credentials). Every service using one of this resolver's binaries
    #: inherits these gates: while one fails, the service parks in `blocked`
    #: instead of crash-looping into resolver failures.
    requires: list[str] = Field(default_factory=list)


class ReadyConfig(Strict):
    """How to tell the service is actually up.

    At most one of `http` (GET must return < 400), `tcp` (`host:port`
    connects) or `path` (file exists — the natural probe for a UDS listener).
    With none of the three, the service counts as ready the moment it spawned.
    """

    http: str | None = None
    tcp: str | None = None
    path: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    interval_seconds: float = Field(default=0.25, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "ReadyConfig":
        if sum(x is not None for x in (self.http, self.tcp, self.path)) > 1:
            raise ValueError("configure at most one of ready.http/tcp/path")
        if self.http is not None and not self.http.startswith("http://"):
            raise ValueError("ready.http must be an http:// URL (probes are loopback)")
        return self


class RestartConfig(Strict):
    """Bounded restarts. More than `max_restarts` unexpected exits within
    `window_seconds` fails the whole instance with an aggregated error —
    a supervisor that restarts forever just hides the problem."""

    max_restarts: int = Field(default=3, ge=0)
    window_seconds: float = Field(default=60.0, gt=0)
    backoff_initial_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=15.0, gt=0)
    #: Exit codes that mean "retrying cannot help" (jetty core's own exit-2
    #: convention: a config bug, not a crash). Any of these fails the instance
    #: immediately.
    no_restart_exit: list[int] = Field(default_factory=lambda: [2])


class StopConfig(Strict):
    signal: Literal["TERM", "INT", "HUP"] = "TERM"
    grace_seconds: float = Field(default=10.0, gt=0)


class ServiceConfig(Strict):
    cmd: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    #: Names of services that must be *ready* before this one starts.
    after: list[str] = Field(default_factory=list)
    #: Names of gates this service depends on.
    requires: list[str] = Field(default_factory=list)
    ready: ReadyConfig = Field(default_factory=ReadyConfig)
    restart: RestartConfig = Field(default_factory=RestartConfig)
    stop: StopConfig = Field(default_factory=StopConfig)


class InstanceConfig(Strict):
    name: str
    #: Containment backend. `auto` prefers the strongest available:
    #: an owned cgroup > re-exec into a delegated systemd user scope > plain
    #: process groups. See containment.py for exactly what each provides.
    containment: Literal["auto", "cgroup", "scope", "pgroup"] = "auto"
    #: Default runtime directory for everything the instance runs: services
    #: without their own `cwd`, gate checks, resolver scripts. Placeholders
    #: render; the usual path rules apply (relative = config-relative and
    #: confined, `~`/absolute = anywhere). Unset = the config file's own
    #: directory.
    workdir: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "InstanceConfig":
        if not _NAME_RE.match(self.name):
            raise ValueError(f"instance.name {self.name!r} must match {_NAME_RE.pattern}")
        return self


class OrchestratorConfig(Strict):
    instance: InstanceConfig
    #: Named ports. Four forms:
    #:   "auto"      — the kernel picks any free port
    #:   8000        — exactly 8000, refuse to start if occupied (we never
    #:                 reclaim a port by killing whatever holds it)
    #:   "8000+"     — prefer 8000; occupied -> 8001, 8002, … first free wins
    #:   "8000-8020" — same, bounded: error if the whole range is taken
    ports: dict[str, int | str] = Field(default_factory=dict)
    gates: dict[str, GateConfig] = Field(default_factory=dict)
    resolvers: dict[str, ResolverConfig] = Field(default_factory=dict)
    services: dict[str, ServiceConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "OrchestratorConfig":
        for kind, names in (
            ("ports", self.ports),
            ("gates", self.gates),
            ("resolvers", self.resolvers),
            ("services", self.services),
        ):
            for name in names:
                if not _NAME_RE.match(name):
                    raise ValueError(f"{kind} key {name!r} must match {_NAME_RE.pattern}")

        provided: dict[str, str] = {}  # bin name -> resolver name
        for rname, resolver in self.resolvers.items():
            if not resolver.provides:
                resolver.provides = [rname]
            for bin_name in resolver.provides:
                if not _NAME_RE.match(bin_name):
                    raise ValueError(
                        f"resolvers.{rname} provides {bin_name!r}, which must "
                        f"match {_NAME_RE.pattern}"
                    )
                if bin_name in provided:
                    raise ValueError(
                        f"binary {bin_name!r} is provided by both resolvers "
                        f"{provided[bin_name]!r} and {rname!r}"
                    )
                provided[bin_name] = rname
            if bin_refs(resolver.cmd):
                raise ValueError(
                    f"resolvers.{rname}: a resolver's own cmd cannot use "
                    "{bin.*} placeholders"
                )
            for gate in resolver.requires:
                if gate not in self.gates:
                    raise ValueError(
                        f"resolvers.{rname} requires unknown gate {gate!r}"
                    )
        for gname, gate in self.gates.items():
            if bin_refs(gate.check):
                raise ValueError(
                    f"gates.{gname}: gate commands cannot use {{bin.*}} "
                    "placeholders (gates run before binaries are resolved)"
                )
        for sname, svc in self.services.items():
            for bin_name in sorted(service_bin_refs(svc)):
                if bin_name not in provided:
                    raise ValueError(
                        f"service {sname!r} references {{bin.{bin_name}}} but "
                        "no resolver provides it"
                    )

        fixed: dict[int, str] = {}
        for name, want in self.ports.items():
            parsed = parse_port_spec(want)
            if parsed is None:
                raise ValueError(
                    f"ports.{name} = {want!r} is not a valid port spec "
                    '(want "auto", 8000, "8000+" or "8000-8020")'
                )
            if isinstance(want, int):
                if want in fixed:
                    raise ValueError(
                        f"ports.{name} and ports.{fixed[want]} both fixed to {want}"
                    )
                fixed[want] = name

        for sname, svc in self.services.items():
            for dep in svc.after:
                if dep == sname:
                    raise ValueError(f"service {sname!r} lists itself in `after`")
                if dep not in self.services:
                    raise ValueError(
                        f"service {sname!r} is `after` unknown service {dep!r}"
                    )
            for gate in svc.requires:
                if gate not in self.gates:
                    raise ValueError(
                        f"service {sname!r} requires unknown gate {gate!r}"
                    )

        start_order(self.services)  # raises on cycles
        return self

    @staticmethod
    def load(path: str | Path) -> "OrchestratorConfig":
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return OrchestratorConfig.model_validate(raw)


def bin_refs(strings: list[str]) -> set[str]:
    """The `{bin.<name>}` placeholders appearing in `strings`."""
    names: set[str] = set()
    for s in strings:
        try:
            fields = [f for _, f, _, _ in string.Formatter().parse(s) if f]
        except ValueError:
            continue  # malformed template; render_str reports it properly
        names.update(f[4:] for f in fields if f.startswith("bin."))
    return names


def service_bin_refs(svc: ServiceConfig) -> set[str]:
    """Every binary name a service's templates depend on."""
    return bin_refs(
        [
            *svc.cmd,
            *svc.env.values(),
            svc.cwd or "",
            svc.ready.http or "",
            svc.ready.tcp or "",
            svc.ready.path or "",
        ]
    )


def parse_port_spec(want: int | str) -> tuple[int, int] | Literal["auto"] | None:
    """Normalize a port spec to "auto" or an inclusive (low, high) candidate
    range: a fixed port is (p, p), "8000+" is (8000, 65535), "8000-8020" is
    exactly that. None = malformed."""
    if want == "auto":
        return "auto"
    if isinstance(want, int):
        return (want, want) if 1 <= want <= 65535 else None
    low_s, sep, high_s = want.partition("-")
    if sep:
        high_valid = high_s.isdigit()
    else:
        low_s, sep, _ = want.partition("+")
        if not sep or _ != "":
            return None
        high_s, high_valid = "65535", True
    if not (low_s.isdigit() and high_valid):
        return None
    low, high = int(low_s), int(high_s)
    if not (1 <= low <= high <= 65535):
        return None
    return (low, high)


def start_order(services: dict[str, ServiceConfig]) -> list[str]:
    """Topological order by `after` (Kahn's algorithm); raises on cycles."""
    remaining = {name: set(svc.after) for name, svc in services.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if not deps)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ValueError(f"`after` dependencies form a cycle among: {cycle}")
        for name in ready:
            del remaining[name]
            order.append(name)
        for deps in remaining.values():
            deps.difference_update(ready)
    return order
