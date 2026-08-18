"""The internal representation and the ``MailDriver`` protocol (mail-v1 §5).

The surface (module.py) validates the wire contract — field shapes, limits,
header-injection, the bearer token — and hands a driver one validated message.
A driver owns everything the surface cannot know: whether this deployment may
send as ``from_addr``, whether the recipients are routable, the idempotency
store, and delivery itself.

Error contract (mail-v1 §5): a driver raises one of the typed exceptions below
for the conditions the wire contract enumerates; anything else it raises is an
unreachable backend, which the surface maps to ``503 upstream_unavailable`` —
never to a fabricated success (SPEC.md §1.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class SenderNotPermitted(Exception):
    """``from`` is not an address this deployment may send as → ``403``."""


class UnroutableRecipients(Exception):
    """One or more recipients cannot be delivered to → ``422``, nothing sent."""

    def __init__(self, recipients: list[str]) -> None:
        super().__init__(", ".join(recipients))
        self.recipients = recipients


class MessageTooLarge(Exception):
    """Over the mail system's own size ceiling → ``413``."""


class RateLimited(Exception):
    """Backend throttling → ``429`` with ``retryAfterSeconds``."""

    def __init__(self, retry_after_s: int = 60) -> None:
        super().__init__(f"retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


@dataclass
class MailSend:
    """One validated message. Addresses are full ``local@domain`` strings."""

    idempotency_key: str
    from_addr: str
    to: list[str]
    subject: str
    text: str
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    html: str = ""            # "" = no html alternative
    thread_key: str = ""
    tags: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass
class SendResult:
    message_id: str
    deduped: bool


class MailDriver(Protocol):
    async def send(self, msg: MailSend) -> SendResult: ...
    async def ping(self) -> None: ...
