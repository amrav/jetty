"""`jetty-orc` entrypoint.

Exit codes follow jetty core's convention:

  0  success / clean shutdown
  1  runtime failure (instance failed, kill timed out, …)
  2  configuration or usage error — not restartable

Subcommands:

  up      run an instance in the foreground (Ctrl-C stops the whole tree)
  check   validate a config without spawning anything
  ls      list known instances with health, ports and resource usage
  status  per-service detail for one instance
  kill    stop an instance ( --force = kernel-level kill of the whole tree)
  doctor  report what this host offers and which containment `auto` would pick
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import signal
import sys
import time
from pathlib import Path

from . import console, containment, procfs
from .config import OrchestratorConfig, start_order
from .registry import Registry, default_root, supervisor_alive
from .render import validate_templates
from .supervisor import Supervisor

#: Like config's name rule but a little longer: base name + random suffix.
_EXACT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,68}$")


def _fail(message: str, code: int = 2) -> None:
    print(f"jetty-orc: {message}", file=sys.stderr)
    raise SystemExit(code)


def _load_config(path_str: str) -> OrchestratorConfig:
    path = Path(path_str)
    if not path.is_file():
        _fail(f"config file not found: {path}")
    try:
        config = OrchestratorConfig.load(path)
        validate_templates(config, str(path.resolve().parent))
    except Exception as e:  # noqa: BLE001 - surface any config problem as exit 2
        _fail(f"invalid config: {e}")
    return config


def _self_argv() -> list[str]:
    """Reconstruct how to re-invoke this CLI (for the scope re-exec), without
    hardcoding a module path — the package may live at a different import
    path when vendored into another build system, or be a bare directory
    run as `python3 <dir>`."""
    import __main__

    spec = getattr(__main__, "__spec__", None)
    if spec and spec.name and spec.name != "__main__":
        mod = spec.name.removesuffix(".__main__")
        return [sys.executable, "-m", mod, *sys.argv[1:]]
    # A console script, or `python3 <dir>` on a bare copy: argv[0] is the
    # thing to re-run — directly if it is an executable file, else through
    # the interpreter (a directory passes X_OK but is not executable).
    if os.path.isfile(sys.argv[0]) and os.access(sys.argv[0], os.X_OK):
        return list(sys.argv)
    return [sys.executable, *sys.argv]


def _resolve_instance(registry: Registry, query: str) -> dict:
    """Exact instance name, or an unambiguous base-name prefix — so
    `jetty-orc logs sf-dev` finds `sf-dev-a3f1` when it's the only one."""
    record = registry.load(query)
    if record is not None:
        return record
    matches = [
        r for r in registry.load_all() if r["name"].startswith(query + "-")
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        names = ", ".join(sorted(r["name"] for r in matches))
        _fail(f"{query!r} is ambiguous: {names}", code=1)
    _fail(f"no instance named {query!r}", code=1)


def _human_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def _human_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600}h"


def _instance_pids(record: dict) -> list[int]:
    """Every pid of an instance, containment-appropriately."""
    cont = record.get("containment", {})
    if cont.get("kind") == "cgroup" and cont.get("root"):
        return procfs.cgroup_procs(Path(cont["root"]))
    sids = {
        s["pid"]
        for s in record.get("services", {}).values()
        if s.get("pid")
    }
    pids = procfs.session_members(sids)
    sup = record.get("supervisor_pid")
    if sup and procfs.alive(sup):
        pids.append(sup)
    return pids


def _instance_usage(record: dict) -> tuple[int | None, "_CpuSample"]:
    cont = record.get("containment", {})
    if cont.get("kind") == "cgroup" and cont.get("root"):
        root = Path(cont["root"])
        return procfs.cgroup_mem_bytes(root), _CpuSample(usec=procfs.cgroup_cpu_usec(root))
    pids = _instance_pids(record)
    return (
        procfs.rss_bytes(pids) if pids else 0,
        _CpuSample(ticks=procfs.cpu_ticks(pids)),
    )


class _CpuSample:
    def __init__(self, usec: int | None = None, ticks: int | None = None):
        self.usec = usec
        self.ticks = ticks

    def percent_since(self, earlier: "_CpuSample", dt: float) -> str:
        if dt <= 0:
            return "-"
        if self.usec is not None and earlier.usec is not None:
            return f"{(self.usec - earlier.usec) / (dt * 1e6) * 100:.1f}"
        if self.ticks is not None and earlier.ticks is not None:
            secs = (self.ticks - earlier.ticks) / procfs.CLK_TCK
            return f"{secs / dt * 100:.1f}"
        return "-"


def _services_summary(record: dict) -> str:
    services = record.get("services", {})
    if not services:
        return "-"
    states = [s.get("state") for s in services.values()]
    if all(s == "running" for s in states):
        return f"{len(states)}/{len(states)} running"
    parts = []
    for name, svc in services.items():
        state = svc.get("state")
        if state == "blocked" and svc.get("blocked_on"):
            state = f"blocked({','.join(svc['blocked_on'])})"
        parts.append(f"{name}:{state}")
    return " ".join(parts)


# --- subcommands ---


def _cmd_up(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    root = Path(args.root) if args.root else default_root()
    registry = Registry(root)
    # `instance.name` is a BASE name: each `up` appends a short random suffix
    # so the same config can run several instances concurrently. `--name`
    # pins the exact name instead — the way to get a stable identity (and a
    # stable {state_dir}) across restarts.
    if args.name:
        if not _EXACT_NAME_RE.match(args.name):
            _fail(f"--name {args.name!r} must match {_EXACT_NAME_RE.pattern}")
        name = args.name
    else:
        for _ in range(20):
            name = f"{config.instance.name}-{secrets.token_hex(2)}"
            if registry.load(name) is None:
                break
    config.instance.name = name
    record = registry.load(name)
    if record and supervisor_alive(record):
        _fail(
            f"instance {name!r} is already running "
            f"(supervisor pid {record['supervisor_pid']}); "
            f"`jetty-orc kill {name}` first, or use a different --name",
            code=1,
        )
    try:
        backend = containment.acquire(
            # Pin the chosen name across the scope re-exec, or the re-entered
            # process would roll a fresh suffix.
            config.instance.containment,
            name,
            [*_self_argv(), "--name", name],
        )  # may re-exec under systemd-run and not return
    except containment.ContainmentError as e:
        _fail(str(e), code=1)
    rc = asyncio.run(
        Supervisor(
            config,
            root=root,
            containment=backend,
            config_path=str(Path(args.config).resolve()),
            quiet=args.quiet,
        ).run()
    )
    raise SystemExit(rc)


def _cmd_logs(args: argparse.Namespace) -> None:
    root = Path(args.root) if args.root else default_root()
    record = _resolve_instance(Registry(root), args.name)
    logs_dir = Path(record.get("logs_dir") or "")
    if not logs_dir.is_dir():
        _fail(f"no logs directory recorded for {args.name!r}", code=1)

    def discover() -> list[Path]:
        return sorted(logs_dir.glob("*.log"))

    files = discover()
    if not files and not args.follow:
        _fail(f"no service logs yet in {logs_dir}", code=1)
    # Every service name is known from the record up front, so the prefix
    # column width and colours stay stable even for a service whose log file
    # only appears mid-tail.
    names = list(record.get("services") or {}) or [f.stem for f in files]
    prefixer = console.Prefixer(
        sorted(set(names) | {f.stem for f in files}),
        console.want_color(sys.stdout),
    )

    def print_tail(path: Path) -> int:
        """Last `--lines` lines, prefixed; returns the offset tailing should
        continue from (the end of the file)."""
        data = path.read_bytes()
        for line in data.splitlines()[-args.lines :]:
            print(prefixer.format(path.stem, line.decode(errors="replace")))
        return len(data)

    offsets = {path: print_tail(path) for path in files}
    if not args.follow:
        return
    buffers = {path: console.LineBuffer() for path in files}
    try:
        while True:
            time.sleep(0.3)
            for path in discover():
                if path not in offsets:  # a service spawned after we started
                    offsets[path] = 0
                    buffers[path] = console.LineBuffer()
                try:
                    size = path.stat().st_size
                    if size <= offsets[path]:
                        continue
                    with open(path, "rb") as f:
                        f.seek(offsets[path])
                        chunk = f.read()
                except OSError:
                    continue
                offsets[path] += len(chunk)
                for line in buffers[path].feed(chunk):
                    print(prefixer.format(path.stem, line), flush=True)
    except KeyboardInterrupt:
        return


def _cmd_check(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    order = start_order(config.services)
    print(f"jetty-orc: config OK; instance {config.instance.name!r}")
    print(f"  containment: {config.instance.containment}")
    print(f"  ports: {', '.join(f'{k}={v}' for k, v in config.ports.items()) or '(none)'}")
    print(f"  gates: {', '.join(config.gates) or '(none)'}")
    print(f"  start order: {' -> '.join(order)}")


def _cmd_ls(args: argparse.Namespace) -> None:
    root = Path(args.root) if args.root else default_root()
    records = Registry(root).load_all()
    if not records:
        print("no instances")
        return
    first = {r["name"]: _instance_usage(r) for r in records}
    t0 = time.monotonic()
    time.sleep(0.3)
    dt = time.monotonic() - t0
    rows = [
        ("NAME", "STATE", "PID", "UPTIME", "PROCS", "MEM", "CPU%", "PORTS", "SERVICES")
    ]
    for record in records:
        name = record["name"]
        alive = supervisor_alive(record)
        state = record.get("state", "?")
        if not alive:
            state = f"dead ({state})" if state.startswith("failed") else "dead"
        mem, cpu1 = _instance_usage(record)
        _mem0, cpu0 = first[name]
        rows.append(
            (
                name,
                state,
                str(record.get("supervisor_pid", "-")) if alive else "-",
                _human_age(time.time() - record.get("created", time.time()))
                if alive
                else "-",
                str(len(_instance_pids(record))) if alive else "0",
                _human_bytes(mem if alive else None),
                cpu1.percent_since(cpu0, dt) if alive else "-",
                " ".join(f"{k}={v}" for k, v in record.get("ports", {}).items()) or "-",
                _services_summary(record),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())


def _cmd_status(args: argparse.Namespace) -> None:
    root = Path(args.root) if args.root else default_root()
    record = _resolve_instance(Registry(root), args.name)
    alive = supervisor_alive(record)
    print(f"instance:    {record['name']}")
    print(f"state:       {record.get('state')}{'' if alive else '  (supervisor dead)'}")
    print(f"supervisor:  pid {record.get('supervisor_pid')}")
    print(f"containment: {record.get('containment', {}).get('kind')}"
          f" {record.get('containment', {}).get('root') or ''}".rstrip())
    print(f"config:      {record.get('config_path')}")
    print(f"state dir:   {record.get('state_dir')}")
    ports = record.get("ports", {})
    print(f"ports:       {', '.join(f'{k}={v}' for k, v in ports.items()) or '(none)'}")
    print("services:")
    for name, svc in record.get("services", {}).items():
        line = f"  {name:<20} {svc.get('state'):<10}"
        if svc.get("pid"):
            line += f" pid {svc['pid']}"
        if svc.get("restarts"):
            line += f" restarts {svc['restarts']}"
        if svc.get("last_exit") is not None:
            line += f" last_exit {svc['last_exit']}"
        if svc.get("blocked_on"):
            line += f" blocked_on {','.join(svc['blocked_on'])}"
        print(line)
    resolvers = record.get("resolvers") or {}
    if resolvers:
        print("binaries:")
        for rname, st in resolvers.items():
            copied_from = st.get("copied_from", {})
            for bname, path in st.get("binaries", {}).items():
                line = f"  {bname:<20} {path}  (via {rname}"
                if bname in copied_from:
                    line += f", copied from {copied_from[bname]}"
                print(line + ")")
    print(f"logs:        {record.get('logs_dir')}/<service>.log")


def _cmd_kill(args: argparse.Namespace) -> None:
    root = Path(args.root) if args.root else default_root()
    registry = Registry(root)
    record = _resolve_instance(registry, args.name)
    name = record["name"]
    if not supervisor_alive(record):
        registry.remove(name)
        print(f"jetty-orc: instance {name!r} was already dead "
              f"(state: {record.get('state')}); removed registry entry")
        return
    pid = record["supervisor_pid"]
    if args.force:
        cont = record.get("containment", {})
        service_pids = [
            s["pid"] for s in record.get("services", {}).values() if s.get("pid")
        ]
        containment.external_kill(cont.get("kind"), cont.get("root"), service_pids)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if not supervisor_alive(record):
            if registry.load(name) is not None:
                registry.remove(name)
            print(f"jetty-orc: instance {name!r} stopped")
            return
        time.sleep(0.2)
    _fail(
        f"instance {name!r} did not stop within {args.timeout:.0f}s; "
        "try `jetty-orc kill --force`",
        code=1,
    )


def _service_pids(record: dict, sname: str) -> list[int]:
    cont = record.get("containment", {})
    if cont.get("kind") == "cgroup" and cont.get("root"):
        return procfs.cgroup_procs(Path(cont["root"]) / f"svc-{sname}")
    pid = record.get("services", {}).get(sname, {}).get("pid")
    return procfs.session_members({pid}) if pid else []


def _cmd_ps(args: argparse.Namespace) -> None:
    """The full process tree, service by service — every pid the containment
    can enumerate, which under cgroup mode is every pid there is. Rendered
    pstree-style, with the same per-service colours as `logs` and `up`."""
    root = Path(args.root) if args.root else default_root()
    record = _resolve_instance(Registry(root), args.name)
    alive = supervisor_alive(record)
    cont = record.get("containment", {})
    services = list(record.get("services", {}))
    prefixer = console.Prefixer(services, console.want_color(sys.stdout))
    print(
        f"instance {record['name']} — supervisor pid "
        f"{record.get('supervisor_pid')}{'' if alive else ' (dead)'}, "
        f"containment {cont.get('kind')}"
        + (f" ({cont.get('root')})" if cont.get("root") else "")
    )
    for sname in services:
        pids = set(_service_pids(record, sname))
        print(prefixer.label(sname) + ("" if pids else "  (no processes)"))
        children: dict[int | None, list[int]] = {}
        for pid in sorted(pids):
            parent = procfs.ppid(pid)
            children.setdefault(parent if parent in pids else None, []).append(pid)

        def render(nodes: list[int], prefix: str) -> None:
            for i, pid in enumerate(nodes):
                last = i == len(nodes) - 1
                rss = _human_bytes(procfs.rss_bytes([pid]))
                cmd = procfs.cmdline(pid)
                print(f" {prefix}{'└─ ' if last else '├─ '}{pid:<7} {rss:>9}  {cmd}")
                render(children.get(pid, []), prefix + ("   " if last else "│  "))

        render(children.get(None, []), "")


def _cmd_doctor(args: argparse.Namespace) -> None:
    del args
    uid = os.getuid()

    def report(label: str, ok: bool, detail: str = "") -> bool:
        mark = "ok" if ok else "--"
        print(f"  [{mark}] {label}" + (f": {detail}" if detail else ""))
        return ok

    print(f"jetty-orc doctor (kernel {os.uname().release}, python "
          f"{sys.version.split()[0]}, uid {uid})")
    has_cg2 = report(
        "cgroup v2 unified hierarchy",
        (procfs.CGROUP_FS / "cgroup.controllers").is_file(),
    )
    self_dir = procfs.cgroup_self_dir()
    report("current cgroup", self_dir is not None, str(self_dir))
    owned = containment._owned_cgroup() is not None if has_cg2 else False
    report(
        "current cgroup usable as containment root (writable, exclusive)", owned
    )
    if self_dir is not None:
        report(
            "cgroup.kill supported (kernel >= 5.14)",
            (self_dir / "cgroup.kill").is_file(),
        )
    sysd = containment._systemd_user_available()
    report("systemd user manager reachable (for scope re-exec)", sysd)
    delegated = ""
    try:
        delegated = (
            procfs.CGROUP_FS
            / "user.slice"
            / f"user-{uid}.slice"
            / f"user@{uid}.service"
            / "cgroup.controllers"
        ).read_text().strip()
    except OSError:
        pass
    report("controllers delegated to user manager", bool(delegated), delegated)

    if owned:
        verdict = "cgroup (already in an owned, delegated cgroup)"
    elif sysd:
        verdict = "scope (re-exec via systemd-run --user --scope -p Delegate=yes)"
    else:
        verdict = "pgroup (process groups only — setsid'ing grandchildren can escape)"
    print(f"  containment \"auto\" would use: {verdict}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jetty-orc", description=__doc__)
    parser.add_argument(
        "--root",
        help="state root (default: $JETTY_ORC_ROOT or ~/.local/state/jetty-orc)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="run an instance in the foreground")
    p_up.add_argument("--config", "-c", required=True, help="path to instance TOML")
    p_up.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="don't echo service output to the console (logs still written)",
    )
    p_up.add_argument(
        "--name",
        help="exact instance name (default: instance.name plus a random "
        "suffix, so one config can run several instances)",
    )
    p_up.set_defaults(func=_cmd_up)

    p_logs = sub.add_parser(
        "logs", help="show (or follow) an instance's service logs, prefixed"
    )
    p_logs.add_argument("name")
    p_logs.add_argument("--follow", "-f", action="store_true", help="keep tailing")
    p_logs.add_argument(
        "--lines", "-n", type=int, default=20, help="initial lines per service"
    )
    p_logs.set_defaults(func=_cmd_logs)

    p_check = sub.add_parser("check", help="validate a config, spawn nothing")
    p_check.add_argument("--config", "-c", required=True)
    p_check.set_defaults(func=_cmd_check)

    p_ls = sub.add_parser("ls", help="list instances")
    p_ls.set_defaults(func=_cmd_ls)

    p_status = sub.add_parser("status", help="per-service detail for one instance")
    p_status.add_argument("name")
    p_status.set_defaults(func=_cmd_status)

    p_ps = sub.add_parser(
        "ps", help="full process tree of an instance, service by service"
    )
    p_ps.add_argument("name")
    p_ps.set_defaults(func=_cmd_ps)

    p_kill = sub.add_parser("kill", help="stop an instance")
    p_kill.add_argument("name")
    p_kill.add_argument(
        "--force", action="store_true", help="kernel-level kill of the whole tree"
    )
    p_kill.add_argument("--timeout", type=float, default=20.0)
    p_kill.set_defaults(func=_cmd_kill)

    p_doc = sub.add_parser("doctor", help="report host capabilities")
    p_doc.set_defaults(func=_cmd_doctor)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
