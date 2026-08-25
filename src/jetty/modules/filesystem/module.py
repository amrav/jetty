"""The filesystem-v1 surface: path syntax and error mapping.

Like `sql`, this is not a foreign protocol: errors use the SPEC.md §3
envelope. filesystem-v1 §6 adds one module code, which SPEC.md §3.1 permits
a module to define; the route class below renders it in the same envelope
shape, with the same closed-set-by-construction discipline as `jetty.errors`.

The bodies themselves are raw bytes both ways (filesystem-v1 §5) — a file is
not JSON, and wrapping one in base64 would make every client carry a second
codec for no gain between co-located processes.

Handlers are async only to read the request body; every driver call runs in
the threadpool (`run_in_threadpool`), so blocking disk I/O never parks the
event loop — the same reasoning as hg's sync handlers.
"""

from __future__ import annotations

import logging
import mimetypes
from typing import Any, Callable, Coroutine, Mapping, TypeVar

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from jetty.errors import ErrorCode, JettyError
from jetty.modules.base import Module
from jetty.modules.filesystem.driver import (
    FileMissing,
    FsDriver,
    InvalidTarget,
    PermissionDenied,
)

log = logging.getLogger("jetty.filesystem")

#: filesystem-v1 §3: longer than any real path, short enough to refuse abuse.
_MAX_PATH_BYTES = 4096

#: filesystem-v1 §6: the module's own code. Status and retryability are
#: paired here so a handler cannot mismatch them — same argument as
#: jetty.errors.
_SEMANTICS: dict[str, tuple[int, bool]] = {
    "permission_denied": (403, False),
}


class FsApiError(Exception):
    """A filesystem-v1 §6 module error, rendered in the SPEC.md §3.1 envelope."""

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


class _FsRoute(APIRoute):
    """Render module codes; let everything else reach the app handlers.

    JettyError already produces the correct envelope at app level — this
    surface speaks the native protocol, so only the module's own codes need
    local treatment.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except FsApiError as exc:
                return exc.response()

        return wrapped


class FilesystemSettings(BaseModel):
    """`[modules.filesystem]` — a typo'd key must fail boot, same as core."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    driver: str = "local"
    #: The servable tree; the module's entire filesystem authority. Required.
    root: str


def _check_path(path: str) -> str:
    """filesystem-v1 §3: root-relative, no traversal, no games.

    Syntax only — where symlinks *lead* is the driver's containment check.
    """
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(seg in ("", ".", "..") for seg in path.split("/"))
    ):
        raise JettyError(ErrorCode.INVALID_REQUEST, f"invalid file path {path!r}")
    if len(path.encode("utf-8")) > _MAX_PATH_BYTES:
        raise JettyError(
            ErrorCode.INVALID_REQUEST, f"path exceeds {_MAX_PATH_BYTES} bytes"
        )
    return path


T = TypeVar("T")


class FilesystemModule(Module):
    name = "filesystem"
    api_version = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.cfg = FilesystemSettings.model_validate(dict(settings))
        if self.cfg.driver != "local":
            # Drivers for other storage are private implementations; naming
            # one this build does not ship must fail at boot, not fall back.
            raise ValueError(
                f"filesystem.driver {self.cfg.driver!r} is not available; "
                "this build ships: local"
            )
        from jetty.modules.filesystem.local import LocalFsDriver

        self.driver: FsDriver = LocalFsDriver(self.cfg.root)

    async def startup(self) -> None:
        """Prove the root once; a misconfiguration must abort boot, not serve
        errors forever (SPEC.md §1.2). Writability is deliberately not
        checked: a read-only root is a legitimate deployment, and per-request
        permission evaluation is the module's whole model."""
        import os

        if not os.path.isdir(self.cfg.root):
            raise RuntimeError(
                f"filesystem module: root {self.cfg.root!r} is not a directory"
            )

    async def _dispatch(self, fn: Callable[..., T], *args: Any) -> T:
        """One driver call in the threadpool, with filesystem-v1 §6's error
        mapping applied."""
        try:
            return await run_in_threadpool(fn, *args)
        except FileMissing as exc:
            raise JettyError(ErrorCode.NOT_FOUND, str(exc)) from exc
        except InvalidTarget as exc:
            raise JettyError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        except PermissionDenied as exc:
            raise FsApiError("permission_denied", str(exc)) from exc
        except (JettyError, FsApiError):
            raise
        except Exception as exc:
            # EIO, ENOSPC, and everything nobody anticipated: the store is
            # failing. Never a fabricated success (SPEC.md §1.2); the detail
            # stays in the log (SPEC.md §1.4).
            log.warning("filesystem driver failure: %r", exc)
            raise JettyError(
                ErrorCode.UPSTREAM_UNAVAILABLE, "filesystem store failure"
            ) from exc

    def router(self) -> APIRouter:
        router = APIRouter(route_class=_FsRoute)

        @router.get("/files/{file_path:path}")
        async def read_file(file_path: str) -> Response:
            rel = _check_path(file_path)
            content = await self._dispatch(self.driver.read, rel)
            media = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            return Response(content=content, media_type=media)

        @router.put("/files/{file_path:path}")
        async def write_file(file_path: str, request: Request) -> dict[str, Any]:
            rel = _check_path(file_path)
            raw = await request.body()
            result = await self._dispatch(self.driver.write, rel, raw)
            # filesystem-v1 §0: paths may be logged, content never.
            log.info(
                "filesystem write path=%s bytes=%d created=%s",
                rel, result.size, result.created,
            )
            return {"size": result.size, "created": result.created}

        return router
