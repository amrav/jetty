"""Configuration loading and validation.

TOML in, validated model out. Invariants that would be a security problem if
misconfigured are checked HERE, at boot, so the process refuses to start rather
than running in a subtly wrong state (SPEC.md §1.2 applied to config).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    """Reject unknown keys everywhere (SPEC.md §6 applied to config).

    A typo in a config key must not silently fall back to a default. Same
    argument as rejecting unknown request fields: an ignored `require_identity`
    is a security bug, not a nit.
    """

    model_config = ConfigDict(extra="forbid")


class ListenerConfig(Strict):
    """Where the control listener binds.

    Exactly one of `uds` or `tcp` (SPEC.md §2.1). UDS is the default because
    the socket file's permissions are then the access control. The protocol
    defines no transport authentication for TCP (SPEC.md §1.5), so a TCP
    listener is reachable by any process that can reach the bound address.
    """

    uds: str | None = "/run/jetty/jetty.sock"
    tcp: str | None = None
    #: SPEC.md §1.5 — the socket must not be reachable by other users.
    uds_mode: int = 0o660
    #: Refuse to bind a non-loopback address unless this is explicit.
    allow_remote: bool = False

    @model_validator(mode="after")
    def _check(self) -> "ListenerConfig":
        if bool(self.uds) == bool(self.tcp):
            raise ValueError("configure exactly one of listener.uds or listener.tcp")

        if self.tcp:
            host = self.tcp.rsplit(":", 1)[0].strip("[]")
            loopback = host in {"127.0.0.1", "::1", "localhost"}
            if not loopback and not self.allow_remote:
                raise ValueError(
                    f"listener.tcp binds non-loopback host {host!r}; "
                    "set listener.allow_remote = true to confirm this is intended"
                )
            if self.uds_mode != 0o660:
                raise ValueError("listener.uds_mode is meaningless with a TCP listener")

        if self.uds and self.uds_mode & 0o007:
            raise ValueError(
                f"listener.uds_mode {self.uds_mode:#o} grants access to other users; "
                "SPEC.md §1.5 requires 0660 or tighter"
            )
        return self


class LogConfig(Strict):
    level: Literal["debug", "info", "warning", "error"] = "info"


class Config(Strict):
    listener: ListenerConfig = Field(default_factory=ListenerConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    #: [modules.<name>] tables, passed through to each module unvalidated by
    #: core — a module owns its own settings schema.
    modules: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @staticmethod
    def load(path: str | Path) -> "Config":
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return Config.model_validate(raw)
