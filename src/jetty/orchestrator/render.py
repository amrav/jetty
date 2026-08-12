"""Placeholder rendering: `{ports.api}`, `{instance.name}`, `{state_dir}`, `{logs_dir}`.

Ports are allocated at launch, so commands, environments, probes and gate
argvs are templates rendered against a context built after allocation. An
unknown placeholder is an error — a `{ports.apii}` typo must fail loudly, not
reach the child verbatim. Literal braces are written `{{` and `}}`.
"""

from __future__ import annotations

import dataclasses
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


@dataclasses.dataclass(frozen=True)
class RenderedService:
    cmd: list[str]
    cwd: str | None
    env: dict[str, str]
    ready_http: str | None
    ready_tcp: str | None
    ready_path: str | None


def render_service(svc: ServiceConfig, ctx: dict[str, str]) -> RenderedService:
    return RenderedService(
        cmd=[render_str(a, ctx) for a in svc.cmd],
        cwd=render_str(svc.cwd, ctx) if svc.cwd is not None else None,
        env={k: render_str(v, ctx) for k, v in svc.env.items()},
        ready_http=render_str(svc.ready.http, ctx) if svc.ready.http else None,
        ready_tcp=render_str(svc.ready.tcp, ctx) if svc.ready.tcp else None,
        ready_path=render_str(svc.ready.path, ctx) if svc.ready.path else None,
    )


def render_gate_argv(gate: GateConfig, ctx: dict[str, str]) -> list[str]:
    return [render_str(a, ctx) for a in gate.check]


def validate_templates(config: OrchestratorConfig) -> None:
    """Render everything against a dummy context so `check` (and `up`, before
    any process is spawned) catches placeholder typos."""
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
            render_service(svc, ctx)
        for gate in config.gates.values():
            render_gate_argv(gate, ctx)
        for resolver in config.resolvers.values():
            for arg in resolver.cmd:
                render_str(arg, ctx)
    except RenderError as e:
        raise ValueError(str(e)) from None
