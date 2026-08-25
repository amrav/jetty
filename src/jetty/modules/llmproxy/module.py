"""The llmproxy module (llmproxy-v1): a transparent proxy for provider APIs.

The proxied surfaces live on a module-declared listener (llmproxy-v1 §1) —
their URL layouts mirror the providers' and cannot be prefix-mounted on the
control listener. `listener_app()` is that surface app; the core binds the
address and serves it. The control plane in `router()` (capabilities, usage)
stays on the control listener and conforms to SPEC.md §3.

Each configured surface is either `passthrough` (forward.py: verbatim in both
directions, jetty owns the credential) or `mock` (mock_gemini.py: a
deterministic emulator, no network I/O). This build ships the `gemini`
surface; configuring one it does not ship fails at boot rather than serving a
subset silently.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Literal, Mapping

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jetty.modules.base import Module

log = logging.getLogger("jetty.llmproxy")

#: Surfaces this build ships, their prefixes, and their providers' bases.
_SHIPPED = {"gemini": "/genai"}
_KNOWN_SURFACES = ("gemini", "openai", "anthropic")
_DEFAULT_UPSTREAM = {
    "gemini": "https://generativelanguage.googleapis.com",
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
}


def _parse_bind(listener: str) -> tuple[str, int]:
    host, sep, port = listener.rpartition(":")
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"llmproxy.listener must be host:port, not {listener!r}")
    return host.strip("[]") or "127.0.0.1", int(port)


class SurfaceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["passthrough", "mock"] = "passthrough"
    upstream: str = ""  # "" = the provider's public base URL
    api_key: str = ""


class LlmProxySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    listener: str = "127.0.0.1:7242"
    allow_remote: bool = False
    surfaces: dict[str, SurfaceSettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "LlmProxySettings":
        if not self.surfaces:
            # llmproxy-v1 §2: a proxy with no surfaces answers nothing and is
            # always a config mistake.
            raise ValueError("llmproxy.surfaces must configure at least one surface")
        for name, surface in self.surfaces.items():
            if name not in _KNOWN_SURFACES:
                raise ValueError(f"llmproxy.surfaces.{name}: not a surface")
            if name not in _SHIPPED:
                raise ValueError(
                    f"llmproxy.surfaces.{name}: not available; "
                    f"this build ships: {', '.join(sorted(_SHIPPED))}"
                )
            if surface.mode == "passthrough" and not surface.api_key:
                raise ValueError(
                    f"llmproxy.surfaces.{name}.api_key is required in passthrough mode"
                )
        host, _ = _parse_bind(self.listener)
        loopback = host in ("::1", "localhost") or host.startswith("127.")
        if not loopback and not self.allow_remote:
            raise ValueError(
                f"llmproxy.listener binds non-loopback host {host!r}; the surfaces "
                "carry no transport authentication — set llmproxy.allow_remote = true "
                "to confirm this is intended"
            )
        return self


class LlmProxyModule(Module):
    name = "llmproxy"
    api_version = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.config = LlmProxySettings.model_validate(dict(settings))
        # Counters only, never content (llmproxy-v1 §6). The lock is real: the
        # surfaces serve from the module listener's thread, the control plane
        # from the main one.
        self._usage: dict[str, dict[str, int]] = {}
        self._usage_lock = threading.Lock()
        self._forwarders = {}
        for name, surface in self.config.surfaces.items():
            if surface.mode == "passthrough":
                from jetty.modules.llmproxy.forward import Forwarder

                self._forwarders[name] = Forwarder(
                    surface=name,
                    upstream=surface.upstream or _DEFAULT_UPSTREAM[name],
                    api_key=surface.api_key,
                    record=self._record,
                )
        self._app: FastAPI | None = None

    # --- contract ----------------------------------------------------------

    @property
    def listener_bind(self) -> tuple[str, int]:
        return _parse_bind(self.config.listener)

    @property
    def listener_url(self) -> str:
        host, port = self.listener_bind
        return f"http://{host}:{port}"

    def meta(self) -> dict[str, Any]:
        return {**super().meta(), "listener": self.listener_url}

    async def startup(self) -> None:
        for forwarder in self._forwarders.values():
            await forwarder.start()

    async def shutdown(self) -> None:
        for forwarder in self._forwarders.values():
            await forwarder.close()

    # --- usage accounting (llmproxy-v1 §6) ---------------------------------

    def _record(
        self,
        model: str,
        requests: int = 0,
        errors: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        with self._usage_lock:
            row = self._usage.setdefault(
                model, {"requests": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0}
            )
            row["requests"] += requests
            row["errors"] += errors
            row["input_tokens"] += input_tokens
            row["output_tokens"] += output_tokens

    # --- control plane (SPEC.md §3) ----------------------------------------

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/capabilities")
        async def capabilities() -> dict[str, Any]:
            surfaces: dict[str, Any] = {}
            for name, surface in self.config.surfaces.items():
                entry: dict[str, Any] = {"mode": surface.mode}
                if surface.mode == "passthrough":
                    entry["upstream"] = self._forwarders[name].upstream
                surfaces[name] = entry
            return {"listener": self.listener_url, "surfaces": surfaces}

        @router.get("/usage")
        async def usage() -> dict[str, Any]:
            with self._usage_lock:
                return {"models": {m: dict(row) for m, row in self._usage.items()}}

        return router

    # --- the surface listener (llmproxy-v1 §1–§3) --------------------------

    def listener_app(self) -> FastAPI:
        if self._app is None:
            app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
            for name, surface in self.config.surfaces.items():
                prefix = _SHIPPED[name]
                if surface.mode == "mock":
                    from jetty.modules.llmproxy.mock_gemini import build_router

                    app.include_router(build_router(self._record), prefix=prefix)
                else:
                    app.include_router(self._forward_router(name), prefix=prefix)

            @app.api_route(
                "/{path:path}",
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                include_in_schema=False,
            )
            async def unmatched(path: str) -> JSONResponse:
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {"code": 404, "message": "unknown path",
                                  "status": "NOT_FOUND"}
                    },
                )

            self._app = app
        return self._app

    def _forward_router(self, name: str) -> APIRouter:
        forwarder = self._forwarders[name]
        router = APIRouter()

        @router.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            include_in_schema=False,
        )
        async def forward(path: str, request: Request) -> Response:
            return await forwarder.handle(request, path)

        return router
