"""Transparent forwarding for one surface (llmproxy-v1 §3–§4).

The forwarder never parses what it does not have to: the request path, query,
and body go upstream verbatim, and the upstream's response — success, error,
or stream — comes back verbatim. The only rewriting is credential custody
(§4) and hop-by-hop transport headers. jetty speaks with its own voice solely
when there is no upstream response to relay (§3.1), and marks that voice with
`x-jetty-error` so clients never have to guess whose failure they are seeing.

Imported only when a surface is in passthrough mode, keeping httpx out of the
process image for mock-only deployments.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("jetty.llmproxy")

#: Client-supplied credentials, stripped before forwarding (llmproxy-v1 §4).
CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "x-goog-api-key"})
CREDENTIAL_PARAMS = frozenset({"key"})

#: Transport-level headers that must not be relayed in either direction.
HOP_HEADERS = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade", "host",
        "content-length", "accept-encoding", "expect",
    }
)

#: How each surface's provider expects the configured credential (§4).
AUTH_HEADER = {"gemini": "x-goog-api-key"}

#: Provider-shaped error for a response jetty must synthesize itself (§3.1).
_SYNTH_BODY = {
    "gemini": lambda status, message: {
        "error": {"code": status, "message": message, "status": "UNAVAILABLE"}
    },
}

#: Model extraction from the surface's URL layout, for usage counters (§6).
_MODEL_RE = {"gemini": re.compile(r"/models/([^:/?]+)")}

_CONNECT_TIMEOUT_S = 10.0

RecordFn = Callable[..., None]  # record(model, *, requests=0, errors=0, ...)


def synthesized(surface: str, reason: str, message: str, status: int = 502) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=_SYNTH_BODY[surface](status, message),
        headers={"x-jetty-error": reason},
    )


class Forwarder:
    def __init__(self, surface: str, upstream: str, api_key: str, record: RecordFn) -> None:
        self.surface = surface
        self.upstream = upstream.rstrip("/")
        self._api_key = api_key
        self._record = record
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.upstream,
            # No read timeout: a long generation or an open stream is not an
            # error, and the client's own disconnect cancels the upstream call.
            timeout=httpx.Timeout(None, connect=_CONNECT_TIMEOUT_S),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _model(self, path: str) -> str:
        match = _MODEL_RE[self.surface].search(f"/{path}")
        return match.group(1) if match else "-"

    async def handle(self, request: Request, path: str) -> Response:
        assert self._client is not None, "forwarder used before startup()"
        model = self._model(path)
        self._record(model, requests=1)

        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_HEADERS and k.lower() not in CREDENTIAL_HEADERS
        }
        headers[AUTH_HEADER[self.surface]] = self._api_key
        # Pin identity so the relayed body matches the relayed headers; a
        # transparent proxy must not re-encode.
        headers["accept-encoding"] = "identity"
        params = [
            (k, v)
            for k, v in request.query_params.multi_items()
            if k.lower() not in CREDENTIAL_PARAMS
        ]

        upstream_request = self._client.build_request(
            request.method,
            f"/{path}",
            params=params,
            content=await request.body(),
            headers=headers,
        )
        try:
            upstream = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            # §3.1: the one case jetty answers for itself. The exception text
            # stays in the log; it can name internals but never the key.
            log.warning("llmproxy %s upstream unreachable: %r", self.surface, exc)
            self._record(model, errors=1)
            return synthesized(self.surface, "upstream_unreachable", "upstream unreachable")

        content_type = upstream.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            return StreamingResponse(
                self._relay(upstream),
                status_code=upstream.status_code,
                media_type=content_type,
                headers={"cache-control": upstream.headers.get("cache-control", "no-store")},
            )

        raw = await upstream.aread()
        await upstream.aclose()
        if upstream.status_code >= 400:
            self._record(model, errors=1)
        else:
            self._usage(model, content_type, raw)
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=content_type or None,
        )

    async def _relay(self, upstream: httpx.Response) -> AsyncIterator[bytes]:
        """Verbatim byte relay (§3): no added events, no added terminators.

        If the upstream connection drops mid-stream, this generator ends and
        the client connection closes — truncation is propagated, never
        papered over with a fabricated tail (SPEC.md §1.2).
        """
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            log.warning("llmproxy %s upstream stream lost: %r", self.surface, exc)
        finally:
            await upstream.aclose()

    def _usage(self, model: str, content_type: str, raw: bytes) -> None:
        """Best-effort token counters (§6): parse, count, and forget."""
        if not content_type.startswith("application/json"):
            return
        try:
            meta: Any = json.loads(raw).get("usageMetadata")
        except Exception:  # noqa: BLE001 - a body we cannot parse is not ours to judge
            return
        if isinstance(meta, dict):
            self._record(
                model,
                input_tokens=int(meta.get("promptTokenCount", 0)),
                output_tokens=int(meta.get("candidatesTokenCount", 0)),
            )
