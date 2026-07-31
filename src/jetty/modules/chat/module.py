"""The emulated surface (chat-v1 §1–§3): URL layout, translation, errors.

This module is a foreign protocol on the control listener (SPEC.md §2.1): it
conforms to the API it emulates, not to SPEC.md §3. Errors use the emulated
shape, requests carry no jetty envelope, and the router owns the whole mount
prefix — `/chat/v1/…` and `/chat/upload/v1/…` — via `router_prefix`.

Fail-closed (SPEC.md §1.2) at this boundary means: a driver failure is the
emulated `UNAVAILABLE`, a surface bug is the emulated `INTERNAL`, and neither
is ever a fabricated `200`. The route class below guarantees the shape even
for exceptions nobody anticipated.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Mapping

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from jetty.modules.base import Module
from jetty.modules.chat.driver import (
    ChatDriver,
    DriverRejects,
    Emoji,
    Member,
    Message,
    MessageCreate,
    MessagePatch,
    MessageQuery,
    MessageSearch,
    Page,
    QuoteRequest,
    Reaction,
    REPLY_FALLBACK,
    REPLY_NEW,
    REPLY_OR_FAIL,
    Space,
    SpaceCreate,
    ThreadNotFound,
    Upload,
)

log = logging.getLogger("jetty.chat")

#: Emulated status -> HTTP code. Closed set; chat-v1 §3 names the statuses.
_STATUS_HTTP = {
    "INVALID_ARGUMENT": 400,
    "NOT_FOUND": 404,
    "UNIMPLEMENTED": 501,
    "UNAVAILABLE": 503,
    "INTERNAL": 500,
}


class ChatApiError(Exception):
    """An error in the emulated API's shape (chat-v1 §1)."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.http = _STATUS_HTTP[status]
        self.message = message

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.http,
            content={
                "error": {
                    "code": self.http,
                    "message": self.message,
                    "status": self.status,
                }
            },
        )


class _ChatRoute(APIRoute):
    """Every chat response — including failures — leaves in the emulated shape.

    App-level exception handlers render the SPEC.md §3.1 envelope, which is
    exactly wrong here, so nothing may escape this route class. Unexpected
    exceptions become the emulated `INTERNAL` with a generic message: the
    exception text stays in the log, where a leaked credential cannot reach a
    client (SPEC.md §1.4).
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except ChatApiError as exc:
                return exc.response()
            except RequestValidationError as exc:
                # A malformed query parameter (`pageSize=abc`) surfaces here;
                # the app-level handler would render the jetty envelope.
                return ChatApiError(
                    "INVALID_ARGUMENT", f"invalid request: {exc.errors()[0]['loc']}"
                ).response()
            except Exception:
                log.exception("chat surface error on %s", request.url.path)
                return ChatApiError("INTERNAL", "internal error").response()

        return wrapped


# --- configuration ---------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeedSpace(_Strict):
    id: str
    display_name: str = ""
    space_type: str = "SPACE"
    members: list[str] = Field(default_factory=list)


class SeedMessage(_Strict):
    space: str
    text: str
    sender: str = "users/seed"


class ChatSeed(_Strict):
    spaces: list[SeedSpace] = Field(default_factory=list)
    messages: list[SeedMessage] = Field(default_factory=list)


class ChatSettings(_Strict):
    enabled: bool = False
    driver: str = "mock"
    upload_max_bytes: int = 26214400
    identity: str = "users/jetty"
    seed: ChatSeed = Field(default_factory=ChatSeed)


# --- time ------------------------------------------------------------------

def _rfc3339(t: float) -> str:
    stamp = datetime.fromtimestamp(t, tz=timezone.utc)
    return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: str, field_name: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise ChatApiError(
            "INVALID_ARGUMENT", f"{field_name}: invalid timestamp {value!r}"
        ) from None


# --- wire rendering (chat-v1 §3.6: full resource names, listed fields) -----

def _space_json(space: Space) -> dict:
    return {
        "name": space.name,
        "displayName": space.display_name,
        "spaceType": space.space_type,
    }


def _member_json(member: Member) -> dict:
    return {
        "name": member.name,
        "member": {"name": member.user, "type": member.user_type},
    }


def _message_json(message: Message) -> dict:
    body: dict[str, Any] = {
        "name": message.name,
        "sender": {"name": message.sender, "type": message.sender_type},
        "thread": {"name": message.thread},
        "text": message.text,
        "createTime": _rfc3339(message.create_time),
    }
    if message.update_time:
        body["lastUpdateTime"] = _rfc3339(message.update_time)
    if message.attachments:
        body["attachment"] = [
            {"contentName": a.content_name, "contentType": a.content_type}
            for a in message.attachments
        ]
    if message.quoted is not None:
        body["quotedMessageMetadata"] = {
            "name": message.quoted.name,
            "lastUpdateTime": _rfc3339(message.quoted.last_update_time),
            "quotedMessageSnapshot": {"text": message.quoted.text},
        }
    return body


def _reaction_json(reaction: Reaction) -> dict:
    return {
        "name": reaction.name,
        "user": {"name": reaction.user},
        "emoji": {"unicode": reaction.emoji},
    }


# --- request parsing -------------------------------------------------------

async def _json_body(request: Request, allowed: set[str]) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise ChatApiError("INVALID_ARGUMENT", "request body is not JSON") from None
    if not isinstance(body, dict):
        raise ChatApiError("INVALID_ARGUMENT", "request body must be an object")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ChatApiError(
            "INVALID_ARGUMENT", f"unknown field {unknown[0]!r} in request body"
        )
    return body


def _page(page_size: int | None, page_token: str | None) -> Page:
    size = page_size if page_size is not None else 0
    if size < 0:
        raise ChatApiError("INVALID_ARGUMENT", "pageSize must not be negative")
    return Page(size=min(size, 1000) or 25, token=page_token or "")


_FILTER_TERM = re.compile(
    r"^\s*(?P<field>[\w.]+)\s*(?P<op>[><=])\s*\"?(?P<value>[^\"]*?)\"?\s*$"
)


def _parse_list_filter(filter_str: str) -> tuple[float, float, str]:
    """chat-v1 §3.2's filter grammar -> (after, before, thread)."""
    after, before, thread = 0.0, 0.0, ""
    for term in re.split(r"\s+AND\s+", filter_str.strip(), flags=re.IGNORECASE):
        if not term:
            continue
        match = _FILTER_TERM.match(term)
        if match is None:
            raise ChatApiError("INVALID_ARGUMENT", f"filter: cannot parse {term!r}")
        field_name, op, value = match.group("field", "op", "value")
        if field_name == "createTime" and op == ">":
            after = _parse_time(value, "filter createTime")
        elif field_name == "createTime" and op == "<":
            before = _parse_time(value, "filter createTime")
        elif field_name == "thread.name" and op == "=":
            if thread:
                raise ChatApiError(
                    "INVALID_ARGUMENT", "filter: only one thread.name per query"
                )
            thread = value
        else:
            raise ChatApiError(
                "INVALID_ARGUMENT",
                f"filter: unsupported field or operator in {term!r}",
            )
    return after, before, thread


def _parse_order_by(order_by: str | None) -> bool:
    """`orderBy` -> descending?  Default is the emulated createTime ASC."""
    if not order_by:
        return False
    parts = order_by.split()
    if len(parts) > 2 or parts[0] != "createTime":
        raise ChatApiError("INVALID_ARGUMENT", f"orderBy: unsupported {order_by!r}")
    direction = parts[1].upper() if len(parts) == 2 else "ASC"
    if direction not in ("ASC", "DESC"):
        raise ChatApiError("INVALID_ARGUMENT", f"orderBy: unsupported {order_by!r}")
    return direction == "DESC"


_REPLY_OPTIONS = {
    None: REPLY_NEW,
    "MESSAGE_REPLY_OPTION_UNSPECIFIED": REPLY_NEW,
    "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD": REPLY_FALLBACK,
    "REPLY_MESSAGE_OR_FAIL": REPLY_OR_FAIL,
}


def _parse_create(body: dict, reply_option: str | None) -> MessageCreate:
    text = body.get("text", "")
    if not isinstance(text, str) or not text:
        raise ChatApiError("INVALID_ARGUMENT", "text is required")
    if reply_option not in _REPLY_OPTIONS:
        raise ChatApiError(
            "INVALID_ARGUMENT", f"messageReplyOption: unsupported {reply_option!r}"
        )
    thread = body.get("thread") or {}
    if not isinstance(thread, dict):
        raise ChatApiError("INVALID_ARGUMENT", "thread must be an object")
    quote = None
    meta = body.get("quotedMessageMetadata")
    if meta is not None:
        if not isinstance(meta, dict) or not meta.get("name"):
            raise ChatApiError(
                "INVALID_ARGUMENT", "quotedMessageMetadata.name is required"
            )
        if not meta.get("lastUpdateTime"):
            # chat-v1 §3.1: the timestamp is how staleness is detected, so a
            # quote request without one cannot be honoured.
            raise ChatApiError(
                "INVALID_ARGUMENT", "quotedMessageMetadata.lastUpdateTime is required"
            )
        quote = QuoteRequest(
            name=meta["name"],
            last_update_time=_parse_time(
                meta["lastUpdateTime"], "quotedMessageMetadata.lastUpdateTime"
            ),
        )
    attachments = []
    for row in body.get("attachment") or []:
        ref = (row or {}).get("attachmentDataRef", {}).get("resourceName", "")
        if not ref:
            raise ChatApiError(
                "INVALID_ARGUMENT", "attachment.attachmentDataRef.resourceName is required"
            )
        attachments.append(ref)
    return MessageCreate(
        text=text,
        thread=thread.get("name", ""),
        thread_key=thread.get("threadKey", ""),
        reply=_REPLY_OPTIONS[reply_option],
        quote=quote,
        attachments=attachments,
    )


# --- the module ------------------------------------------------------------

class ChatModule(Module):
    name = "chat"
    api_version = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.config = ChatSettings.model_validate(dict(settings))
        if self.config.driver != "mock":
            # `passthrough` is specified but not implemented; naming it must
            # fail at boot, not serve mock answers under a passthrough label.
            raise ValueError(
                f"chat.driver {self.config.driver!r} is not available; "
                "this build ships: mock"
            )
        from jetty.modules.chat.mock import MockChatDriver

        self.driver: ChatDriver = MockChatDriver(
            identity=self.config.identity,
            seed_spaces=[s.model_dump() for s in self.config.seed.spaces],
            seed_messages=[m.model_dump() for m in self.config.seed.messages],
        )

    @property
    def router_prefix(self) -> str:
        return self.mount  # foreign protocol: the router owns /chat entirely

    async def _drive(self, coro: Coroutine) -> Any:
        """One driver call, with chat-v1 §4's error mapping applied."""
        try:
            return await coro
        except DriverRejects as exc:
            raise ChatApiError("INVALID_ARGUMENT", str(exc)) from exc
        except ThreadNotFound as exc:
            raise ChatApiError("NOT_FOUND", f"thread not found: {exc}") from exc
        except ChatApiError:
            raise
        except Exception as exc:
            log.warning("chat driver failure: %r", exc)
            raise ChatApiError("UNAVAILABLE", "chat upstream unavailable") from exc

    async def _require_message(self, name: str) -> Message:
        message = await self._drive(self.driver.get_message(name))
        if message is None:
            raise ChatApiError("NOT_FOUND", f"message not found: {name}")
        return message

    async def _require_space(self, space_id: str) -> Space:
        space = await self._drive(self.driver.get_space(f"spaces/{space_id}"))
        if space is None:
            raise ChatApiError("NOT_FOUND", f"space not found: spaces/{space_id}")
        return space

    def router(self) -> APIRouter:  # noqa: C901 - one route per emulated method
        router = APIRouter(route_class=_ChatRoute)

        # --- spaces ---------------------------------------------------
        @router.get("/v1/spaces")
        async def list_spaces(pageSize: int | None = None, pageToken: str | None = None):
            result = await self._drive(self.driver.list_spaces(_page(pageSize, pageToken)))
            body: dict[str, Any] = {"spaces": [_space_json(s) for s in result.spaces]}
            if result.next_token:
                body["nextPageToken"] = result.next_token
            return body

        @router.get("/v1/spaces:findDirectMessage")
        async def find_direct_message(name: str | None = None):
            if not name:
                raise ChatApiError("INVALID_ARGUMENT", "name is required")
            space = await self._drive(self.driver.find_direct_message(name))
            if space is None:
                raise ChatApiError("NOT_FOUND", f"no direct message with {name}")
            return _space_json(space)

        @router.post("/v1/spaces")
        async def create_space(request: Request):
            body = await _json_body(request, {"displayName", "spaceType"})
            space = await self._drive(
                self.driver.create_space(
                    SpaceCreate(
                        display_name=body.get("displayName", ""),
                        space_type=body.get("spaceType", "SPACE"),
                    )
                )
            )
            return _space_json(space)

        @router.get("/v1/spaces/{space_id}")
        async def get_space(space_id: str):
            return _space_json(await self._require_space(space_id))

        @router.get("/v1/spaces/{space_id}/members")
        async def list_members(
            space_id: str, pageSize: int | None = None, pageToken: str | None = None
        ):
            space = await self._require_space(space_id)
            result = await self._drive(
                self.driver.list_members(space.name, _page(pageSize, pageToken))
            )
            body: dict[str, Any] = {
                "memberships": [_member_json(m) for m in result.members]
            }
            if result.next_token:
                body["nextPageToken"] = result.next_token
            return body

        # --- messages -------------------------------------------------
        @router.get("/v1/spaces/{space_id}/messages")
        async def list_messages(
            space_id: str,
            pageSize: int | None = None,
            pageToken: str | None = None,
            filter: str | None = None,
            orderBy: str | None = None,
        ):
            space = await self._require_space(space_id)
            after, before, thread = _parse_list_filter(filter or "")
            query = MessageQuery(
                page=_page(pageSize, pageToken),
                after=after,
                before=before,
                thread=thread,
                descending=_parse_order_by(orderBy),
            )
            result = await self._drive(self.driver.list_messages(space.name, query))
            body: dict[str, Any] = {
                "messages": [_message_json(m) for m in result.messages]
            }
            if result.next_token:
                body["nextPageToken"] = result.next_token
            return body

        @router.post("/v1/spaces/{space_id}/messages")
        async def create_message(
            space_id: str, request: Request, messageReplyOption: str | None = None
        ):
            space = await self._require_space(space_id)
            body = await _json_body(
                request, {"text", "thread", "quotedMessageMetadata", "attachment"}
            )
            req = _parse_create(body, messageReplyOption)
            message = await self._drive(self.driver.create_message(space.name, req))
            return _message_json(message)

        @router.get("/v1/spaces/{space_id}/messages/{message_id}")
        async def get_message(space_id: str, message_id: str):
            name = f"spaces/{space_id}/messages/{message_id}"
            message = await self._drive(self.driver.get_message(name))
            if message is None:
                raise ChatApiError("NOT_FOUND", f"message not found: {name}")
            return _message_json(message)

        @router.patch("/v1/spaces/{space_id}/messages/{message_id}")
        async def patch_message(
            space_id: str, message_id: str, request: Request, updateMask: str = ""
        ):
            paths = [p.strip() for p in updateMask.split(",") if p.strip()]
            if not paths:
                raise ChatApiError("INVALID_ARGUMENT", "updateMask is required")
            unsupported = [p for p in paths if p != "text"]
            if unsupported:
                # chat-v1 §3.4: only `text` is mandatory here, and §3.5 forbids
                # silently dropping the rest of the mask.
                raise ChatApiError(
                    "INVALID_ARGUMENT",
                    f"updateMask path {unsupported[0]!r} is not supported",
                )
            body = await _json_body(request, {"text"})
            text = body.get("text", "")
            if not isinstance(text, str):
                raise ChatApiError("INVALID_ARGUMENT", "text must be a string")
            name = f"spaces/{space_id}/messages/{message_id}"
            message = await self._drive(
                self.driver.patch_message(name, MessagePatch(text=text))
            )
            if message is None:
                raise ChatApiError("NOT_FOUND", f"message not found: {name}")
            return _message_json(message)

        @router.delete("/v1/spaces/{space_id}/messages/{message_id}")
        async def delete_message(space_id: str, message_id: str):
            name = f"spaces/{space_id}/messages/{message_id}"
            deleted = await self._drive(self.driver.delete_message(name))
            if not deleted:
                raise ChatApiError("NOT_FOUND", f"message not found: {name}")
            return {}

        @router.post("/v1/spaces/{space_id}/messages:search")
        async def search_messages(space_id: str, request: Request):
            if space_id != "-":
                # chat-v1 §3.3: the parent is the literal `spaces/-`.
                raise ChatApiError(
                    "INVALID_ARGUMENT", "search parent must be spaces/-"
                )
            body = await _json_body(
                request, {"filter", "pageSize", "pageToken", "orderBy"}
            )
            filter_str = body.get("filter", "")
            if not isinstance(filter_str, str) or not filter_str:
                raise ChatApiError("INVALID_ARGUMENT", "filter is required")
            page_size = body.get("pageSize")
            if page_size is not None and not isinstance(page_size, int):
                raise ChatApiError("INVALID_ARGUMENT", "pageSize must be an integer")
            result = await self._drive(
                self.driver.search_messages(
                    MessageSearch(
                        filter=filter_str,
                        page=_page(page_size, body.get("pageToken")),
                        descending=_parse_order_by(body.get("orderBy")),
                    )
                )
            )
            body_out: dict[str, Any] = {
                "messages": [_message_json(m) for m in result.messages]
            }
            if result.next_token:
                body_out["nextPageToken"] = result.next_token
            return body_out

        # --- reactions ------------------------------------------------
        @router.post("/v1/spaces/{space_id}/messages/{message_id}/reactions")
        async def create_reaction(space_id: str, message_id: str, request: Request):
            body = await _json_body(request, {"emoji"})
            unicode = ((body.get("emoji") or {}).get("unicode") or "").strip()
            if not unicode:
                raise ChatApiError("INVALID_ARGUMENT", "emoji.unicode is required")
            name = f"spaces/{space_id}/messages/{message_id}"
            await self._require_message(name)
            reaction = await self._drive(
                self.driver.create_reaction(name, Emoji(unicode=unicode))
            )
            return _reaction_json(reaction)

        @router.get("/v1/spaces/{space_id}/messages/{message_id}/reactions")
        async def list_reactions(
            space_id: str,
            message_id: str,
            pageSize: int | None = None,
            pageToken: str | None = None,
        ):
            name = f"spaces/{space_id}/messages/{message_id}"
            await self._require_message(name)
            result = await self._drive(
                self.driver.list_reactions(name, _page(pageSize, pageToken))
            )
            body: dict[str, Any] = {
                "reactions": [_reaction_json(r) for r in result.reactions]
            }
            if result.next_token:
                body["nextPageToken"] = result.next_token
            return body

        @router.delete(
            "/v1/spaces/{space_id}/messages/{message_id}/reactions/{reaction_id}"
        )
        async def delete_reaction(space_id: str, message_id: str, reaction_id: str):
            name = (
                f"spaces/{space_id}/messages/{message_id}/reactions/{reaction_id}"
            )
            deleted = await self._drive(self.driver.delete_reaction(name))
            if not deleted:
                raise ChatApiError("NOT_FOUND", f"reaction not found: {name}")
            return {}

        # --- media ----------------------------------------------------
        @router.post("/upload/v1/spaces/{space_id}/attachments:upload")
        async def upload_attachment(space_id: str, request: Request):
            space = await self._require_space(space_id)
            content = await request.body()
            if len(content) > self.config.upload_max_bytes:
                # chat-v1 §3.7: an oversized upload is an error, never a
                # truncated write.
                raise ChatApiError(
                    "INVALID_ARGUMENT",
                    f"upload exceeds chat.upload_max_bytes "
                    f"({self.config.upload_max_bytes})",
                )
            filename = request.query_params.get("filename", "") or request.headers.get(
                "x-goog-upload-file-name", ""
            )
            upload = Upload(
                filename=filename,
                content_type=request.headers.get(
                    "content-type", "application/octet-stream"
                ),
                size=len(content),
                content=content,
            )
            ref = await self._drive(self.driver.upload_attachment(space.name, upload))
            return {"attachmentDataRef": {"resourceName": ref.resource_name}}

        # --- everything else (chat-v1 §3): emulated-shape 501/404 -----
        @router.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            include_in_schema=False,
        )
        async def unmatched(path: str):
            in_layout = re.match(r"(upload/)?v1/(spaces|users|media)(/|$|:)", path)
            if in_layout:
                raise ChatApiError(
                    "UNIMPLEMENTED", f"not implemented by this subset: /{path}"
                )
            raise ChatApiError("NOT_FOUND", f"no such route: /{path}")

        return router
