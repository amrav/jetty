"""App factory: the core endpoints plus whatever modules config enabled."""

from __future__ import annotations

import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

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

#: SPEC.md §4.2 — cache readiness briefly so probes cannot amplify into
#: upstream load. A 1-second probe interval across a fleet is a DDoS on your
#: own directory otherwise.
READINESS_TTL_S = 5.0

#: SPEC.md §4.1/§2.3 — reachable without a credential so a supervisor can probe
#: liveness, and (readyz) so an orchestrator can probe readiness. Neither
#: discloses identity data.
UNAUTHENTICATED_PATHS = frozenset({"/healthz", "/readyz"})


class RequestIdMiddleware(BaseHTTPMiddleware):
    """SPEC.md §3.2 — echo the client's id, or mint one."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """SPEC.md §2.3 — bearer token on a TCP control listener."""

    def __init__(self, app: Any, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if request.url.path not in UNAUTHENTICATED_PATHS:
            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            # compare_digest, not ==: token comparison is a timing oracle
            # otherwise (SPEC.md §2.3 requires constant time).
            ok = scheme.lower() == "bearer" and secrets.compare_digest(
                presented.strip(), self._token
            )
            if not ok:
                err = JettyError(
                    ErrorCode.UNAUTHENTICATED, "missing or invalid bearer token"
                )
                return JSONResponse(status_code=err.status, content=err.body())
        return await call_next(request)


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """SPEC.md §2.2/§3.4 — cap control-listener bodies."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        super().__init__(app)
        self._max = max_bytes

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max:
            err = JettyError(
                ErrorCode.PAYLOAD_TOO_LARGE, f"body exceeds {self._max} bytes"
            )
            return JSONResponse(status_code=err.status, content=err.body())
        return await call_next(request)


def _core_router(app_state: "AppState") -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """SPEC.md §4.1 — liveness ONLY. Deliberately contacts no upstream.

        Wiring an upstream check to a path named healthz means a container
        supervisor restarts the sidecar whenever a remote directory has a bad
        minute — fleet-wide and simultaneously. Restarting a stateless sidecar
        cannot fix a remote outage; it only removes the component that was
        correctly reporting it. Readiness is /readyz.
        """
        return {
            "ok": True,
            "spec_version": SPEC_VERSION,
            "uptime_s": int(time.monotonic() - app_state.started_at),
        }

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        """SPEC.md §4.2 — per-module upstream reachability, briefly cached."""
        report = await app_state.readiness()
        failing_required = any(
            not entry["ready"] and entry["required"] for entry in report.values()
        )
        return JSONResponse(
            status_code=503 if failing_required else 200,
            content={"ok": not failing_required, "modules": report},
        )

    @router.get("/v1/meta")
    async def meta() -> dict[str, Any]:
        """SPEC.md §4.3 — capability discovery."""
        return {
            "spec_version": SPEC_VERSION,
            "implementation": {"name": IMPLEMENTATION_NAME, "version": __version__},
            "modules": [m.meta() for m in app_state.modules],
            "limits": app_state.config.limits,
        }

    return router


class AppState:
    """Process-wide state: the enabled modules and the readiness cache."""

    def __init__(self, config: Config, modules: list[Module]) -> None:
        self.config = config
        self.modules = modules
        self.started_at = time.monotonic()
        self._readiness_cache: tuple[float, dict[str, Any]] | None = None

    async def readiness(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._readiness_cache and now - self._readiness_cache[0] < READINESS_TTL_S:
            return self._readiness_cache[1]

        report: dict[str, Any] = {}
        for module in self.modules:
            try:
                result = await module.ready()
            except Exception:
                # Fail closed (SPEC.md §1.2): a module whose readiness check
                # itself blew up is not ready. Swallowing to "ready" here would
                # be the exact fail-open this spec exists to prevent.
                report[module.name] = {
                    "ready": False,
                    "required": module.required,
                    "detail": str(ErrorCode.INTERNAL_ERROR),
                }
                continue
            report[module.name] = {
                "ready": result.ready,
                "required": module.required,
                "detail": str(result.detail) if result.detail else None,
            }
        self._readiness_cache = (now, report)
        return report


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
        (SPEC.md §4.4): "you turned this off" and "this never existed" call for
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

    # Middleware runs bottom-up: request id outermost so that even a rejected
    # request carries one back.
    app.add_middleware(BodyLimitMiddleware, max_bytes=config.limits["body_bytes"])
    if config.listener.token:
        app.add_middleware(BearerAuthMiddleware, token=config.listener.token)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(_core_router(state))
    for module in modules:
        app.include_router(
            module.router(), prefix=f"{module.mount}/{module.api_version}"
        )

    return app
