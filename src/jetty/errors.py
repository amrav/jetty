"""The error envelope from SPEC.md §3.1.

Every non-2xx response on the control listener goes through here, so the closed
code set is enforced by construction rather than by review: `JettyError` cannot
be raised with a code outside `ErrorCode`.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCode(StrEnum):
    """SPEC.md §3.1. Clients switch on these; they are contractual."""

    UNAUTHENTICATED = "unauthenticated"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    MODULE_DISABLED = "module_disabled"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_ERROR = "upstream_error"


#: Canonical status + retryability per code, so a handler cannot pair a code
#: with the wrong status. SPEC.md §3.1's table, executable.
_SEMANTICS: dict[ErrorCode, tuple[int, bool]] = {
    ErrorCode.UNAUTHENTICATED: (401, False),
    ErrorCode.INVALID_REQUEST: (400, False),
    ErrorCode.NOT_FOUND: (404, False),
    ErrorCode.MODULE_DISABLED: (404, False),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: (415, False),
    ErrorCode.PAYLOAD_TOO_LARGE: (413, False),
    ErrorCode.RATE_LIMITED: (429, True),
    ErrorCode.INTERNAL_ERROR: (500, True),
    ErrorCode.UPSTREAM_UNAVAILABLE: (503, True),
    ErrorCode.UPSTREAM_ERROR: (502, True),
}


class JettyError(Exception):
    """Raise this anywhere; the handler below renders the envelope.

    `message` is for humans and logs and is explicitly NOT stable across
    versions (SPEC.md §3.1). It must never contain credential material
    (SPEC.md §1.4) — callers are responsible for not putting it there, since
    only the caller knows which of its locals are secret.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status, self.retryable = _SEMANTICS[code]

    def body(self) -> dict[str, object]:
        return {
            "error": {
                "code": str(self.code),
                "message": self.message,
                "retryable": self.retryable,
            }
        }


async def jetty_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, JettyError)
    return JSONResponse(status_code=exc.status, content=exc.body())


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Fail-closed backstop (SPEC.md §1.2).

    A bug becomes a 500 with a generic message. The exception text is
    deliberately NOT forwarded to the client: it is the most likely place for a
    credential to leak into a response, since nobody audits the repr of an
    arbitrary third-party exception.
    """
    err = JettyError(ErrorCode.INTERNAL_ERROR, "internal error; see sidecar logs")
    return JSONResponse(status_code=err.status, content=err.body())
