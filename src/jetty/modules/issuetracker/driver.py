"""The internal representation and the ``IssueTrackerDriver`` protocol
(issuetracker-v1 §5).

The surface (module.py) translates the emulated API's wire shapes into these
types and dispatches to a driver; a driver translates them to whatever the
local tracker speaks. Nothing in this file knows about URLs, JSON, or int64
string encoding.

Times are unix seconds (float) throughout; the surface owns RFC-3339.
Identifiers are plain ints — issue ids, component ids, hotlist ids — exactly
as the emulated API defines them (issuetracker-v1 §4); comment numbers are
the 1-based position within the issue.

Error contract (issuetracker-v1 §5): a driver returns ``None``/``False`` for
a resource that does not exist and raises for an unreachable upstream.
``DriverRejects`` refines that for a request the driver cannot honour as
stated — an unsupported query term, a mask field it cannot set, an unknown
component on create (issuetracker-v1 §4.6). The surface maps it to the
emulated ``INVALID_ARGUMENT``. Anything else a driver raises is an
unreachable upstream: the emulated ``UNAVAILABLE``, never a fabricated
success (SPEC.md §1.2).
"""

from __future__ import annotations

from contextvars import ContextVar

from dataclasses import dataclass, field
from typing import Protocol


class DriverRejects(Exception):
    """The driver cannot honour the request as stated; the message names why."""


# --- the emulated vocabulary (issuetracker-v1 §4.1) -------------------------

STATUSES = frozenset({
    "NEW", "ASSIGNED", "ACCEPTED", "FIXED", "VERIFIED", "NOT_REPRODUCIBLE",
    "INTENDED_BEHAVIOR", "OBSOLETE", "INFEASIBLE", "DUPLICATE", "INACTIVE",
})
#: `status:open` in the mock's query grammar.
OPEN_STATUSES = frozenset({"NEW", "ASSIGNED", "ACCEPTED"})

TYPES = frozenset({
    "BUG", "FEATURE_REQUEST", "CUSTOMER_ISSUE", "INTERNAL_CLEANUP", "PROCESS",
    "VULNERABILITY", "PRIVACY_ISSUE", "PORTFOLIO", "PROGRAM", "PROJECT",
    "FEATURE", "MILESTONE", "EPIC", "STORY", "TASK",
})

PRIORITIES = frozenset({"P0", "P1", "P2", "P3", "P4"})
SEVERITIES = frozenset({"S0", "S1", "S2", "S3", "S4"})
RELATIONSHIP_TYPES = frozenset({"CHILD", "DEPENDENCY", "LINKED"})

VIEW_BASIC = "BASIC"
VIEW_FULL = "FULL"


# --- pages ------------------------------------------------------------------

@dataclass
class Page:
    """Pagination as the emulated API sees it; tokens are driver-opaque."""

    size: int = 25
    token: str = ""


# --- resources --------------------------------------------------------------

@dataclass
class Component:
    component_id: int
    is_archived: bool = False


@dataclass
class Issue:
    issue_id: int
    component_id: int
    title: str
    type: str = "BUG"
    status: str = "NEW"
    priority: str = "P2"
    severity: str = "S2"
    reporter: str = ""             # email address; "" = absent
    assignee: str = ""
    verifier: str = ""
    ccs: list[str] = field(default_factory=list)
    hotlist_ids: list[int] = field(default_factory=list)
    blocked_by: list[int] = field(default_factory=list)
    blocking: list[int] = field(default_factory=list)
    created: float = 0.0
    modified: float = 0.0
    #: Comment 1's text (issuetracker-v1 §4.3); the full comment list lives
    #: with the driver.
    description: str = ""


@dataclass
class IssuePage:
    issues: list[Issue]
    next_token: str = ""
    total: int = 0


@dataclass
class IssueQuery:
    """issuetracker-v1 §4.2. The query string's grammar is driver-owned; the
    ordering is the interface's (parsed and validated by the surface)."""

    query: str
    #: (field, descending) pairs, primary first. Fields: priority, severity,
    #: created, modified, assignee.
    order_by: list[tuple[str, bool]] = field(default_factory=list)
    page: Page = field(default_factory=Page)


@dataclass
class IssueCreate:
    """issuetracker-v1 §4.3's required fields, enums pre-validated by the
    surface; the driver validates the component."""

    component_id: int
    title: str
    status: str
    type: str
    priority: str
    severity: str
    description: str = ""
    assignee: str = ""


@dataclass
class IssueModify:
    """The masked read-modify-write (issuetracker-v1 §4.3), already resolved:
    ``sets`` carries scalar fields from add/addMask, ``add``/``remove`` carry
    collection deltas, and ``comment`` is recorded atomically when present."""

    sets: dict[str, str | int] = field(default_factory=dict)
    add: dict[str, list] = field(default_factory=dict)
    remove: dict[str, list] = field(default_factory=dict)
    comment: str | None = None


@dataclass
class Comment:
    issue_id: int
    number: int                    # 1-based; 1 is the description
    text: str


@dataclass
class CommentPage:
    comments: list[Comment]
    next_token: str = ""
    total: int = 0


@dataclass
class FieldChange:
    field: str
    old: str | int | None
    new: str | int | None


@dataclass
class IssueUpdate:
    """One entry in the issue's change history (issuetracker-v1 §4.4)."""

    author: str
    timestamp: float
    changes: list[FieldChange] = field(default_factory=list)
    comment_number: int = 0        # 0 = no comment recorded with the change


@dataclass
class IssueUpdatePage:
    updates: list[IssueUpdate]
    next_token: str = ""
    total: int = 0


@dataclass
class Relationship:
    target_issue_id: int
    type: str                      # RELATIONSHIP_TYPES


@dataclass
class Attachment:
    attachment_id: int
    filename: str
    content_type: str
    length: int


@dataclass
class HotlistEntry:
    hotlist_id: int
    issue_id: int
    position: int = 0


# --- the protocol -----------------------------------------------------------

#: Request headers the surface was configured to forward (issuetracker-v1
#: §3), bound around each driver call. A driver whose upstream authenticates
#: per caller reads them; drivers with a service identity (the mock) ignore
#: them. A ContextVar rather than a parameter: it is per-request state, not
#: part of any operation's meaning, and threading it through every method
#: would put transport detail into the domain interface.
forwarded_headers: ContextVar[dict[str, str]] = ContextVar(
    "issuetracker_forwarded_headers", default={}
)


class IssueTrackerDriver(Protocol):
    async def get_component(self, component_id: int) -> Component | None: ...
    async def list_issues(self, query: IssueQuery) -> IssuePage: ...
    async def batch_get_issues(self, issue_ids: list[int]) -> list[Issue]: ...
    async def get_issue(self, issue_id: int) -> Issue | None: ...
    async def create_issue(self, req: IssueCreate) -> Issue: ...
    async def modify_issue(self, issue_id: int, req: IssueModify) -> Issue | None: ...
    async def create_relationship(
        self, issue_id: int, rel: Relationship
    ) -> Relationship | None: ...
    async def list_relationships(
        self, issue_id: int, type: str
    ) -> list[Relationship] | None: ...
    async def list_issue_updates(self, issue_id: int, page: Page) -> IssueUpdatePage | None: ...
    async def create_comment(self, issue_id: int, text: str) -> Comment | None: ...
    async def list_comments(self, issue_id: int, page: Page) -> CommentPage | None: ...
    async def update_comment(self, issue_id: int, number: int, text: str) -> Comment | None: ...
    async def list_attachments(self, issue_id: int) -> list[Attachment] | None: ...
    async def create_hotlist_entry(self, entry: HotlistEntry) -> HotlistEntry: ...
    async def delete_hotlist_entry(self, hotlist_id: int, issue_id: int) -> bool: ...
    async def ping(self) -> None: ...
