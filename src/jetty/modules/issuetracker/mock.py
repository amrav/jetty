"""The ``mock`` driver: an in-memory issue tracker, seeded from configuration.

For development, CI, and conformance runs (issuetracker-v1 §5). No network
I/O; the whole state lives in this object and dies with the process.

Determinism: issue ids are counters from a fixed base, not randomness, so a
test can predict the id a create will return. Time is real (`time.time()`),
because clients poll on `modifiedTime` and a frozen clock would make every
change simultaneous.

Query grammar (issuetracker-v1 §5): the mock supports exactly the documented
floor — ``componentid:``, ``status:`` (a status name, ``open``, or
``closed``), ``assignee:``, ``p:``, and bare words matching the title
case-insensitively. Any other ``field:`` term is ``DriverRejects``: silently
dropping a term would return a wrong result set as if it were right
(issuetracker-v1 §4.6 in spirit).
"""

from __future__ import annotations

import itertools
import time

from jetty.modules.issuetracker.driver import (
    Attachment,
    Comment,
    CommentPage,
    Component,
    DriverRejects,
    FieldChange,
    HotlistEntry,
    Issue,
    IssueCreate,
    IssueModify,
    IssuePage,
    IssueQuery,
    IssueUpdate,
    IssueUpdatePage,
    OPEN_STATUSES,
    Page,
    PRIORITIES,
    Relationship,
    RELATIONSHIP_TYPES,
    STATUSES,
)

#: Seeded and created issues number from here — high enough not to collide
#: with anything a test seeds explicitly.
_ID_BASE = 1000


def _paginate(rows: list, page: Page) -> tuple[list, str]:
    """Offset tokens. Opaque to callers; `str(int)` is enough for memory."""
    try:
        offset = int(page.token) if page.token else 0
    except ValueError:
        raise DriverRejects(f"invalid pageToken {page.token!r}") from None
    if offset < 0:
        raise DriverRejects(f"invalid pageToken {page.token!r}")
    size = page.size if page.size > 0 else 25
    window = rows[offset : offset + size]
    next_token = str(offset + size) if offset + size < len(rows) else ""
    return window, next_token


#: Scalar issue fields a modify may set, and their attribute names.
_SCALAR_FIELDS = {
    "status": "status",
    "priority": "priority",
    "severity": "severity",
    "title": "title",
    "type": "type",
    "assignee": "assignee",
    "verifier": "verifier",
}

#: Collection fields a modify may add to / remove from.
_COLLECTION_FIELDS = {
    "ccs": "ccs",
    "hotlistIds": "hotlist_ids",
    "blockedByIssueIds": "blocked_by",
}

#: orderBy fields (issuetracker-v1 §4.2) -> sort key attribute.
_SORT_FIELDS = {
    "priority": "priority",
    "severity": "severity",
    "created": "created",
    "modified": "modified",
    "assignee": "assignee",
}


class MockIssueTrackerDriver:
    def __init__(
        self,
        identity: str = "jetty@example.com",
        seed_components: list[dict] | None = None,
        seed_issues: list[dict] | None = None,
    ) -> None:
        self._identity = identity
        self._counter = itertools.count(_ID_BASE + 1)
        self._components: dict[int, Component] = {}
        self._issues: dict[int, Issue] = {}
        self._comments: dict[int, list[Comment]] = {}       # issue -> comments
        self._updates: dict[int, list[IssueUpdate]] = {}    # issue -> history
        self._relationships: dict[int, list[Relationship]] = {}
        self._attachments: dict[int, list[Attachment]] = {}
        for row in seed_components or []:
            cid = int(row["component_id"])
            self._components[cid] = Component(component_id=cid)
        for row in seed_issues or []:
            cid = int(row["component_id"])
            if cid not in self._components:
                raise ValueError(
                    f"issuetracker.seed issue names unknown component {cid}"
                )
            self._insert(
                IssueCreate(
                    component_id=cid,
                    title=row["title"],
                    status=row.get("status", "NEW"),
                    type=row.get("type", "BUG"),
                    priority=row.get("priority", "P2"),
                    severity=row.get("severity", "S2"),
                    description=row.get("description", ""),
                    assignee=row.get("assignee", ""),
                ),
                reporter=row.get("reporter", self._identity),
            )

    # --- internals ----------------------------------------------------------
    def _insert(self, req: IssueCreate, *, reporter: str) -> Issue:
        issue_id = next(self._counter)
        stamp = round(time.time(), 6)
        issue = Issue(
            issue_id=issue_id,
            component_id=req.component_id,
            title=req.title,
            type=req.type,
            status=req.status,
            priority=req.priority,
            severity=req.severity,
            reporter=reporter,
            assignee=req.assignee,
            created=stamp,
            modified=stamp,
            description=req.description,
        )
        self._issues[issue_id] = issue
        self._comments[issue_id] = [
            Comment(issue_id=issue_id, number=1, text=req.description)
        ]
        self._updates[issue_id] = []
        return issue

    def _touch(self, issue: Issue) -> None:
        # Strictly monotonic: pollers compare modifiedTime for change
        # detection, and two writes within one microsecond must still differ.
        issue.modified = max(round(time.time(), 6), issue.modified + 1e-6)

    # --- components ---------------------------------------------------------
    async def get_component(self, component_id: int) -> Component | None:
        return self._components.get(component_id)

    # --- listing ------------------------------------------------------------
    def _match(self, issue: Issue, query: str) -> bool:
        for term in query.split():
            field_name, sep, value = term.partition(":")
            value = value.lower()
            if not sep:
                if term.lower() not in issue.title.lower():
                    return False
            elif field_name.lower() == "componentid":
                if str(issue.component_id) != value:
                    return False
            elif field_name.lower() == "status":
                if value == "open":
                    if issue.status not in OPEN_STATUSES:
                        return False
                elif value == "closed":
                    if issue.status in OPEN_STATUSES:
                        return False
                elif value.upper() in STATUSES:
                    if issue.status != value.upper():
                        return False
                else:
                    raise DriverRejects(f"query: unknown status {value!r}")
            elif field_name.lower() == "assignee":
                assignee = issue.assignee.lower()
                if value not in (assignee, assignee.partition("@")[0]):
                    return False
            elif field_name.lower() == "p":
                if value.upper() not in PRIORITIES:
                    raise DriverRejects(f"query: unknown priority {value!r}")
                if issue.priority != value.upper():
                    return False
            else:
                raise DriverRejects(
                    f"query: unsupported term {field_name!r} "
                    "(this driver supports componentid:, status:, assignee:, "
                    "p:, and bare-word title match)"
                )
        return True

    async def list_issues(self, query: IssueQuery) -> IssuePage:
        rows = [i for i in self._issues.values() if self._match(i, query.query)]
        # Stable composite sort: apply keys in reverse significance order.
        rows.sort(key=lambda i: i.issue_id)
        for field_name, descending in reversed(query.order_by):
            attr = _SORT_FIELDS[field_name]
            rows.sort(key=lambda i: getattr(i, attr), reverse=descending)
        window, token = _paginate(rows, query.page)
        return IssuePage(issues=window, next_token=token, total=len(rows))

    async def batch_get_issues(self, issue_ids: list[int]) -> list[Issue]:
        # issuetracker-v1 §4.2: unknown ids are omitted, not an error.
        return [self._issues[i] for i in issue_ids if i in self._issues]

    async def get_issue(self, issue_id: int) -> Issue | None:
        return self._issues.get(issue_id)

    # --- mutation -----------------------------------------------------------
    async def create_issue(self, req: IssueCreate) -> Issue:
        if req.component_id not in self._components:
            raise DriverRejects(
                f"issueState.componentId: no such component {req.component_id}"
            )
        return self._insert(req, reporter=self._identity)

    async def modify_issue(self, issue_id: int, req: IssueModify) -> Issue | None:
        issue = self._issues.get(issue_id)
        if issue is None:
            return None
        changes: list[FieldChange] = []
        for field_name, value in req.sets.items():
            attr = _SCALAR_FIELDS.get(field_name)
            if attr is None:
                raise DriverRejects(f"addMask: cannot set field {field_name!r}")
            old = getattr(issue, attr)
            if old != value:
                setattr(issue, attr, value)
                changes.append(FieldChange(field=field_name, old=old, new=value))
        for deltas, removing in ((req.add, False), (req.remove, True)):
            for field_name, values in deltas.items():
                attr = _COLLECTION_FIELDS.get(field_name)
                if attr is None:
                    raise DriverRejects(
                        f"{'removeMask' if removing else 'addMask'}: "
                        f"cannot edit collection {field_name!r}"
                    )
                current: list = getattr(issue, attr)
                for value in values:
                    if removing and value in current:
                        current.remove(value)
                        changes.append(FieldChange(field=field_name, old=value, new=None))
                    elif not removing and value not in current:
                        current.append(value)
                        changes.append(FieldChange(field=field_name, old=None, new=value))
        comment_number = 0
        if req.comment is not None:
            comment = await self.create_comment(issue_id, req.comment)
            assert comment is not None
            comment_number = comment.number
        if changes or comment_number:
            self._touch(issue)
            self._updates[issue_id].append(
                IssueUpdate(
                    author=self._identity,
                    timestamp=issue.modified,
                    changes=changes,
                    comment_number=comment_number,
                )
            )
        return issue

    # --- relationships ------------------------------------------------------
    async def create_relationship(
        self, issue_id: int, rel: Relationship
    ) -> Relationship | None:
        issue = self._issues.get(issue_id)
        if issue is None:
            return None
        if rel.target_issue_id not in self._issues:
            raise DriverRejects(
                f"issueRelationship.targetIssueId: no such issue {rel.target_issue_id}"
            )
        rows = self._relationships.setdefault(issue_id, [])
        if any(
            r.target_issue_id == rel.target_issue_id and r.type == rel.type
            for r in rows
        ):
            raise DriverRejects("issueRelationship already exists")
        rows.append(rel)
        self._touch(issue)
        return rel

    async def list_relationships(
        self, issue_id: int, type: str
    ) -> list[Relationship] | None:
        if issue_id not in self._issues:
            return None
        assert type in RELATIONSHIP_TYPES  # surface validated
        return [r for r in self._relationships.get(issue_id, []) if r.type == type]

    # --- history ------------------------------------------------------------
    async def list_issue_updates(
        self, issue_id: int, page: Page
    ) -> IssueUpdatePage | None:
        if issue_id not in self._issues:
            return None
        rows = list(reversed(self._updates.get(issue_id, [])))  # newest first
        window, token = _paginate(rows, page)
        return IssueUpdatePage(updates=window, next_token=token, total=len(rows))

    # --- comments -----------------------------------------------------------
    async def create_comment(self, issue_id: int, text: str) -> Comment | None:
        issue = self._issues.get(issue_id)
        if issue is None:
            return None
        rows = self._comments[issue_id]
        comment = Comment(issue_id=issue_id, number=len(rows) + 1, text=text)
        rows.append(comment)
        self._touch(issue)
        return comment

    async def list_comments(self, issue_id: int, page: Page) -> CommentPage | None:
        if issue_id not in self._issues:
            return None
        rows = list(self._comments.get(issue_id, []))
        window, token = _paginate(rows, page)
        return CommentPage(comments=window, next_token=token, total=len(rows))

    async def update_comment(
        self, issue_id: int, number: int, text: str
    ) -> Comment | None:
        issue = self._issues.get(issue_id)
        if issue is None:
            return None
        rows = self._comments.get(issue_id, [])
        if not 1 <= number <= len(rows):
            return None
        rows[number - 1].text = text
        if number == 1:
            issue.description = text  # comment 1 is the description
        self._touch(issue)
        return rows[number - 1]

    # --- attachments --------------------------------------------------------
    async def list_attachments(self, issue_id: int) -> list[Attachment] | None:
        if issue_id not in self._issues:
            return None
        return list(self._attachments.get(issue_id, []))

    # --- hotlists -----------------------------------------------------------
    async def create_hotlist_entry(self, entry: HotlistEntry) -> HotlistEntry:
        issue = self._issues.get(entry.issue_id)
        if issue is None:
            raise DriverRejects(f"hotlistEntry.issueId: no such issue {entry.issue_id}")
        if entry.hotlist_id not in issue.hotlist_ids:
            issue.hotlist_ids.append(entry.hotlist_id)
            self._touch(issue)
        return entry

    async def delete_hotlist_entry(self, hotlist_id: int, issue_id: int) -> bool:
        issue = self._issues.get(issue_id)
        if issue is None or hotlist_id not in issue.hotlist_ids:
            return False
        issue.hotlist_ids.remove(hotlist_id)
        self._touch(issue)
        return True

    async def ping(self) -> None:
        return None
