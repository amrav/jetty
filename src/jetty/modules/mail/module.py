"""The mail surface (mail-v1 §1–§3): wire validation, errors, driver dispatch.

This module is a foreign protocol on the control listener (SPEC.md §2.1): it
conforms to the relay contract it ports — flat ``{"error": slug}`` bodies, the
contract's status codes — not to SPEC.md §3. The router owns the whole mount
prefix via ``router_prefix``: ``/mail/v1/send``, ``/mail/healthz``.

Fail-closed (SPEC.md §1.2) at this boundary means: a driver failure is
``503 upstream_unavailable``, a surface bug is ``500 internal_error``, and
neither is ever a fabricated ``202``. The route class below guarantees the
shape even for exceptions nobody anticipated.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Coroutine, Mapping

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, model_validator

from jetty.modules.base import Module
from jetty.modules.mail.driver import (
    MailDriver,
    MailSend,
    MessageTooLarge,
    RateLimited,
    SenderNotPermitted,
    UnroutableRecipients,
)

log = logging.getLogger("jetty.mail")

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_RECIPIENTS = 100
MAX_KEY_BYTES = 128
MAX_SUBJECT_CHARS = 512
MAX_TEXT_BYTES = 256 * 1024
MAX_HTML_BYTES = 512 * 1024
MAX_THREAD_KEY_BYTES = 256
MAX_TAGS = 10
MAX_TAG_BYTES = 64
MAX_ADDRESS_BYTES = 320
MAX_REPLY_TO = 5

#: A plain ``local@domain``, no display names (mail-v1 §3.1).
_ADDRESS = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_FIELDS = {
    "idempotencyKey", "from", "replyTo", "to", "cc", "bcc",
    "subject", "text", "html", "threadKey", "tags", "dryRun",
}


class MailApiError(Exception):
    """An error in the relay contract's shape (mail-v1 §1)."""

    def __init__(self, status: int, slug: str, **extra: Any) -> None:
        super().__init__(slug)
        self.status = status
        self.slug = slug
        self.extra = extra

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status, content={"error": self.slug, **self.extra}
        )


class _MailRoute(APIRoute):
    """Every mail response — including failures — leaves in the contract shape.

    App-level exception handlers render the SPEC.md §3.1 envelope, which is
    exactly wrong here, so nothing may escape this route class. Unexpected
    exceptions become ``500 internal_error`` with no detail: the exception
    text stays in the log, where a leaked credential cannot reach a client
    (SPEC.md §1.4).
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except MailApiError as exc:
                return exc.response()
            except Exception:
                log.exception("mail surface error on %s", request.url.path)
                return MailApiError(500, "internal_error").response()

        return wrapped


class MailSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    driver: str = "spool"
    token: str = ""
    spool_dir: str = ""
    sender: str = ""
    domain: str = ""
    fail: str = ""

    @model_validator(mode="after")
    def _check(self) -> "MailSettings":
        if self.fail not in ("", "503", "429", "422"):
            raise ValueError(f"mail.fail must be 503, 429 or 422, not {self.fail!r}")
        if self.driver == "spool" and not self.spool_dir:
            raise ValueError("mail.spool_dir is required for the spool driver")
        return self


# --- request validation (mail-v1 §3.1). Every rejection is 400 bad_request
# with a detail naming the field; the slug is what clients switch on. --------

def _bad(detail: str) -> MailApiError:
    return MailApiError(400, "bad_request", detail=detail)


def _addresses(value: Any, field_name: str, maximum: int | None = None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise _bad(f"{field_name} must be an array of addresses")
    for address in value:
        if not _ADDRESS.match(address) or len(address.encode("utf-8")) > MAX_ADDRESS_BYTES:
            raise _bad(f"{field_name}: not a plain local@domain address")
    if maximum is not None and len(value) > maximum:
        raise _bad(f"{field_name} carries more than {maximum} addresses")
    return value


def _bounded_str(
    value: Any, field_name: str, max_bytes: int, required: bool = False
) -> str:
    if value is None:
        if required:
            raise _bad(f"{field_name} is required")
        return ""
    if not isinstance(value, str) or (required and not value):
        raise _bad(f"{field_name} must be a non-empty string")
    if len(value.encode("utf-8")) > max_bytes:
        raise _bad(f"{field_name} exceeds {max_bytes} bytes")
    return value


def _parse_send(body: Any) -> MailSend:
    if not isinstance(body, dict):
        raise _bad("request body must be an object")
    unknown = sorted(set(body) - _FIELDS)
    if unknown:
        raise _bad(f"unknown field {unknown[0]!r} in request body")

    key = _bounded_str(body.get("idempotencyKey"), "idempotencyKey", MAX_KEY_BYTES, required=True)

    from_addr = body.get("from")
    if not isinstance(from_addr, str) or not _ADDRESS.match(from_addr):
        raise _bad("from must be a plain local@domain address")

    to = _addresses(body.get("to"), "to")
    if not to:
        raise _bad("to requires at least one recipient")
    cc = _addresses(body.get("cc"), "cc")
    bcc = _addresses(body.get("bcc"), "bcc")
    reply_to = _addresses(body.get("replyTo"), "replyTo", maximum=MAX_REPLY_TO)
    if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
        raise _bad(f"to+cc+bcc exceeds {MAX_RECIPIENTS} addresses")

    subject = body.get("subject")
    if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_CHARS:
        raise _bad(f"subject is required, one line, at most {MAX_SUBJECT_CHARS} chars")
    # Header injection: a subject is one line, always.
    if "\r" in subject or "\n" in subject:
        raise _bad("subject must not contain CR or LF")

    text = _bounded_str(body.get("text"), "text", MAX_TEXT_BYTES, required=True)
    html = _bounded_str(body.get("html"), "html", MAX_HTML_BYTES)
    thread_key = _bounded_str(body.get("threadKey"), "threadKey", MAX_THREAD_KEY_BYTES)

    tags = body.get("tags")
    if tags is None:
        tags = []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise _bad("tags must be an array of strings")
    if len(tags) > MAX_TAGS:
        raise _bad(f"tags carries more than {MAX_TAGS} entries")
    if any(len(t.encode("utf-8")) > MAX_TAG_BYTES for t in tags):
        raise _bad(f"tags entries are at most {MAX_TAG_BYTES} bytes")

    dry_run = body.get("dryRun", False)
    if not isinstance(dry_run, bool):
        raise _bad("dryRun must be a boolean")

    return MailSend(
        idempotency_key=key,
        from_addr=from_addr,
        to=to,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        subject=subject,
        text=text,
        html=html,
        thread_key=thread_key,
        tags=tags,
        dry_run=dry_run,
    )


# --- the module ------------------------------------------------------------

class MailModule(Module):
    name = "mail"
    api_version = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.config = MailSettings.model_validate(dict(settings))
        if self.config.driver != "spool":
            # Delivery drivers are private implementations; naming one this
            # build does not ship must fail at boot, not spool silently.
            raise ValueError(
                f"mail.driver {self.config.driver!r} is not available; "
                "this build ships: spool"
            )
        from jetty.modules.mail.spool import SpoolMailDriver

        self.driver: MailDriver = SpoolMailDriver(
            spool_dir=self.config.spool_dir,
            sender=self.config.sender,
            domain=self.config.domain,
            fail=self.config.fail,
        )

    @property
    def router_prefix(self) -> str:
        return self.mount  # foreign protocol: the router owns /mail entirely

    def _require_token(self, request: Request) -> None:
        """mail-v1 §2 — every /v1 route, healthz exempt."""
        if not self.config.token:
            return
        if request.headers.get("authorization") != f"Bearer {self.config.token}":
            raise MailApiError(401, "missing_or_bad_bearer_token")

    def router(self) -> APIRouter:
        router = APIRouter(route_class=_MailRoute)

        @router.get("/healthz")
        async def healthz():
            """The relay contract's health check: upstream reachability —
            deliberately unlike the core /healthz (mail-v1 §3.4)."""
            try:
                await self.driver.ping()
            except Exception as exc:
                log.warning("mail driver ping failed: %r", exc)
                raise MailApiError(503, "upstream_unavailable") from exc
            return {"ok": True}

        @router.post("/v1/send")
        async def send(request: Request):
            self._require_token(request)
            raw = await request.body()
            if len(raw) > MAX_BODY_BYTES:
                raise _bad("request body too large")
            try:
                body = await request.json()
            except Exception:
                raise _bad("unparseable body") from None
            msg = _parse_send(body)
            try:
                result = await self.driver.send(msg)
            except SenderNotPermitted as exc:
                raise MailApiError(403, "sender_not_permitted", detail=str(exc)) from exc
            except UnroutableRecipients as exc:
                raise MailApiError(
                    422, "unroutable_recipients", recipients=exc.recipients
                ) from exc
            except MessageTooLarge as exc:
                raise MailApiError(413, "message_too_large") from exc
            except RateLimited as exc:
                raise MailApiError(
                    429, "rate_limited", retryAfterSeconds=exc.retry_after_s
                ) from exc
            except MailApiError:
                raise
            except Exception as exc:
                log.warning("mail driver failure: %r", exc)
                raise MailApiError(503, "upstream_unavailable") from exc
            # mail-v1 §4: key, id, recipient count, outcome — never a subject
            # or body, which may name people.
            log.info(
                "mail send key=%s id=%s recipients=%d deduped=%s dry_run=%s",
                msg.idempotency_key,
                result.message_id,
                len(msg.to) + len(msg.cc) + len(msg.bcc),
                result.deduped,
                msg.dry_run,
            )
            return JSONResponse(
                status_code=202,
                content={"messageId": result.message_id, "deduped": result.deduped},
            )

        # --- everything else: the contract has exactly two endpoints -------
        @router.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            include_in_schema=False,
        )
        async def unmatched(request: Request, path: str):
            self._require_token(request)
            raise MailApiError(404, "not_found")

        return router
