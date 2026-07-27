"""The `reference` module — a complete, minimal implementation of the contract.

It exists for three reasons, and is worth keeping even once real modules land:

1. It is the worked example for anyone writing a module: every part of
   `Module` is exercised here in about eighty lines.
2. It gives the core's own tests something to mount that has no upstream and no
   dependencies, so a core regression cannot hide behind a module's complexity.
3. It is the conformance suite's fixture for the module-lifecycle rules —
   enable/disable, `/v1/meta` advertisement, readiness propagation, and the
   `404 module_disabled` behaviour of SPEC.md §4.4.

It intentionally holds no state and talks to nothing.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from jetty.errors import ErrorCode, JettyError
from jetty.modules.base import Module, Readiness


class EchoRequest(BaseModel):
    # SPEC.md §6: unknown request fields are an error, never ignored.
    model_config = ConfigDict(extra="forbid")

    message: str


class ReferenceModule(Module):
    name = "reference"
    api_version = "v1"
    #: Not required for readiness: a demo module must never be able to take a
    #: host out of rotation.
    required = False

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        #: Lets tests and the conformance suite drive the /readyz path without
        #: needing a real upstream to break.
        self._healthy: bool = bool(settings.get("healthy", True))

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/echo")
        async def echo(body: EchoRequest) -> dict[str, str]:
            return {"message": body.message}

        @router.get("/boom")
        async def boom() -> dict[str, str]:
            """Deliberate failure, for asserting the error envelope end to end."""
            raise JettyError(
                ErrorCode.UPSTREAM_UNAVAILABLE, "reference module: deliberate failure"
            )

        return router

    async def ready(self) -> Readiness:
        return Readiness.up() if self._healthy else Readiness.down()
