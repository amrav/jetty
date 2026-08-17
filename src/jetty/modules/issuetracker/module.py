"""The emulated surface (issuetracker-v1 §1–§4): URL layout, translation,
errors.

This module is a foreign protocol on the control listener (SPEC.md §2.1): it
conforms to the API it emulates — `google.devtools.issuetracker.v1`, whose
only public definition is the generated code vendored in chromium's luci-go
repository (issuetracker-v1 §1) — not to SPEC.md §3. Errors use the emulated
shape, and the router owns the whole mount prefix, `/issuetracker/v1/…`, via
`router_prefix`.

Wire conventions follow canonical proto JSON: int64 fields (issue ids,
component ids, hotlist ids) are rendered as strings and accepted as strings
or numbers; int32 fields (comment numbers, totals) are numbers; enums are
their names; timestamps are RFC-3339.

No authentication (issuetracker-v1 §3): stock clients attach `Authorization`,
`x-goog-api-key`, or `?key=`; the surface ignores all three and forwards none
of them to the driver.

Fail-closed (SPEC.md §1.2) at this boundary: a driver failure is the emulated
`UNAVAILABLE`, a surface bug is the emulated `INTERNAL`, and neither is ever
a fabricated `200`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Mapping

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from jetty.modules.base import Module
from jetty.modules.issuetracker.driver import (
    Attachment,
    Comment,
    Component,
    DriverRejects,
    HotlistEntry,
    Issue,
    IssueCreate,
    IssueModify,
    IssueQuery,
    IssueTrackerDriver,
    IssueUpdate,
    Page,
    PRIORITIES,
    Relationship,
    RELATIONSHIP_TYPES,
    SEVERITIES,
    STATUSES,
    TYPES,
    VIEW_BASIC,
    VIEW_FULL,
)

log = logging.getLogger("jetty.issuetracker")

#: Emulated status -> HTTP code. Closed set.
_STATUS_HTTP = {
    "INVALID_ARGUMENT": 400,
    "NOT_FOUND": 404,
    "UNIMPLEMENTED": 501,
    "UNAVAILABLE": 503,
    "INTERNAL": 500,
}


class TrackerApiError(Exception):
    """An error in the emulated API's shape (issuetracker-v1 §2)."""

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


class _TrackerRoute(APIRoute):
    """Every response — including failures — leaves in the emulated shape.

    App-level exception handlers render the SPEC.md §3.1 envelope, which is
    exactly wrong here, so nothing may escape this route class. Unexpected
    exceptions become the emulated `INTERNAL` with a generic message: the
    exception text stays in the log (SPEC.md §1.4).
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except TrackerApiError as exc:
                return exc.response()
            except RequestValidationError as exc:
                return TrackerApiError(
                    "INVALID_ARGUMENT", f"invalid request: {exc.errors()[0]['loc']}"
                ).response()
            except Exception:
                log.exception("issuetracker surface error on %s", request.url.path)
                return TrackerApiError("INTERNAL", "internal error").response()

        return wrapped


# --- configuration ----------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeedComponent(_Strict):
    component_id: int
    name: str = ""  # config sugar only; the emulated Component carries no name


class SeedIssue(_Strict):
    component_id: int
    title: str
    status: str = "NEW"
    type: str = "BUG"
    priority: str = "P2"
    severity: str = "S2"
    reporter: str = ""
    assignee: str = ""
    description: str = ""


class TrackerSeed(_Strict):
    components: list[SeedComponent] = Field(default_factory=list)
    issues: list[SeedIssue] = Field(default_factory=list)


class TrackerSettings(_Strict):
    enabled: bool = False
    driver: str = "mock"
    identity: str = "jetty@example.com"
    seed: TrackerSeed = Field(default_factory=TrackerSeed)


# --- wire helpers -----------------------------------------------------------

def _rfc3339(t: float) -> str:
    stamp = datetime.fromtimestamp(t, tz=timezone.utc)
    return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_id64(value: Any, field_name: str) -> int:
    """int64 on the wire: a decimal string or a number (issuetracker-v1 §4)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TrackerApiError("INVALID_ARGUMENT", f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except ValueError:
        raise TrackerApiError(
            "INVALID_ARGUMENT", f"{field_name} must be an integer"
        ) from None
    if parsed <= 0:
        raise TrackerApiError("INVALID_ARGUMENT", f"{field_name} must be positive")
    return parsed


def _user_json(email: str) -> dict:
    return {"emailAddress": email}


def _parse_user(value: Any, field_name: str) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("emailAddress"), str):
        raise TrackerApiError(
            "INVALID_ARGUMENT", f"{field_name} must be a User with emailAddress"
        )
    return value["emailAddress"]


def _comment_json(comment: Comment) -> dict:
    return {
        "issueId": str(comment.issue_id),
        "commentNumber": comment.number,
        "comment": comment.text,
    }


def _issue_json(issue: Issue, view: str) -> dict:
    state: dict[str, Any] = {
        "componentId": str(issue.component_id),
        "type": issue.type,
        "status": issue.status,
        "priority": issue.priority,
        "severity": issue.severity,
        "title": issue.title,
        "reporter": _user_json(issue.reporter),
    }
    if issue.assignee:
        state["assignee"] = _user_json(issue.assignee)
    if issue.verifier:
        state["verifier"] = _user_json(issue.verifier)
    if issue.ccs:
        state["ccs"] = [_user_json(u) for u in issue.ccs]
    if issue.hotlist_ids:
        state["hotlistIds"] = [str(h) for h in issue.hotlist_ids]
    if issue.blocked_by:
        state["blockedByIssueIds"] = [str(i) for i in issue.blocked_by]
    if issue.blocking:
        state["blockingIssueIds"] = [str(i) for i in issue.blocking]
    body: dict[str, Any] = {
        "issueId": str(issue.issue_id),
        "issueState": state,
        "createdTime": _rfc3339(issue.created),
        "modifiedTime": _rfc3339(issue.modified),
    }
    if view == VIEW_FULL:
        body["description"] = _comment_json(
            Comment(issue_id=issue.issue_id, number=1, text=issue.description)
        )
    return body


def _field_value_json(value: str | int | None) -> Any:
    return str(value) if isinstance(value, int) else value


def _update_json(update: IssueUpdate) -> dict:
    body: dict[str, Any] = {
        "author": _user_json(update.author),
        "timestamp": _rfc3339(update.timestamp),
        "fieldUpdates": [
            {
                "field": c.field,
                "singleValueUpdate": {
                    "oldValue": _field_value_json(c.old),
                    "newValue": _field_value_json(c.new),
                },
            }
            for c in update.changes
        ],
    }
    if update.comment_number:
        body["commentNumber"] = update.comment_number
    return body


def _relationship_json(rel: Relationship) -> dict:
    return {"targetIssueId": str(rel.target_issue_id)}


def _attachment_json(a: Attachment) -> dict:
    return {
        "attachmentId": str(a.attachment_id),
        "filename": a.filename,
        "contentType": a.content_type,
        "length": str(a.length),
        "attachmentDataRef": {"resourceName": f"attachment:{a.attachment_id}"},
    }


def _hotlist_entry_json(entry: HotlistEntry) -> dict:
    body: dict[str, Any] = {"issueId": str(entry.issue_id)}
    if entry.position:
        body["position"] = entry.position
    return body


# --- request parsing --------------------------------------------------------

async def _json_body(request: Request, allowed: set[str]) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise TrackerApiError("INVALID_ARGUMENT", "request body is not JSON") from None
    if not isinstance(body, dict):
        raise TrackerApiError("INVALID_ARGUMENT", "request body must be an object")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise TrackerApiError(
            "INVALID_ARGUMENT", f"unknown field {unknown[0]!r} in request body"
        )
    return body


def _page(page_size: int | None, page_token: str | None) -> Page:
    # issuetracker-v1 §4.2: default 25, maximum 500.
    size = page_size if page_size is not None else 25
    if size < 0:
        raise TrackerApiError("INVALID_ARGUMENT", "pageSize must not be negative")
    return Page(size=min(size, 500) or 25, token=page_token or "")


def _parse_view(view: str | None) -> str:
    if view in (None, "", "ISSUE_VIEW_UNSPECIFIED", VIEW_BASIC):
        return VIEW_BASIC
    if view == VIEW_FULL:
        return VIEW_FULL
    raise TrackerApiError("INVALID_ARGUMENT", f"view: unsupported {view!r}")


#: orderBy fields the interface sorts on (issuetracker-v1 §4.2).
_ORDER_FIELDS = {"priority", "severity", "created", "modified", "assignee"}


def _parse_order_by(order_by: str | None) -> list[tuple[str, bool]]:
    if not order_by:
        return []
    parsed: list[tuple[str, bool]] = []
    for part in order_by.split(","):
        words = part.split()
        if not 1 <= len(words) <= 2 or words[0] not in _ORDER_FIELDS:
            raise TrackerApiError("INVALID_ARGUMENT", f"orderBy: unsupported {part.strip()!r}")
        direction = words[1].lower() if len(words) == 2 else "asc"
        if direction not in ("asc", "desc"):
            raise TrackerApiError("INVALID_ARGUMENT", f"orderBy: unsupported {part.strip()!r}")
        parsed.append((words[0], direction == "desc"))
    return parsed


def _parse_enum(value: Any, allowed: frozenset[str], field_name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise TrackerApiError(
            "INVALID_ARGUMENT",
            f"{field_name}: must be one of {', '.join(sorted(allowed))}",
        )
    return value


#: Scalar addMask fields -> how the wire value becomes a driver value.
_MODIFY_SCALARS: dict[str, Callable[[Any], str | int]] = {
    "status": lambda v: _parse_enum(v, STATUSES, "add.status"),
    "priority": lambda v: _parse_enum(v, PRIORITIES, "add.priority"),
    "severity": lambda v: _parse_enum(v, SEVERITIES, "add.severity"),
    "type": lambda v: _parse_enum(v, TYPES, "add.type"),
    "title": lambda v: v if isinstance(v, str) and v else _bad("add.title must be a non-empty string"),
    "assignee": lambda v: _parse_user(v, "add.assignee"),
    "verifier": lambda v: _parse_user(v, "add.verifier"),
}

#: Collection mask fields -> element parser.
_MODIFY_COLLECTIONS: dict[str, Callable[[Any, str], Any]] = {
    "ccs": lambda v, ctx: _parse_user(v, ctx),
    "hotlistIds": lambda v, ctx: _parse_id64(v, ctx),
    "blockedByIssueIds": lambda v, ctx: _parse_id64(v, ctx),
}


def _bad(message: str) -> Any:
    raise TrackerApiError("INVALID_ARGUMENT", message)


def _parse_mask(mask: Any, field_name: str) -> list[str]:
    if mask is None:
        return []
    if not isinstance(mask, str):
        raise TrackerApiError("INVALID_ARGUMENT", f"{field_name} must be a string")
    return [p.strip() for p in mask.split(",") if p.strip()]


def _masked_deltas(
    state: Any, mask_fields: list[str], mask_name: str, state_name: str,
    *, scalars_allowed: bool,
) -> tuple[dict[str, str | int], dict[str, list]]:
    """Resolve one (state, mask) pair into scalar sets and collection deltas."""
    if state is None:
        state = {}
    if not isinstance(state, dict):
        raise TrackerApiError("INVALID_ARGUMENT", f"{state_name} must be an IssueState")
    sets: dict[str, str | int] = {}
    collections: dict[str, list] = {}
    for field_name in mask_fields:
        if field_name in _MODIFY_SCALARS:
            if not scalars_allowed:
                raise TrackerApiError(
                    "INVALID_ARGUMENT",
                    f"{mask_name}: {field_name!r} is not a collection field",
                )
            if field_name not in state:
                raise TrackerApiError(
                    "INVALID_ARGUMENT",
                    f"{mask_name} names {field_name!r} but {state_name} does not carry it",
                )
            sets[field_name] = _MODIFY_SCALARS[field_name](state[field_name])
        elif field_name in _MODIFY_COLLECTIONS:
            values = state.get(field_name)
            if not isinstance(values, list):
                raise TrackerApiError(
                    "INVALID_ARGUMENT",
                    f"{mask_name} names {field_name!r} but {state_name}.{field_name} "
                    "is not a list",
                )
            parse = _MODIFY_COLLECTIONS[field_name]
            collections[field_name] = [
                parse(v, f"{state_name}.{field_name}") for v in values
            ]
        else:
            # issuetracker-v1 §4.6: never silently drop a masked field.
            raise TrackerApiError(
                "INVALID_ARGUMENT", f"{mask_name}: unsupported field {field_name!r}"
            )
    return sets, collections


def _parse_modify(body: dict) -> IssueModify:
    add_mask = _parse_mask(body.get("addMask"), "addMask")
    remove_mask = _parse_mask(body.get("removeMask"), "removeMask")
    sets, add = _masked_deltas(
        body.get("add"), add_mask, "addMask", "add", scalars_allowed=True
    )
    removed_sets, remove = _masked_deltas(
        body.get("remove"), remove_mask, "removeMask", "remove", scalars_allowed=False
    )
    assert not removed_sets
    comment = None
    if "issueComment" in body:
        raw = body["issueComment"]
        if not isinstance(raw, dict) or not isinstance(raw.get("comment"), str):
            raise TrackerApiError(
                "INVALID_ARGUMENT", "issueComment must carry a comment string"
            )
        comment = raw["comment"]
    if not sets and not add and not remove and comment is None:
        # issuetracker-v1 §4.3: an empty modify is an error, not a no-op.
        raise TrackerApiError(
            "INVALID_ARGUMENT", "modify names no masked field and carries no comment"
        )
    return IssueModify(sets=sets, add=add, remove=remove, comment=comment)


def _parse_create(body: dict) -> IssueCreate:
    if "templateOptions" in body:
        # The mock applies no templates; honouring the field silently would
        # violate issuetracker-v1 §4.6.
        raise TrackerApiError("INVALID_ARGUMENT", "templateOptions is not supported")
    issue = body.get("issue")
    if not isinstance(issue, dict):
        raise TrackerApiError("INVALID_ARGUMENT", "issue is required")
    state = issue.get("issueState")
    if not isinstance(state, dict):
        raise TrackerApiError("INVALID_ARGUMENT", "issue.issueState is required")
    # issuetracker-v1 §4.3: the required minimum, rejected incomplete.
    for required in ("componentId", "title", "status", "type", "priority", "severity"):
        if required not in state:
            raise TrackerApiError(
                "INVALID_ARGUMENT", f"issue.issueState.{required} is required"
            )
    title = state["title"]
    if not isinstance(title, str) or not title:
        raise TrackerApiError(
            "INVALID_ARGUMENT", "issue.issueState.title must be a non-empty string"
        )
    description = ""
    if "description" in issue:
        raw = issue["description"]
        if not isinstance(raw, dict) or not isinstance(raw.get("comment"), str):
            raise TrackerApiError(
                "INVALID_ARGUMENT", "issue.description must carry a comment string"
            )
        description = raw["comment"]
    assignee = ""
    if "assignee" in state:
        assignee = _parse_user(state["assignee"], "issue.issueState.assignee")
    return IssueCreate(
        component_id=_parse_id64(state["componentId"], "issue.issueState.componentId"),
        title=title,
        status=_parse_enum(state["status"], STATUSES, "issue.issueState.status"),
        type=_parse_enum(state["type"], TYPES, "issue.issueState.type"),
        priority=_parse_enum(state["priority"], PRIORITIES, "issue.issueState.priority"),
        severity=_parse_enum(state["severity"], SEVERITIES, "issue.issueState.severity"),
        description=description,
        assignee=assignee,
    )


# --- the module -------------------------------------------------------------

class IssueTrackerModule(Module):
    name = "issuetracker"
    api_version = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.config = TrackerSettings.model_validate(dict(settings))
        if self.config.driver != "mock":
            # `passthrough` is specified but not implemented; naming it must
            # fail at boot, not serve mock answers under a passthrough label.
            raise ValueError(
                f"issuetracker.driver {self.config.driver!r} is not available; "
                "this build ships: mock"
            )
        for issue in self.config.seed.issues:
            for value, allowed, key in (
                (issue.status, STATUSES, "status"),
                (issue.type, TYPES, "type"),
                (issue.priority, PRIORITIES, "priority"),
                (issue.severity, SEVERITIES, "severity"),
            ):
                if value not in allowed:
                    raise ValueError(
                        f"issuetracker.seed issue {issue.title!r}: "
                        f"invalid {key} {value!r}"
                    )
        from jetty.modules.issuetracker.mock import MockIssueTrackerDriver

        self.driver: IssueTrackerDriver = MockIssueTrackerDriver(
            identity=self.config.identity,
            seed_components=[c.model_dump() for c in self.config.seed.components],
            seed_issues=[i.model_dump() for i in self.config.seed.issues],
        )

    @property
    def router_prefix(self) -> str:
        return self.mount  # foreign protocol: the router owns /issuetracker

    async def _drive(self, coro: Coroutine) -> Any:
        """One driver call, with issuetracker-v1 §5's error mapping applied."""
        try:
            return await coro
        except DriverRejects as exc:
            raise TrackerApiError("INVALID_ARGUMENT", str(exc)) from exc
        except TrackerApiError:
            raise
        except Exception as exc:
            log.warning("issuetracker driver failure: %r", exc)
            raise TrackerApiError("UNAVAILABLE", "issue tracker upstream unavailable") from exc

    def _issue_path_id(self, issue_id: str) -> int:
        return _parse_id64(issue_id, "issueId")

    async def _require(self, result: Any, what: str) -> Any:
        if result is None:
            raise TrackerApiError("NOT_FOUND", f"{what} not found")
        return result

    def router(self) -> APIRouter:  # noqa: C901 - one route per emulated method
        router = APIRouter(route_class=_TrackerRoute)

        # --- components -----------------------------------------------
        @router.get("/v1/components/{component_id}")
        async def get_component(component_id: str):
            cid = _parse_id64(component_id, "componentId")
            component: Component = await self._require(
                await self._drive(self.driver.get_component(cid)),
                f"component {cid}",
            )
            return {
                "componentId": str(component.component_id),
                "isArchived": component.is_archived,
            }

        # --- issues: list / batchGet / get ----------------------------
        @router.get("/v1/issues")
        async def list_issues(
            query: str | None = None,
            orderBy: str | None = None,
            pageSize: int | None = None,
            pageToken: str | None = None,
            view: str | None = None,
        ):
            if not query:
                # issuetracker-v1 §4.2: query is required.
                raise TrackerApiError("INVALID_ARGUMENT", "query is required")
            resolved_view = _parse_view(view)
            result = await self._drive(
                self.driver.list_issues(
                    IssueQuery(
                        query=query,
                        order_by=_parse_order_by(orderBy),
                        page=_page(pageSize, pageToken),
                    )
                )
            )
            body: dict[str, Any] = {
                "issues": [_issue_json(i, resolved_view) for i in result.issues],
                "totalSize": result.total,
            }
            if result.next_token:
                body["nextPageToken"] = result.next_token
            return body

        @router.get("/v1/issues:batchGet")
        async def batch_get_issues(
            issueIds: list[str] = Query(default=[]), view: str | None = None
        ):
            resolved_view = _parse_view(view)
            ids = [_parse_id64(i, "issueIds") for i in issueIds]
            issues = await self._drive(self.driver.batch_get_issues(ids))
            return {"issues": [_issue_json(i, resolved_view) for i in issues]}

        @router.get("/v1/issues/{issue_id}")
        async def get_issue(issue_id: str, view: str | None = None):
            resolved_view = _parse_view(view)
            iid = self._issue_path_id(issue_id)
            issue = await self._require(
                await self._drive(self.driver.get_issue(iid)), f"issue {iid}"
            )
            return _issue_json(issue, resolved_view)

        # --- issues: create / modify ----------------------------------
        @router.post("/v1/issues")
        async def create_issue(request: Request):
            body = await _json_body(request, {"issue", "templateOptions"})
            issue = await self._drive(self.driver.create_issue(_parse_create(body)))
            return _issue_json(issue, VIEW_FULL)

        @router.post("/v1/issues/{issue_id}:modify")
        async def modify_issue(issue_id: str, request: Request):
            iid = self._issue_path_id(issue_id)
            body = await _json_body(
                request, {"add", "addMask", "remove", "removeMask", "issueComment"}
            )
            req = _parse_modify(body)
            issue = await self._require(
                await self._drive(self.driver.modify_issue(iid, req)), f"issue {iid}"
            )
            return _issue_json(issue, VIEW_FULL)

        # --- relationships --------------------------------------------
        @router.post("/v1/issues/{issue_id}/relationships")
        async def create_relationship(
            issue_id: str, request: Request, relationshipType: str | None = None
        ):
            iid = self._issue_path_id(issue_id)
            rel_type = _parse_enum(
                relationshipType, RELATIONSHIP_TYPES, "relationshipType"
            )
            body = await _json_body(request, {"targetIssueId"})
            target = _parse_id64(body.get("targetIssueId"), "targetIssueId")
            rel = await self._require(
                await self._drive(
                    self.driver.create_relationship(
                        iid, Relationship(target_issue_id=target, type=rel_type)
                    )
                ),
                f"issue {iid}",
            )
            return _relationship_json(rel)

        @router.get("/v1/issues/{issue_id}/relationships")
        async def list_relationships(issue_id: str, relationshipType: str | None = None):
            iid = self._issue_path_id(issue_id)
            rel_type = _parse_enum(
                relationshipType, RELATIONSHIP_TYPES, "relationshipType"
            )
            rows = await self._require(
                await self._drive(self.driver.list_relationships(iid, rel_type)),
                f"issue {iid}",
            )
            return {"issueRelationships": [_relationship_json(r) for r in rows]}

        # --- history --------------------------------------------------
        @router.get("/v1/issues/{issue_id}/issueUpdates")
        async def list_issue_updates(
            issue_id: str, pageSize: int | None = None, pageToken: str | None = None
        ):
            iid = self._issue_path_id(issue_id)
            result = await self._require(
                await self._drive(
                    self.driver.list_issue_updates(iid, _page(pageSize, pageToken))
                ),
                f"issue {iid}",
            )
            body: dict[str, Any] = {
                "issueUpdates": [_update_json(u) for u in result.updates],
                "totalSize": result.total,
            }
            if result.next_token:
                body["nextPageToken"] = result.next_token
            return body

        # --- comments -------------------------------------------------
        @router.post("/v1/issues/{issue_id}/comments")
        async def create_comment(issue_id: str, request: Request):
            iid = self._issue_path_id(issue_id)
            body = await _json_body(request, {"comment"})
            text = body.get("comment")
            if not isinstance(text, str) or not text:
                raise TrackerApiError(
                    "INVALID_ARGUMENT", "comment must be a non-empty string"
                )
            comment = await self._require(
                await self._drive(self.driver.create_comment(iid, text)),
                f"issue {iid}",
            )
            return _comment_json(comment)

        @router.get("/v1/issues/{issue_id}/comments")
        async def list_comments(
            issue_id: str, pageSize: int | None = None, pageToken: str | None = None
        ):
            iid = self._issue_path_id(issue_id)
            result = await self._require(
                await self._drive(
                    self.driver.list_comments(iid, _page(pageSize, pageToken))
                ),
                f"issue {iid}",
            )
            body: dict[str, Any] = {
                "issueComments": [_comment_json(c) for c in result.comments],
                "totalSize": result.total,
            }
            if result.next_token:
                body["nextPageToken"] = result.next_token
            return body

        @router.put("/v1/issues/{issue_id}/comments/{comment_number}")
        async def update_comment(issue_id: str, comment_number: int, request: Request):
            iid = self._issue_path_id(issue_id)
            body = await _json_body(request, {"comment"})
            text = body.get("comment")
            if not isinstance(text, str) or not text:
                raise TrackerApiError(
                    "INVALID_ARGUMENT", "comment must be a non-empty string"
                )
            comment = await self._require(
                await self._drive(self.driver.update_comment(iid, comment_number, text)),
                f"comment {comment_number} on issue {iid}",
            )
            return _comment_json(comment)

        # --- attachments ----------------------------------------------
        @router.get("/v1/issues/{issue_id}/attachments")
        async def list_attachments(issue_id: str):
            iid = self._issue_path_id(issue_id)
            rows = await self._require(
                await self._drive(self.driver.list_attachments(iid)), f"issue {iid}"
            )
            return {"attachments": [_attachment_json(a) for a in rows]}

        # --- hotlists -------------------------------------------------
        @router.post("/v1/hotlists/{hotlist_id}/entries")
        async def create_hotlist_entry(hotlist_id: str, request: Request):
            hid = _parse_id64(hotlist_id, "hotlistId")
            body = await _json_body(request, {"issueId", "position"})
            iid = _parse_id64(body.get("issueId"), "hotlistEntry.issueId")
            position = body.get("position", 0)
            if not isinstance(position, int) or isinstance(position, bool) or position < 0:
                raise TrackerApiError(
                    "INVALID_ARGUMENT", "hotlistEntry.position must be a non-negative integer"
                )
            entry = await self._drive(
                self.driver.create_hotlist_entry(
                    HotlistEntry(hotlist_id=hid, issue_id=iid, position=position)
                )
            )
            return _hotlist_entry_json(entry)

        @router.delete("/v1/hotlists/{hotlist_id}/entries/{issue_id}")
        async def delete_hotlist_entry(hotlist_id: str, issue_id: str):
            hid = _parse_id64(hotlist_id, "hotlistId")
            iid = self._issue_path_id(issue_id)
            deleted = await self._drive(self.driver.delete_hotlist_entry(hid, iid))
            if not deleted:
                raise TrackerApiError(
                    "NOT_FOUND", f"issue {iid} is not on hotlist {hid}"
                )
            return {}

        # --- everything else (issuetracker-v1 §4): 501 / 404 ----------
        @router.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            include_in_schema=False,
        )
        async def unmatched(path: str):
            in_layout = re.match(r"v1/(issues|components|hotlists)(/|$|:)", path)
            if in_layout:
                raise TrackerApiError(
                    "UNIMPLEMENTED", f"not implemented by this subset: /{path}"
                )
            raise TrackerApiError("NOT_FOUND", f"no such route: /{path}")

        return router
