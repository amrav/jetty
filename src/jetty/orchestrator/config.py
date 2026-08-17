"""Orchestrator configuration: TOML in, validated objects out.

Same philosophy as jetty core config: anything that would otherwise surface
as a confusing runtime failure is rejected at load time — unknown keys,
wrong types, dangling references, dependency cycles, duplicate fixed ports.
A config that loads is a config that can at least be *attempted*.

Validation is hand-rolled on purpose. The orchestrator is stdlib-only so a
bare copy of the package directory runs on any box with Python 3.11+
(`python3 <dir> ...`) — no pip, no venv, no wheels. The validation surface
is small and stable; a dependency for it would be the only dependency, and
it would cost exactly the deployment story this tool exists to provide.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
import string
import tomllib
from pathlib import Path
from typing import Literal

#: Names end up in cgroup directories, unit names, filenames and log lines,
#: so keep them boring.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ConfigError(ValueError):
    pass


class _Table:
    """One TOML table: typed `take_*` accessors that consume keys, plus
    unknown-key rejection at the end — a typo'd `max_restart` must not
    silently mean `max_restarts = 3`."""

    def __init__(self, raw: object, where: str):
        if not isinstance(raw, dict):
            raise ConfigError(f"{where or 'config'}: expected a table")
        self._raw = dict(raw)
        self._where = where

    def at(self, key: str) -> str:
        return f"{self._where}.{key}" if self._where else key

    def take_str(
        self, key: str, default: str | None = None, required: bool = False
    ) -> str | None:
        if key not in self._raw:
            if required:
                raise ConfigError(f"{self.at(key)} is required")
            return default
        value = self._raw.pop(key)
        if not isinstance(value, str):
            raise ConfigError(f"{self.at(key)} must be a string")
        return value

    def take_number(
        self,
        key: str,
        default: float,
        *,
        gt: float | None = None,
        ge: float | None = None,
    ) -> float:
        value = self._raw.pop(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{self.at(key)} must be a number")
        value = float(value)
        if gt is not None and not value > gt:
            raise ConfigError(f"{self.at(key)} must be > {gt}")
        if ge is not None and not value >= ge:
            raise ConfigError(f"{self.at(key)} must be >= {ge}")
        return value

    def take_int(self, key: str, default: int, *, ge: int | None = None) -> int:
        value = self._raw.pop(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self.at(key)} must be an integer")
        if ge is not None and value < ge:
            raise ConfigError(f"{self.at(key)} must be >= {ge}")
        return value

    def take_bool(self, key: str, default: bool) -> bool:
        value = self._raw.pop(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{self.at(key)} must be true or false")
        return value

    def take_str_list(self, key: str, min_len: int = 0) -> list[str]:
        value = self._raw.pop(key, [])
        if not isinstance(value, list) or any(
            not isinstance(x, str) for x in value
        ):
            raise ConfigError(f"{self.at(key)} must be a list of strings")
        if len(value) < min_len:
            raise ConfigError(
                f"{self.at(key)} needs at least {min_len} "
                f"entr{'y' if min_len == 1 else 'ies'}"
            )
        return value


    def take_argv(self, key: str, min_len: int = 0) -> list[str]:
        """An argv: a list of strings, or a single string split shell-style
        (`cmd = "./run --flag"`). The string form is a convenience, not a
        shell — `&&`, pipes and redirects stay literal argv words; wrap in
        `["bash", "-c", "..."]` for shell semantics."""
        value = self._raw.get(key)
        if isinstance(value, str):
            try:
                self._raw[key] = shlex.split(value)
            except ValueError as e:
                raise ConfigError(f"{self.at(key)}: {e}") from None
        return self.take_str_list(key, min_len)

    def take_int_list(self, key: str, default: list[int]) -> list[int]:
        value = self._raw.pop(key, list(default))
        if not isinstance(value, list) or any(
            isinstance(x, bool) or not isinstance(x, int) for x in value
        ):
            raise ConfigError(f"{self.at(key)} must be a list of integers")
        return value

    def take_str_dict(self, key: str) -> dict[str, str]:
        value = self._raw.pop(key, {})
        if not isinstance(value, dict) or any(
            not isinstance(v, str) for v in value.values()
        ):
            raise ConfigError(f"{self.at(key)} must be a table of strings")
        return dict(value)

    def take_choice(self, key: str, choices: tuple[str, ...], default: str) -> str:
        value = self._raw.pop(key, default)
        if value not in choices:
            raise ConfigError(
                f"{self.at(key)} must be one of: {', '.join(choices)}"
            )
        return value

    def take_table(self, key: str) -> dict:
        value = self._raw.pop(key, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{self.at(key)} must be a table")
        return value

    def done(self) -> None:
        if self._raw:
            keys = ", ".join(repr(k) for k in sorted(self._raw))
            raise ConfigError(f"{self._where or 'config'}: unknown key(s) {keys}")


@dataclasses.dataclass
class GateConfig:
    """An external condition (credentials, VPN, mounted disk...).

    `check` is an argv; exit 0 means satisfied. A service that lists this
    gate in `requires` and dies while the gate is failing is parked in
    `blocked` instead of burning its restart budget, and is restarted when
    the gate passes again.
    """

    check: list[str]
    recheck_seconds: float = 15.0
    timeout_seconds: float = 20.0
    #: A continuous gate is a runtime INVARIANT, not just a start
    #: precondition: while it is closed, services requiring it must not run.
    #: The supervisor polls it for running services and gracefully stops them
    #: into `blocked` when it closes (budget-free), reviving them when it
    #: reopens. Default gates never touch a running process.
    continuous: bool = False
    #: A check might flake; stopping a healthy process over one bad probe
    #: would turn the blip into an outage. A continuous gate counts as
    #: CLOSED only after this many consecutive failed checks (one real run
    #: per recheck_seconds — closure latency ≈ close_after × recheck).
    #: Reopening is immediate: one passing check revives the service.
    close_after: int = 3

    @classmethod
    def parse(cls, raw: object, where: str) -> "GateConfig":
        t = _Table(raw, where)
        cfg = cls(
            check=t.take_argv("check", min_len=1),
            recheck_seconds=t.take_number("recheck_seconds", 15.0, gt=0),
            timeout_seconds=t.take_number("timeout_seconds", 20.0, gt=0),
            continuous=t.take_bool("continuous", False),
            close_after=t.take_int("close_after", 3, ge=1),
        )
        t.done()
        return cfg


@dataclasses.dataclass
class ResolverConfig:
    """A script that maps names to binary paths, so "which binary" can change
    between releases without editing the config. See resolvers.py for the
    full semantics (pinning, generations, copying, gating)."""

    cmd: list[str]
    provides: list[str] = dataclasses.field(default_factory=list)
    timeout_seconds: float = 30.0
    refresh: Literal["spawn", "instance"] = "spawn"
    cache_seconds: float = 5.0
    #: Config key `copy`: copy resolved binaries into ~/.jetty/bin (cached by
    #: source path, vanish-resilient, aged out after `copy_keep_days`).
    copy_binaries: bool = False
    copy_keep_days: float = 7.0
    #: Gates this resolver depends on; its consumers inherit them.
    requires: list[str] = dataclasses.field(default_factory=list)

    @classmethod
    def parse(cls, raw: object, where: str) -> "ResolverConfig":
        t = _Table(raw, where)
        cfg = cls(
            cmd=t.take_argv("cmd", min_len=1),
            provides=t.take_str_list("provides"),
            timeout_seconds=t.take_number("timeout_seconds", 30.0, gt=0),
            refresh=t.take_choice("refresh", ("spawn", "instance"), "spawn"),
            cache_seconds=t.take_number("cache_seconds", 5.0, ge=0),
            copy_binaries=t.take_bool("copy", False),
            copy_keep_days=t.take_number("copy_keep_days", 7.0, gt=0),
            requires=t.take_str_list("requires"),
        )
        t.done()
        return cfg


@dataclasses.dataclass
class ReadyConfig:
    """How to tell the service is actually up. At most one of `http` (GET
    must return < 400), `tcp` (`host:port` connects), `uds` (unix socket
    connects — the robust probe for a UDS listener) or `path` (file exists).
    `path` cannot tell a live socket from a stale file left by a crashed
    incarnation; prefer `uds` for sockets. With none of the four, the
    service counts as ready the moment it spawned."""

    http: str | None = None
    tcp: str | None = None
    uds: str | None = None
    path: str | None = None
    timeout_seconds: float = 30.0
    interval_seconds: float = 0.25

    @classmethod
    def parse(cls, raw: object, where: str) -> "ReadyConfig":
        t = _Table(raw, where)
        cfg = cls(
            http=t.take_str("http"),
            tcp=t.take_str("tcp"),
            uds=t.take_str("uds"),
            path=t.take_str("path"),
            timeout_seconds=t.take_number("timeout_seconds", 30.0, gt=0),
            interval_seconds=t.take_number("interval_seconds", 0.25, gt=0),
        )
        t.done()
        if sum(x is not None for x in (cfg.http, cfg.tcp, cfg.uds, cfg.path)) > 1:
            raise ConfigError(f"{where}: configure at most one of http/tcp/uds/path")
        if cfg.http is not None and not cfg.http.startswith("http://"):
            raise ConfigError(
                f"{where}.http must be an http:// URL (probes are loopback)"
            )
        return cfg


@dataclasses.dataclass
class RestartConfig:
    """Bounded restarts. More than `max_restarts` unexpected exits within
    `window_seconds` fails the whole instance with an aggregated error — a
    supervisor that restarts forever just hides the problem."""

    max_restarts: int = 3
    window_seconds: float = 60.0
    backoff_initial_seconds: float = 0.5
    backoff_max_seconds: float = 15.0
    #: Exit codes that mean "retrying cannot help" (jetty core's own exit-2
    #: convention: a config bug, not a crash).
    no_restart_exit: list[int] = dataclasses.field(default_factory=lambda: [2])

    @classmethod
    def parse(cls, raw: object, where: str) -> "RestartConfig":
        t = _Table(raw, where)
        cfg = cls(
            max_restarts=t.take_int("max_restarts", 3, ge=0),
            window_seconds=t.take_number("window_seconds", 60.0, gt=0),
            backoff_initial_seconds=t.take_number(
                "backoff_initial_seconds", 0.5, gt=0
            ),
            backoff_max_seconds=t.take_number("backoff_max_seconds", 15.0, gt=0),
            no_restart_exit=t.take_int_list("no_restart_exit", [2]),
        )
        t.done()
        return cfg


@dataclasses.dataclass
class StopConfig:
    signal: Literal["TERM", "INT", "HUP"] = "TERM"
    grace_seconds: float = 10.0

    @classmethod
    def parse(cls, raw: object, where: str) -> "StopConfig":
        t = _Table(raw, where)
        cfg = cls(
            signal=t.take_choice("signal", ("TERM", "INT", "HUP"), "TERM"),
            grace_seconds=t.take_number("grace_seconds", 10.0, gt=0),
        )
        t.done()
        return cfg


@dataclasses.dataclass
class ServiceConfig:
    cmd: list[str]
    cwd: str | None = None
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Names of services that must be *ready* before this one starts.
    after: list[str] = dataclasses.field(default_factory=list)
    #: Names of gates this service depends on.
    requires: list[str] = dataclasses.field(default_factory=list)
    ready: ReadyConfig = dataclasses.field(default_factory=ReadyConfig)
    restart: RestartConfig = dataclasses.field(default_factory=RestartConfig)
    stop: StopConfig = dataclasses.field(default_factory=StopConfig)

    @classmethod
    def parse(cls, raw: object, where: str) -> "ServiceConfig":
        t = _Table(raw, where)
        cfg = cls(
            cmd=t.take_argv("cmd", min_len=1),
            cwd=t.take_str("cwd"),
            env=t.take_str_dict("env"),
            after=t.take_str_list("after"),
            requires=t.take_str_list("requires"),
            ready=ReadyConfig.parse(t.take_table("ready"), t.at("ready")),
            restart=RestartConfig.parse(t.take_table("restart"), t.at("restart")),
            stop=StopConfig.parse(t.take_table("stop"), t.at("stop")),
        )
        t.done()
        return cfg


@dataclasses.dataclass
class InstanceConfig:
    #: A BASE name: each `up` appends a short random suffix unless pinned
    #: with `--name`.
    name: str
    #: Containment backend. `auto` prefers the strongest available: an owned
    #: cgroup > re-exec into a delegated systemd user scope > plain process
    #: groups. See containment.py for exactly what each provides.
    containment: Literal["auto", "cgroup", "scope", "pgroup"] = "auto"
    #: Default runtime directory for services without their own `cwd`, gate
    #: checks and resolver scripts. Unset = the config file's own directory.
    workdir: str | None = None

    @classmethod
    def parse(cls, raw: object, where: str) -> "InstanceConfig":
        t = _Table(raw, where)
        cfg = cls(
            name=t.take_str("name", required=True),
            containment=t.take_choice(
                "containment", ("auto", "cgroup", "scope", "pgroup"), "auto"
            ),
            workdir=t.take_str("workdir"),
        )
        t.done()
        if not _NAME_RE.match(cfg.name):
            raise ConfigError(
                f"instance.name {cfg.name!r} must match {_NAME_RE.pattern}"
            )
        return cfg


@dataclasses.dataclass
class OrchestratorConfig:
    instance: InstanceConfig
    #: Named ports: "auto", a fixed integer, "8000+" or "8000-8020".
    ports: dict[str, int | str]
    gates: dict[str, GateConfig]
    resolvers: dict[str, ResolverConfig]
    services: dict[str, ServiceConfig]

    @classmethod
    def parse(cls, raw: object) -> "OrchestratorConfig":
        t = _Table(raw, "")
        cfg = cls(
            instance=InstanceConfig.parse(t.take_table("instance"), "instance"),
            ports=t.take_table("ports"),
            gates={
                n: GateConfig.parse(v, f"gates.{n}")
                for n, v in t.take_table("gates").items()
            },
            resolvers={
                n: ResolverConfig.parse(v, f"resolvers.{n}")
                for n, v in t.take_table("resolvers").items()
            },
            services={
                n: ServiceConfig.parse(v, f"services.{n}")
                for n, v in t.take_table("services").items()
            },
        )
        t.done()
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        if not self.services:
            raise ConfigError("at least one [services.<name>] is required")
        for kind, names in (
            ("ports", self.ports),
            ("gates", self.gates),
            ("resolvers", self.resolvers),
            ("services", self.services),
        ):
            for name in names:
                if not _NAME_RE.match(name):
                    raise ConfigError(
                        f"{kind} key {name!r} must match {_NAME_RE.pattern}"
                    )

        fixed: dict[int, str] = {}
        for name, want in self.ports.items():
            if isinstance(want, str) and "{" in want:
                # A templated spec ("{env.HTTP_PORT:-8080+}"): the rendered
                # form is validated at template-check time and again at
                # launch, when the environment is actually consulted.
                continue
            if not isinstance(want, (int, str)) or parse_port_spec(want) is None:
                raise ConfigError(
                    f"ports.{name} = {want!r} is not a valid port spec "
                    '(want "auto", 8000, "8000+" or "8000-8020")'
                )
            if isinstance(want, int):
                if want in fixed:
                    raise ConfigError(
                        f"ports.{name} and ports.{fixed[want]} both fixed to {want}"
                    )
                fixed[want] = name

        provided: dict[str, str] = {}  # bin name -> resolver name
        for rname, resolver in self.resolvers.items():
            if not resolver.provides:
                resolver.provides = [rname]
            for bin_name in resolver.provides:
                if not _NAME_RE.match(bin_name):
                    raise ConfigError(
                        f"resolvers.{rname} provides {bin_name!r}, which must "
                        f"match {_NAME_RE.pattern}"
                    )
                if bin_name in provided:
                    raise ConfigError(
                        f"binary {bin_name!r} is provided by both resolvers "
                        f"{provided[bin_name]!r} and {rname!r}"
                    )
                provided[bin_name] = rname
            if bin_refs(resolver.cmd):
                raise ConfigError(
                    f"resolvers.{rname}: a resolver's own cmd cannot use "
                    "{bin.*} placeholders"
                )
            for gate in resolver.requires:
                if gate not in self.gates:
                    raise ConfigError(
                        f"resolvers.{rname} requires unknown gate {gate!r}"
                    )
        for gname, gate in self.gates.items():
            if bin_refs(gate.check):
                raise ConfigError(
                    f"gates.{gname}: gate commands cannot use {{bin.*}} "
                    "placeholders (gates run before binaries are resolved)"
                )
        for sname, svc in self.services.items():
            for dep in svc.after:
                if dep == sname:
                    raise ConfigError(f"service {sname!r} lists itself in `after`")
                if dep not in self.services:
                    raise ConfigError(
                        f"service {sname!r} is `after` unknown service {dep!r}"
                    )
            for gate in svc.requires:
                if gate not in self.gates:
                    raise ConfigError(
                        f"service {sname!r} requires unknown gate {gate!r}"
                    )
            for bin_name in sorted(service_bin_refs(svc)):
                if bin_name not in provided:
                    raise ConfigError(
                        f"service {sname!r} references {{bin.{bin_name}}} but "
                        "no resolver provides it"
                    )

        start_order(self.services)  # raises on cycles

    @staticmethod
    def load(path: str | Path) -> "OrchestratorConfig":
        return OrchestratorConfig.parse(load_raw(Path(path)))


def load_raw(path: Path, _seen: frozenset[str] = frozenset()) -> dict:
    """Read a config file, following its `extends` chain.

    Single inheritance: a config names at most one parent (`extends =
    "prod.toml"`), though chains are fine (dev → staging → prod). The parent
    path follows the usual rules — relative anchors to THIS file's directory
    and must stay inside its subtree; `~` and absolute paths go anywhere.

    Merge: tables merge recursively and the child wins; scalars and arrays
    replace wholesale (splicing lists by position is a guessing game — a
    child that wants a different `cmd` states the whole cmd). Overriding an
    inherited table with `false` deletes it, so a dev config can drop a
    prod-only service:

        extends = "prod.toml"
        [services]
        metrics = false
    """
    real = os.path.realpath(path)
    if real in _seen:
        raise ConfigError(f"extends cycle involving {path}")
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    extends = raw.pop("extends", None)
    if extends is None:
        return raw
    if not isinstance(extends, str):
        raise ConfigError(f"{path}: extends must be a path string")
    parent = _resolve_extends(extends, str(Path(real).parent))
    return merge_raw(load_raw(Path(parent), _seen | {real}), raw)


def _resolve_extends(value: str, config_dir: str) -> str:
    """The path rules every config path follows, restated here because
    render.py (which owns them for commands and cwds) imports this module."""
    if value.startswith("~"):
        expanded = os.path.expanduser(value)
        if os.path.isabs(expanded):
            value = expanded
    if os.path.isabs(value):
        return value
    base = os.path.realpath(config_dir)
    resolved = os.path.realpath(os.path.join(base, value))
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ConfigError(
            f"extends: relative path {value!r} resolves to {resolved}, outside "
            f"this config's directory ({base}); relative paths may only reach "
            "the config's own subtree — use an absolute path if this is "
            "intentional"
        )
    return resolved


def merge_raw(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if value is False and isinstance(out.get(key), dict):
            out.pop(key, None)  # the delete sentinel for inherited tables
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_raw(out[key], value)
        else:
            out[key] = value
    return out


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
            svc.ready.uds or "",
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
    if want.isdigit():
        # A fixed port as a string — what an env substitution naturally
        # produces (HTTP_PORT=8080 arrives as "8080").
        port = int(want)
        return (port, port) if 1 <= port <= 65535 else None
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
            raise ConfigError(f"`after` dependencies form a cycle among: {cycle}")
        for name in ready:
            del remaining[name]
            order.append(name)
        for deps in remaining.values():
            deps.difference_update(ready)
    return order
