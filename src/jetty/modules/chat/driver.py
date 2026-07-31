"""The internal representation and the ``ChatDriver`` protocol (chat-v1 §4).

The surface (module.py) translates the emulated API's wire shapes into these
types and dispatches to a driver; a driver translates them to whatever the
local chat service speaks. Nothing in this file knows about URLs, JSON, or the
emulated API's field names.

Times are unix seconds (float) throughout; the surface owns RFC-3339.
Resource names are the emulated API's full forms (``spaces/X``,
``spaces/X/messages/Y``, …) — they are the shared vocabulary of surface and
driver, not a wire detail.

Error contract (chat-v1 §4): a driver returns ``None``/``False`` for a
resource that does not exist and raises for an unreachable upstream. Two typed
exceptions refine that for conditions the emulated API distinguishes:

- ``DriverRejects``   — the driver cannot honour the request as stated
                        (stale quote timestamp, bad page token, unsupported
                        field). Surface: ``INVALID_ARGUMENT`` (chat-v1 §3.5).
- ``ThreadNotFound``  — a reply demanded an existing thread
                        (``REPLY_MESSAGE_OR_FAIL``) and there is none.
                        Surface: ``NOT_FOUND``.

Anything else a driver raises is an unreachable upstream: the surface maps it
to the emulated ``UNAVAILABLE``, never to a fabricated success (SPEC.md §1.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class DriverRejects(Exception):
    """The driver cannot honour the request as stated; the message names why."""


class ThreadNotFound(Exception):
    """``REPLY_MESSAGE_OR_FAIL`` named a thread the upstream does not have."""


# --- pages -----------------------------------------------------------------

@dataclass
class Page:
    """Pagination as the emulated API sees it; tokens are driver-opaque."""

    size: int = 25
    token: str = ""


@dataclass
class MessageQuery:
    """chat-v1 §3.2: the mandatory list constraints, beside pagination."""

    page: Page = field(default_factory=Page)
    after: float = 0.0        # createTime >  (0 = unbounded)
    before: float = 0.0       # createTime <  (0 = unbounded)
    thread: str = ""          # thread resource name ("" = whole space)
    descending: bool = False  # orderBy createTime DESC


@dataclass
class MessageSearch:
    """chat-v1 §3.3: search across every visible space."""

    filter: str
    page: Page = field(default_factory=Page)
    descending: bool = False


# --- resources -------------------------------------------------------------

@dataclass
class Space:
    name: str                     # spaces/X
    display_name: str = ""
    space_type: str = "SPACE"     # SPACE | DIRECT_MESSAGE | GROUP_CHAT


@dataclass
class SpaceCreate:
    display_name: str = ""
    space_type: str = "SPACE"


@dataclass
class SpacePage:
    spaces: list[Space]
    next_token: str = ""


@dataclass
class Member:
    name: str                     # spaces/X/members/N
    user: str                     # users/U
    user_type: str = "HUMAN"      # HUMAN | BOT


@dataclass
class MemberPage:
    members: list[Member]
    next_token: str = ""


@dataclass
class Attachment:
    content_name: str = ""
    content_type: str = ""


@dataclass
class Quote:
    """A quoted message, as carried on a message (chat-v1 §3.6)."""

    name: str                     # spaces/X/messages/Y
    last_update_time: float = 0.0
    text: str = ""                # the snapshot


@dataclass
class QuoteRequest:
    """A quote-reply as requested (chat-v1 §3.1): name + expected timestamp."""

    name: str
    last_update_time: float


@dataclass
class Message:
    name: str                     # spaces/X/messages/Y
    sender: str                   # users/U
    sender_type: str              # HUMAN | BOT
    thread: str                   # spaces/X/threads/T
    text: str
    create_time: float
    update_time: float = 0.0      # 0 = never edited
    attachments: list[Attachment] = field(default_factory=list)
    quoted: Quote | None = None


@dataclass
class MessagePage:
    messages: list[Message]
    next_token: str = ""


#: MessageCreate.reply — how a requested thread is honoured (chat-v1 §3.1).
REPLY_NEW = "new"            # start a new thread; any requested thread ignored
REPLY_FALLBACK = "fallback"  # reply if the thread exists, else a new thread
REPLY_OR_FAIL = "fail"       # reply, or raise ThreadNotFound


@dataclass
class MessageCreate:
    text: str
    thread: str = ""              # existing thread resource name
    thread_key: str = ""          # caller-chosen key
    reply: str = REPLY_NEW
    quote: QuoteRequest | None = None
    attachments: list[str] = field(default_factory=list)  # upload ref names


@dataclass
class MessagePatch:
    """Fields named by the update mask; ``None`` = not in the mask."""

    text: str | None = None


@dataclass
class Emoji:
    unicode: str


@dataclass
class Reaction:
    name: str                     # spaces/X/messages/Y/reactions/R
    user: str
    emoji: str


@dataclass
class ReactionPage:
    reactions: list[Reaction]
    next_token: str = ""


@dataclass
class Upload:
    filename: str
    content_type: str
    size: int
    content: bytes = b""


@dataclass
class AttachmentRef:
    resource_name: str


# --- the protocol ----------------------------------------------------------

class ChatDriver(Protocol):
    async def list_spaces(self, page: Page) -> SpacePage: ...
    async def get_space(self, space: str) -> Space | None: ...
    async def create_space(self, req: SpaceCreate) -> Space: ...
    async def find_direct_message(self, user: str) -> Space | None: ...
    async def list_members(self, space: str, page: Page) -> MemberPage: ...
    async def list_messages(self, space: str, query: MessageQuery) -> MessagePage: ...
    async def get_message(self, name: str) -> Message | None: ...
    async def create_message(self, space: str, req: MessageCreate) -> Message: ...
    async def patch_message(self, name: str, patch: MessagePatch) -> Message | None: ...
    async def delete_message(self, name: str) -> bool: ...
    async def search_messages(self, req: MessageSearch) -> MessagePage: ...
    async def create_reaction(self, message: str, emoji: Emoji) -> Reaction: ...
    async def list_reactions(self, message: str, page: Page) -> ReactionPage: ...
    async def delete_reaction(self, name: str) -> bool: ...
    async def upload_attachment(self, space: str, upload: Upload) -> AttachmentRef: ...
    async def ping(self) -> None: ...
