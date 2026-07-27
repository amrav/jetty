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

    @abc.abstractmethod
    def router(self) -> APIRouter:
        """Endpoints, mounted under `/{name}/{api_version}`."""

    def meta(self) -> dict[str, Any]:
        """This module's entry in `GET /v1/meta` (SPEC.md §4.2)."""
        return {
            "name": self.name,
            "api_version": self.api_version,
            "mount": self.mount,
        }

    async def startup(self) -> None:
        """Optional: open pools, load keys. Raising here aborts boot."""

    async def shutdown(self) -> None:
        """Optional: release resources. MUST NOT raise."""
