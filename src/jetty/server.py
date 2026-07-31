"""App factory: the core endpoints plus whatever modules config enabled."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jetty import __version__
from jetty.config import Config
from jetty.errors import (
    ErrorCode,
    JettyError,
    jetty_error_handler,
    unhandled_error_handler,
)
from jetty.modules.base import Module
from jetty.modules.registry import build_enabled, known_modules

SPEC_VERSION = "1.0.0-draft"
IMPLEMENTATION_NAME = "jetty-oss"


def _core_router(app_state: "AppState") -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """SPEC.md §4.1 — liveness. Deliberately contacts no upstream.

        Answers exactly one question: should my supervisor restart me? An
        upstream check here would mean a remote outage restarts the sidecar,
        fleet-wide and simultaneously, which cannot repair the dependency and
        removes the component that was correctly reporting it.
        """
        return {
            "ok": True,
            "spec_version": SPEC_VERSION,
            "uptime_s": int(time.monotonic() - app_state.started_at),
        }

    @router.get("/v1/meta")
    async def meta() -> dict[str, Any]:
        """SPEC.md §4.2 — capability discovery."""
        return {
            "spec_version": SPEC_VERSION,
            "implementation": {"name": IMPLEMENTATION_NAME, "version": __version__},
            "modules": [m.meta() for m in app_state.modules],
        }

    return router


class AppState:
    """Process-wide state: the enabled modules and the start time."""

    def __init__(self, config: Config, modules: list[Module]) -> None:
        self.config = config
        self.modules = modules
        self.started_at = time.monotonic()


def create_app(config: Config) -> FastAPI:
    modules = build_enabled(config.modules)
    state = AppState(config, modules)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started: list[Module] = []
        try:
            for module in modules:
                await module.startup()
                started.append(module)
            yield
        finally:
            for module in reversed(started):
                try:
                    await module.shutdown()
                except Exception:  # noqa: BLE001 - shutdown must not mask exit
                    pass

    app = FastAPI(
        title="jetty",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        # OpenAPI off by default: it enumerates the sidecar's whole surface to
        # anyone who can reach it. /v1/meta is the supported discovery path.
        openapi_url=None,
    )
    app.state.jetty = state

    app.add_exception_handler(JettyError, jetty_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Framework HTTPExceptions → the spec's envelope (SPEC.md §3.1).

        Without this, an unmatched route returns Starlette's
        `{"detail": "Not Found"}`, which is not the documented envelope — so a
        client's error handling would break on exactly the paths it is most
        likely to hit while misconfigured.

        A 404 whose path names a module Jetty *knows about* but which is not
        enabled is reported as `module_disabled` rather than `not_found`
        (SPEC.md §4.3): "you turned this off" and "this never existed" call for
        completely different operator responses.
        """
        if exc.status_code == 404:
            segment = request.url.path.lstrip("/").split("/", 1)[0]
            enabled = {m.name for m in state.modules}
            if segment in set(known_modules()) - enabled:
                err = JettyError(
                    ErrorCode.MODULE_DISABLED, f"module {segment!r} is not enabled"
                )
            else:
                err = JettyError(ErrorCode.NOT_FOUND, "no such route")
        elif exc.status_code == 405:
            err = JettyError(ErrorCode.NOT_FOUND, "no such route for this method")
        elif exc.status_code == 415:
            err = JettyError(
                ErrorCode.UNSUPPORTED_MEDIA_TYPE, "body must be application/json"
            )
        else:
            err = JettyError(ErrorCode.INTERNAL_ERROR, "unexpected framework error")
        return JSONResponse(status_code=err.status, content=err.body())

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic rejection → the spec's envelope (SPEC.md §3.1, §6).

        Unknown request fields are rejected rather than ignored, so a client
        relying on a field this build does not know about fails loudly.
        """
        err = JettyError(ErrorCode.INVALID_REQUEST, "request body failed validation")
        return JSONResponse(status_code=err.status, content=err.body())

    app.include_router(_core_router(state))
    for module in modules:
        app.include_router(module.router(), prefix=module.router_prefix)

    return app
