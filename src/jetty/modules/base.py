"""The module contract (SPEC.md §5).

Everything Jetty can do is a module. The core is a shell: it owns config
loading, listeners, the error envelope, and `/healthz` and `/v1/meta` — and
knows nothing about auth, LLMs, filesystems or anything else.

A module supplies three things:

  name          the mount segment and the key in config and /v1/meta
  api_version   versioned independently of every other module (SPEC.md §5)
  router        its endpoints, mounted at /{name}/{api_version}

That is the entire surface. If a capability cannot be expressed through it, the
right move is to extend this contract deliberately rather than to reach around
it — a module that pokes at core internals is one that cannot be disabled
cleanly, and disabling cleanly is the point.
"""

from __future__ import annotations

import abc
from typing import Any, Mapping

from fastapi import APIRouter


class Module(abc.ABC):
    """Base class for every Jetty module."""

    #: Mount segment, config key, and `/v1/meta` name. Lowercase, no slashes.
    name: str
    #: Per-module API version — the second path segment. SPEC.md §5.
    api_version: str = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self.settings = settings

    @property
    def mount(self) -> str:
        return f"/{self.name}"

    @property
    def router_prefix(self) -> str:
        """Where the core mounts `router()`.

        The default is the SPEC.md §5 layout. A module serving a foreign
        protocol under its mount prefix (SPEC.md §2.1) overrides this to
        `self.mount` and lays out the foreign URL space — including its own
        version segments — inside its router.
        """
        return f"{self.mount}/{self.api_version}"

    @abc.abstractmethod
    def router(self) -> APIRouter:
        """Endpoints, mounted under `router_prefix`."""

    def meta(self) -> dict[str, Any]:
        """This module's entry in `GET /v1/meta` (SPEC.md §4.2)."""
        return {
            "name": self.name,
            "api_version": self.api_version,
            "mount": self.mount,
        }

    def listener_app(self) -> Any | None:
        """SPEC.md §2.1: a module MAY declare its own listener for a foreign
        URL space that cannot be prefix-mounted on the control listener.

        Return the ASGI app to serve there, or None (the default). A module
        returning one must also provide `listener_bind`, and overrides
        `meta()` to advertise the listener URL (SPEC.md §4.2).
        """
        return None

    @property
    def listener_bind(self) -> tuple[str, int] | None:
        """(host, port) for `listener_app`; None when the module has none."""
        return None

    async def startup(self) -> None:
        """Optional: open pools, load keys. Raising here aborts boot."""

    async def shutdown(self) -> None:
        """Optional: release resources. MUST NOT raise."""
