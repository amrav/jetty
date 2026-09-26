"""The functions-v1 surface: name syntax, raw payloads, error mapping.

Like `sql` and `filesystem`, this is not a foreign protocol: errors use the
SPEC.md §3 envelope, and functions-v1 §5 adds one module code rendered in
that same envelope, closed-set-by-construction as in `jetty.errors`.

Payloads cross as raw bytes both ways and are never interpreted: no content
negotiation, no JSON parsing, no schema. The HTTP status is Jetty's layer;
the bytes are the application's (functions-v1 §4.1). That split is the
whole design — a function's own failure vocabulary lives inside its
payload, where the application's spec defines it, and a non-2xx here always
means something about Jetty or its driver.

Drivers are pluggable by name. ``exec`` ships here; a private build adds
its own with ``register_driver``, or names a ``package.module:factory``
path in config and needs no code in this repository at all.
"""

from __future__ import annotations

import importlib
import logging
import re
from typing import Any, Callable, Coroutine, Mapping

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from jetty.errors import ErrorCode, JettyError
from jetty.modules.base import Module
from jetty.modules.functions.driver import (
    DriverFactory,
    FunctionDriver,
    FunctionFailed,
    UnknownFunction,
)

log = logging.getLogger("jetty.functions")

#: functions-v1 §2: one path segment, no encoding ever needed.
_NAME = re.compile(r"^[a-z0-9_.-]{1,128}$")

#: functions-v1 §5: the module's own code, status and retryability paired so
#: a handler cannot mismatch them — same argument as jetty.errors.
_SEMANTICS: dict[str, tuple[int, bool]] = {
    "unknown_function": (404, False),
}


# --- driver registry (functions-v1 §3, §6) ----------------------------------

_DRIVERS: dict[str, DriverFactory] = {}


def register_driver(name: str, factory: DriverFactory) -> None:
    """Make ``driver = "<name>"`` build a driver with ``factory``.

    For builds that vendor this repository and add a driver of their own.
    Registration must happen before the config is loaded — i.e. at import
    time of whatever entrypoint the build uses.
    """
    if name in _DRIVERS:
        raise ValueError(f"functions driver {name!r} is already registered")
    _DRIVERS[name] = factory


def _exec_factory(settings: Mapping[str, Any]) -> FunctionDriver:
    from jetty.modules.functions.exec import build

    return build(settings)


register_driver("exec", _exec_factory)


def _resolve_driver(spec: str) -> DriverFactory:
    """A registered name, or a ``package.module:attribute`` import path."""
    if spec in _DRIVERS:
        return _DRIVERS[spec]
    if ":" not in spec:
        raise ValueError(
            f"functions.driver {spec!r} is not available; registered drivers: "
            f"{', '.join(sorted(_DRIVERS))}; or name a package.module:factory"
        )
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"functions.driver {spec!r}: cannot import: {exc}") from exc
    factory = getattr(module, attr, None)
    if not callable(factory):
        raise ValueError(f"functions.driver {spec!r}: {attr!r} is not a callable")
    return factory


# --- errors -----------------------------------------------------------------

class FunctionsApiError(Exception):
    """A functions-v1 §5 module error, rendered in the SPEC.md §3.1 envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status, self.retryable = _SEMANTICS[code]

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "retryable": self.retryable,
                }
            },
        )


class _FunctionsRoute(APIRoute):
    """Render module codes; let everything else reach the app handlers."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except FunctionsApiError as exc:
                return exc.response()

        return wrapped


class _CoreSettings(BaseModel):
    """The two keys the module itself reads. Everything else in the table is
    the driver's, and the driver's factory validates it (a typo'd key still
    fails boot — in the driver's own schema, as with `exec`)."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    driver: str = "exec"


def _check_name(name: str) -> str:
    if not _NAME.match(name):
        raise JettyError(ErrorCode.INVALID_REQUEST, f"invalid function name {name!r}")
    return name


class FunctionsModule(Module):
    name = "functions"
    api_version = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        core = _CoreSettings.model_validate(dict(settings))
        factory = _resolve_driver(core.driver)
        self.driver: FunctionDriver = factory(settings)

    async def startup(self) -> None:
        hook = getattr(self.driver, "startup", None)
        if hook is not None:
            await hook()

    async def shutdown(self) -> None:
        hook = getattr(self.driver, "shutdown", None)
        if hook is not None:
            await hook()

    def router(self) -> APIRouter:
        router = APIRouter(route_class=_FunctionsRoute)

        @router.get("/list")
        async def list_functions() -> dict[str, Any]:
            return {"functions": sorted(self.driver.functions())}

        @router.post("/call/{name}")
        async def call(name: str, request: Request) -> Response:
            fn_name = _check_name(name)
            payload = await request.body()
            try:
                result = await self.driver.call(fn_name, payload)
            except UnknownFunction as exc:
                raise FunctionsApiError(
                    "unknown_function", f"no function named {fn_name!r}"
                ) from exc
            except FunctionFailed as exc:
                log.warning("function %s failed: %s", fn_name, exc)
                raise JettyError(
                    ErrorCode.UPSTREAM_ERROR, f"function {fn_name!r} failed"
                ) from exc
            except (JettyError, FunctionsApiError):
                raise
            except Exception as exc:
                # Could not spawn, timed out, and everything nobody
                # anticipated: the backend is unreachable. Never a fabricated
                # success (SPEC.md §1.2); the detail stays in the log.
                log.warning("function %s unavailable: %r", fn_name, exc)
                raise JettyError(
                    ErrorCode.UPSTREAM_UNAVAILABLE,
                    f"function {fn_name!r} could not be run",
                ) from exc
            # functions-v1 §0: names and sizes may be logged, payloads never.
            log.info(
                "function call name=%s in=%d out=%d",
                fn_name, len(payload), len(result),
            )
            return Response(content=result, media_type="application/octet-stream")

        return router
