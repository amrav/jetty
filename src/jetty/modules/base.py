"""The module contract (SPEC.md §5).

Everything Jetty can do is a module. The core is a shell: it owns config
loading, listeners, the error envelope, and `/healthz` `/readyz` `/v1/meta` —
and knows nothing about auth, LLMs, filesystems or anything else.

A module supplies four things:

  name          the mount segment and the key in config and /v1/meta
  api_version   versioned independently of every other module (SPEC.md §5)
  router        its endpoints, mounted at /{name}/{api_version}
  ready()       an upstream reachability check for /readyz

That is the entire surface. If a capability cannot be expressed through it, the
right move is to extend this contract deliberately rather than to reach around
it — a module that pokes at core internals is one that cannot be disabled
cleanly, and disabling cleanly is the point.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import APIRouter

from jetty.errors import ErrorCode


@dataclass(frozen=True)
class Readiness:
    """One module's answer for `/readyz` (SPEC.md §4.2).

    `detail` is constrained to an ErrorCode rather than free text, because the
    spec forbids leaking internal topology through the probe — and because
    clients parse whatever you give them, so free text becomes an accidental
    contract.
    """

    ready: bool
    detail: ErrorCode | None = None

    @staticmethod
    def up() -> "Readiness":
        return Readiness(ready=True, detail=None)

    @staticmethod
    def down(detail: ErrorCode = ErrorCode.UPSTREAM_UNAVAILABLE) -> "Readiness":
        return Readiness(ready=False, detail=detail)


class Module(abc.ABC):
    """Base class for every Jetty module."""

    #: Mount segment, config key, and `/v1/meta` name. Lowercase, no slashes.
    name: str
    #: Per-module API version — the second path segment. SPEC.md §5.
    api_version: str = "v1"
    #: Whether a failed `ready()` should fail the whole `/readyz` probe.
    #: Modules that are nice-to-have (the LLM proxy) set this False so that
    #: their outage does not take a host out of rotation for everything else.
    required: bool = True

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self.settings = settings

    @property
    def mount(self) -> str:
        return f"/{self.name}"

    @abc.abstractmethod
    def router(self) -> APIRouter:
        """Endpoints, mounted under `/{name}/{api_version}`."""

    async def ready(self) -> Readiness:
        """Live upstream reachability check.

        Default is "up": a module with no upstream (a pure-local one) is ready
        as soon as it is mounted. Any module that talks to something MUST
        override this — the default is a fail-OPEN answer and is only safe
        because it is trivially visible in a subclass that has no override.
        """
        return Readiness.up()

    def meta(self) -> dict[str, Any]:
        """This module's entry in `GET /v1/meta` (SPEC.md §4.3)."""
        return {
            "name": self.name,
            "api_version": self.api_version,
            "mount": self.mount,
            "required": self.required,
        }

    async def startup(self) -> None:
        """Optional: open pools, load keys. Raising here aborts boot."""

    async def shutdown(self) -> None:
        """Optional: release resources. MUST NOT raise."""
