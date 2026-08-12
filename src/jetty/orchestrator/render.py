"""Placeholder rendering: `{ports.api}`, `{instance.name}`, `{state_dir}`, `{logs_dir}`.

Ports are allocated at launch, so commands, environments, probes and gate
argvs are templates rendered against a context built after allocation. An
unknown placeholder is an error — a `{ports.apii}` typo must fail loudly, not
reach the child verbatim. Literal braces are written `{{` and `}}`.
"""

from __future__ import annotations

import dataclasses
import os
import string

from .config import GateConfig, OrchestratorConfig, ServiceConfig


class RenderError(ValueError):
    pass


class _Formatter(string.Formatter):
    """str.format's parser (so `{{`/`}}` and `}}}` disambiguate the way every
    Python user already expects), but with the whole field name — dots
    included — treated as a flat context key instead of attribute access."""

    def __init__(self, ctx: dict[str, str]):
        super().__init__()
        self._ctx = ctx

    def get_field(self, field_name, args, kwargs):
        if field_name not in self._ctx:
            known = ", ".join(sorted(self._ctx))
            raise RenderError(
                f"unknown placeholder {{{field_name}}} (known: {known})"
            )
        return self._ctx[field_name], field_name


def build_context(
    instance_name: str, ports: dict[str, int], state_dir: str, logs_dir: str
) -> dict[str, str]:
    ctx = {
        "instance.name": instance_name,
        "state_dir": state_dir,
        "logs_dir": logs_dir,
    }
    for name, port in ports.items():
        ctx[f"ports.{name}"] = str(port)
    return ctx


def render_str(template: str, ctx: dict[str, str]) -> str:
    try:
        return _Formatter(ctx).vformat(template, (), {})
    except RenderError:
        raise
    except (ValueError, IndexError, KeyError) as e:
        raise RenderError(f"bad template {template!r}: {e}") from None


def resolve_config_path(value: str, config_dir: str, what: str) -> str:
    """Resolve a relative config path against the TOML file's directory,
    confined to its subtree.

    Relative paths make a config portable — but only if they mean the same
    thing wherever the supervisor is launched from, so they anchor to the
    config file, not the process's cwd. And they may only reach *into* the
    config's own tree: `../sibling/thing` silently depends on where the
    config happens to be checked out, which is exactly the fragility relative
    paths are supposed to avoid. Anything outside the tree must be spelled
    absolutely — that makes the dependency visible and deliberate.

    realpath on both sides, so a symlink inside the tree pointing out of it
    counts as outside.
    """
    if os.path.isabs(value):
        return value
    base = os.path.realpath(config_dir)
    resolved = os.path.realpath(os.path.join(base, value))
    if resolved != base and not resolved.startswith(base + os.sep):
        raise RenderError(
            f"{what}: relative path {value!r} resolves to {resolved}, outside "
            f"the config file's directory ({base}); relative paths may only "
            "reach the config's own subtree — use an absolute path if this "
            "is intentional"
        )
    return resolved


def resolve_command(argv: list[str], config_dir: str, what: str) -> list[str]:
    """Config-relative resolution for a command's argv[0], shell-style: a
    bare name (`python`, `npm`) is a PATH lookup and passes through; anything
    with a slash is a path and gets the config-relative treatment. Later argv
    elements are opaque — the orchestrator cannot know which of them are
    paths."""
    if "/" in argv[0]:
        return [resolve_config_path(argv[0], config_dir, what), *argv[1:]]
    return argv


@dataclasses.dataclass(frozen=True)
class RenderedService:
    cmd: list[str]
    cwd: str | None
    env: dict[str, str]
    ready_http: str | None
    ready_tcp: str | None
    ready_path: str | None


def render_service(
    svc: ServiceConfig, ctx: dict[str, str], config_dir: str
) -> RenderedService:
    cmd = resolve_command(
        [render_str(a, ctx) for a in svc.cmd], config_dir, "cmd"
    )
    # The runtime directory: explicit and relative -> config-relative
    # (confined); explicit and absolute -> anywhere; unset -> the config's
    # own directory, so a config means the same thing however it is launched.
    cwd = render_str(svc.cwd, ctx) if svc.cwd is not None else config_dir
    cwd = resolve_config_path(cwd, config_dir, "cwd")
    ready_path = render_str(svc.ready.path, ctx) if svc.ready.path else None
    if ready_path is not None:
        ready_path = resolve_config_path(ready_path, config_dir, "ready.path")
    return RenderedService(
        cmd=cmd,
        cwd=cwd,
        env={k: render_str(v, ctx) for k, v in svc.env.items()},
        ready_http=render_str(svc.ready.http, ctx) if svc.ready.http else None,
        ready_tcp=render_str(svc.ready.tcp, ctx) if svc.ready.tcp else None,
        ready_path=ready_path,
    )


def render_gate_argv(
    gate: GateConfig, ctx: dict[str, str], config_dir: str
) -> list[str]:
    return resolve_command(
        [render_str(a, ctx) for a in gate.check], config_dir, "gate check"
    )


def validate_templates(config: OrchestratorConfig, config_dir: str) -> None:
    """Render everything against a dummy context so `check` (and `up`, before
    any process is spawned) catches placeholder typos — and, because dummy
    values are absolute, any confinement violation in a STATIC relative path
    surfaces here too."""
    ctx = build_context(
        config.instance.name,
        {name: 1 for name in config.ports},
        state_dir="/dev/null",
        logs_dir="/dev/null",
    )
    for resolver in config.resolvers.values():
        for provided in resolver.provides:
            ctx[f"bin.{provided}"] = "/dev/null"
    try:
        for svc in config.services.values():
            render_service(svc, ctx, config_dir)
        for gname, gate in config.gates.items():
            render_gate_argv(gate, ctx, config_dir)
        for rname, resolver in config.resolvers.items():
            resolve_command(
                [render_str(a, ctx) for a in resolver.cmd],
                config_dir,
                f"resolvers.{rname} cmd",
            )
    except RenderError as e:
        raise ValueError(str(e)) from None
