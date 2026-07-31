"""The ``mock`` driver: in-memory chat, seeded from configuration.

For development, CI, and conformance runs (chat-v1 §4). No network I/O; the
whole state lives in this object and dies with the process — which is fine,
because a sidecar holds no durable state anyway (SPEC.md §1.1 is about user
credentials, but the same posture keeps the mock honest: a client must work
against a chat service it cannot assume it has written to before).

Determinism: resource ids are counters, not randomness, so a test can predict
the name a create will return. Time is real (`time.time()`), because clients
of this driver filter on `createTime` and a frozen clock would make every
seeded message simultaneous.
"""

from __future__ import annotations

import itertools
import time

from jetty.modules.chat.driver import (
    Attachment,
    AttachmentRef,
    DriverRejects,
    Emoji,
    Member,
    MemberPage,
    Message,
    MessageCreate,
    MessagePage,
    MessagePatch,
    MessageQuery,
    MessageSearch,
    Page,
    Quote,
    Reaction,
    ReactionPage,
    REPLY_NEW,
    REPLY_OR_FAIL,
    Space,
    SpaceCreate,
    SpacePage,
    ThreadNotFound,
    Upload,
)


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


class MockChatDriver:
    def __init__(
        self,
        identity: str = "users/jetty",
        seed_spaces: list[dict] | None = None,
        seed_messages: list[dict] | None = None,
    ) -> None:
        self._identity = identity
        self._counter = itertools.count(1)
        self._spaces: dict[str, Space] = {}                 # name -> Space
        self._members: dict[str, list[Member]] = {}         # space -> members
        self._messages: dict[str, Message] = {}             # name -> Message
        self._order: dict[str, list[str]] = {}              # space -> names
        self._threads: set[str] = set()
        self._thread_keys: dict[tuple[str, str], str] = {}  # (space, key) -> thread
        self._thread_roots: dict[str, str] = {}             # thread -> first msg
        self._reactions: dict[str, list[Reaction]] = {}     # message -> reactions
        self._uploads: dict[str, Upload] = {}               # ref -> upload
        for row in seed_spaces or []:
            name = f"spaces/{row['id']}"
            self._spaces[name] = Space(
                name=name,
                display_name=row.get("display_name", ""),
                space_type=row.get("space_type", "SPACE"),
            )
            self._members[name] = [
                Member(name=f"{name}/members/{i + 1}", user=user)
                for i, user in enumerate(row.get("members", []))
            ]
        for row in seed_messages or []:
            space = f"spaces/{row['space']}"
            if space not in self._spaces:
                raise ValueError(f"chat.seed message names unknown space {space!r}")
            self._insert(
                space,
                sender=row.get("sender", "users/seed"),
                text=row["text"],
                thread="",
            )

    # --- internals --------------------------------------------------------
    def _new_thread(self, space: str) -> str:
        thread = f"{space}/threads/t{next(self._counter)}"
        self._threads.add(thread)
        return thread

    def _insert(
        self,
        space: str,
        *,
        sender: str,
        text: str,
        thread: str,
        sender_type: str = "HUMAN",
        attachments: list[Attachment] | None = None,
        quoted: Quote | None = None,
    ) -> Message:
        if not thread:
            thread = self._new_thread(space)
        name = f"{space}/messages/m{next(self._counter)}"
        message = Message(
            name=name,
            sender=sender,
            sender_type=sender_type,
            thread=thread,
            text=text,
            # Truncated to the microsecond the wire carries, so a client that
            # feeds a message's own createTime back as a filter bound gets
            # exact comparisons, not sub-microsecond float jitter.
            create_time=round(time.time(), 6),
            attachments=attachments or [],
            quoted=quoted,
        )
        self._messages[name] = message
        self._order.setdefault(space, []).append(name)
        self._thread_roots.setdefault(thread, name)
        return message

    def _space_of(self, resource: str) -> str:
        return "/".join(resource.split("/")[:2])

    # --- spaces -----------------------------------------------------------
    async def list_spaces(self, page: Page) -> SpacePage:
        rows = [self._spaces[k] for k in sorted(self._spaces)]
        window, token = _paginate(rows, page)
        return SpacePage(spaces=window, next_token=token)

    async def get_space(self, space: str) -> Space | None:
        return self._spaces.get(space)

    async def create_space(self, req: SpaceCreate) -> Space:
        name = f"spaces/mock{next(self._counter)}"
        created = Space(
            name=name, display_name=req.display_name, space_type=req.space_type
        )
        self._spaces[name] = created
        self._members[name] = [Member(name=f"{name}/members/1", user=self._identity)]
        return created

    async def find_direct_message(self, user: str) -> Space | None:
        for name in sorted(self._spaces):
            space = self._spaces[name]
            if space.space_type != "DIRECT_MESSAGE":
                continue
            if any(m.user == user for m in self._members.get(name, [])):
                return space
        return None

    async def list_members(self, space: str, page: Page) -> MemberPage:
        window, token = _paginate(list(self._members.get(space, [])), page)
        return MemberPage(members=window, next_token=token)

    # --- messages ---------------------------------------------------------
    async def list_messages(self, space: str, query: MessageQuery) -> MessagePage:
        rows = [self._messages[n] for n in self._order.get(space, [])]
        if query.thread:
            rows = [m for m in rows if m.thread == query.thread]
        if query.after:
            rows = [m for m in rows if m.create_time > query.after]
        if query.before:
            rows = [m for m in rows if m.create_time < query.before]
        rows.sort(key=lambda m: m.create_time, reverse=query.descending)
        window, token = _paginate(rows, query.page)
        return MessagePage(messages=window, next_token=token)

    async def get_message(self, name: str) -> Message | None:
        return self._messages.get(name)

    async def create_message(self, space: str, req: MessageCreate) -> Message:
        thread = self._resolve_thread(space, req)
        quoted = self._resolve_quote(req, thread) if req.quote else None
        attachments = [self._resolve_attachment(ref) for ref in req.attachments]
        return self._insert(
            space,
            sender=self._identity,
            text=req.text,
            thread=thread,
            attachments=attachments,
            quoted=quoted,
        )

    def _resolve_thread(self, space: str, req: MessageCreate) -> str:
        if req.reply == REPLY_NEW or not (req.thread or req.thread_key):
            return ""  # _insert starts a new thread
        if req.thread:
            if req.thread in self._threads:
                return req.thread
            if req.reply == REPLY_OR_FAIL:
                raise ThreadNotFound(req.thread)
            return ""
        key = (space, req.thread_key)
        if key in self._thread_keys:
            return self._thread_keys[key]
        if req.reply == REPLY_OR_FAIL:
            raise ThreadNotFound(f"threadKey {req.thread_key!r}")
        thread = self._new_thread(space)
        self._thread_keys[key] = thread
        return thread

    def _resolve_quote(self, req: MessageCreate, thread: str) -> Quote:
        assert req.quote is not None
        quoted = self._messages.get(req.quote.name)
        if quoted is None:
            raise DriverRejects(
                f"quotedMessageMetadata.name: no such message {req.quote.name!r}"
            )
        current = quoted.update_time or quoted.create_time
        # Sub-second float jitter must not fail an honest client: the wire
        # carries RFC-3339 with microseconds, so equality is to the microsecond.
        if abs(current - req.quote.last_update_time) > 1e-6:
            raise DriverRejects(
                "quotedMessageMetadata.lastUpdateTime does not match the "
                "latest version of the quoted message"
            )
        is_root = self._thread_roots.get(quoted.thread) == quoted.name
        if quoted.thread != thread and not is_root:
            raise DriverRejects(
                "quotedMessageMetadata.name: a message may only quote a "
                "message in the same thread or a root message"
            )
        return Quote(name=quoted.name, last_update_time=current, text=quoted.text)

    def _resolve_attachment(self, ref: str) -> Attachment:
        upload = self._uploads.get(ref)
        if upload is None:
            raise DriverRejects(f"attachmentDataRef: no such upload {ref!r}")
        return Attachment(
            content_name=upload.filename, content_type=upload.content_type
        )

    async def patch_message(self, name: str, patch: MessagePatch) -> Message | None:
        message = self._messages.get(name)
        if message is None:
            return None
        if patch.text is not None:
            message.text = patch.text
            message.update_time = round(time.time(), 6)
        return message

    async def delete_message(self, name: str) -> bool:
        message = self._messages.pop(name, None)
        if message is None:
            return False
        self._order[self._space_of(name)].remove(name)
        self._reactions.pop(name, None)
        return True

    async def search_messages(self, req: MessageSearch) -> MessagePage:
        """The mock's filter grammar is a case-insensitive text substring.

        Documented here rather than hidden: a driver owns its search filter
        semantics, and pretending to parse a query language the mock does not
        have would violate chat-v1 §3.5 in spirit.
        """
        needle = req.filter.lower()
        rows = [m for m in self._messages.values() if needle in m.text.lower()]
        rows.sort(key=lambda m: m.create_time, reverse=req.descending)
        window, token = _paginate(rows, req.page)
        return MessagePage(messages=window, next_token=token)

    # --- reactions --------------------------------------------------------
    async def create_reaction(self, message: str, emoji: Emoji) -> Reaction:
        reaction = Reaction(
            name=f"{message}/reactions/r{next(self._counter)}",
            user=self._identity,
            emoji=emoji.unicode,
        )
        self._reactions.setdefault(message, []).append(reaction)
        return reaction

    async def list_reactions(self, message: str, page: Page) -> ReactionPage:
        window, token = _paginate(list(self._reactions.get(message, [])), page)
        return ReactionPage(reactions=window, next_token=token)

    async def delete_reaction(self, name: str) -> bool:
        message = name.rsplit("/reactions/", 1)[0]
        rows = self._reactions.get(message, [])
        for i, reaction in enumerate(rows):
            if reaction.name == name:
                del rows[i]
                return True
        return False

    # --- media ------------------------------------------------------------
    async def upload_attachment(self, space: str, upload: Upload) -> AttachmentRef:
        if space not in self._spaces:
            raise DriverRejects(f"no such space {space!r}")
        ref = f"{space}/attachments/up{next(self._counter)}"
        self._uploads[ref] = upload
        return AttachmentRef(resource_name=ref)

    async def ping(self) -> None:
        return None
