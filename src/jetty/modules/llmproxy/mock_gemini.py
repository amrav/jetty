"""The gemini surface's mock mode (llmproxy-v1 §5): a deterministic emulator.

Responses derive from a SHA-256 of the model id and the whole request body,
so a test can assert exact bytes and a re-run gets the same ones. Per §5 the
mock never rejects fields the provider would accept — it models what it needs
(`contents` text, `generationConfig.maxOutputTokens`) and ignores the rest —
because a CI run that uses tools or safety settings must not fail against the
fixture when it would succeed against the provider.

Token accounting is words: wrong for every real tokenizer, deterministic for
all of them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from jetty.modules.llmproxy.forward import RecordFn

MODELS = ["jetty-mock-large", "jetty-mock-small"]


def _error(status: int, google_status: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": status, "message": message, "status": google_status}},
    )


def _texts(value: Any) -> list[str]:
    """Every `text` field reachable under `value`, in document order."""
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            if key == "text" and isinstance(item, str):
                found.append(item)
            else:
                found.extend(_texts(item))
        return found
    if isinstance(value, list):
        return [t for item in value for t in _texts(item)]
    return []


def _completion(model: str, body: dict) -> tuple[str, str, dict[str, int]]:
    """(text, finishReason, usageMetadata) — deterministic in (model, body)."""
    digest = hashlib.sha256(
        model.encode() + b"\0" + json.dumps(body, sort_keys=True).encode()
    ).hexdigest()
    words = [f"mock({model})", *(digest[i : i + 8] for i in (0, 8, 16, 24))]
    finish = "STOP"
    limit = (body.get("generationConfig") or {}).get("maxOutputTokens")
    if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit < len(words):
        words = words[:limit]
        finish = "MAX_TOKENS"
    prompt_words = sum(len(t.split()) for t in _texts(body.get("contents")))
    usage = {
        "promptTokenCount": prompt_words,
        "candidatesTokenCount": len(words),
        "totalTokenCount": prompt_words + len(words),
    }
    return " ".join(words), finish, usage


def _chunks(model: str, text: str, finish: str, usage: dict[str, int]) -> list[dict]:
    words = text.split(" ")
    half = max(1, len(words) // 2)
    deltas = [" ".join(words[:half])]
    if words[half:]:
        deltas.append(" " + " ".join(words[half:]))
    chunks = [
        {"candidates": [{"content": {"parts": [{"text": d}], "role": "model"}, "index": 0}],
         "modelVersion": model}
        for d in deltas
    ]
    chunks[-1]["candidates"][0]["finishReason"] = finish
    chunks[-1]["usageMetadata"] = usage
    return chunks


def build_router(record: RecordFn) -> APIRouter:
    router = APIRouter()

    @router.get("/v1beta/models")
    async def list_models() -> dict:
        return {
            "models": [
                {
                    "name": f"models/{m}",
                    "displayName": m,
                    "supportedGenerationMethods": [
                        "generateContent", "streamGenerateContent",
                    ],
                }
                for m in MODELS
            ]
        }

    @router.post("/v1beta/models/{model_call}")
    async def model_call(model_call: str, request: Request):
        model, sep, action = model_call.partition(":")
        if not sep or action not in ("generateContent", "streamGenerateContent"):
            return _error(404, "NOT_FOUND", f"unknown method for {model_call!r}")
        record(model, requests=1)
        try:
            body = json.loads(await request.body())
        except ValueError:
            record(model, errors=1)
            return _error(400, "INVALID_ARGUMENT", "request body is not valid JSON")
        if not isinstance(body, dict) or not body.get("contents"):
            record(model, errors=1)
            return _error(400, "INVALID_ARGUMENT", "contents is required")

        text, finish, usage = _completion(model, body)
        record(
            model,
            input_tokens=usage["promptTokenCount"],
            output_tokens=usage["candidatesTokenCount"],
        )
        if action == "generateContent":
            return JSONResponse(
                {
                    "candidates": [
                        {"content": {"parts": [{"text": text}], "role": "model"},
                         "finishReason": finish, "index": 0}
                    ],
                    "usageMetadata": usage,
                    "modelVersion": model,
                }
            )
        chunks = _chunks(model, text, finish, usage)
        if request.query_params.get("alt") == "sse":
            payload = "".join(
                f"data: {json.dumps(c, separators=(',', ':'))}\r\n\r\n" for c in chunks
            )
            return StreamingResponse(
                iter([payload]),
                media_type="text/event-stream",
                headers={"cache-control": "no-store"},
            )
        # Without ?alt=sse the provider frames the stream as a JSON array.
        return JSONResponse(chunks)

    @router.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def unmatched(path: str) -> JSONResponse:
        return _error(404, "NOT_FOUND", "not implemented by the mock")

    return router
