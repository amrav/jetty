"""The ``spool`` driver (mail-v1 §5): delivery is a JSON file in a directory.

The reference driver for development, CI, and conformance runs — a consumer's
integration tests read the spool to assert who was mailed, with what cc, and
in what order. The spool file shape (mail-v1 §5.1) is contractual: consumers'
test harnesses parse it.

Two behaviours worth noting:

- **Message ids are deterministic** (a hash of the idempotency key) and the
  idempotency store is rebuilt from the spool at boot, so dedup survives a
  restart for as long as the spool is retained — the contract's ≥ 7 day window
  becomes "as long as you keep the files".
- **Sender policy honours plus-addressing** (mail-v1 §3.3): the configured
  sender and the request's ``from`` are compared with any ``+tag`` stripped,
  so pinning ``bot@corp`` permits ``bot+reviews@corp`` and vice versa.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from jetty.modules.mail.driver import (
    MailSend,
    RateLimited,
    SendResult,
    SenderNotPermitted,
    UnroutableRecipients,
)


def _strip_plus(address: str) -> str:
    """``local+tag@domain`` → ``local@domain``; anything else unchanged."""
    local, _, domain = address.partition("@")
    return f"{local.partition('+')[0]}@{domain}"


class SpoolMailDriver:
    def __init__(
        self,
        spool_dir: str,
        sender: str = "",
        domain: str = "",
        fail: str = "",
    ) -> None:
        self.spool_dir = spool_dir
        self.sender = sender
        self.domain = domain
        self.fail = fail
        os.makedirs(spool_dir, exist_ok=True)
        #: idempotencyKey -> messageId. The one thing this driver must
        #: remember; seeded from the spool so a restart keeps deduplicating.
        self._seen: dict[str, str] = {}
        for name in os.listdir(spool_dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(spool_dir, name), encoding="utf-8") as f:
                    row = json.load(f)
                self._seen[row["idempotencyKey"]] = row["messageId"]
            except (OSError, ValueError, KeyError):
                continue  # a foreign or truncated file is not this driver's spool

    async def ping(self) -> None:
        if self.fail == "503":
            raise RuntimeError("forced failure: mail.fail = 503")
        if not os.access(self.spool_dir, os.W_OK):
            raise RuntimeError(f"spool dir {self.spool_dir!r} is not writable")

    async def send(self, msg: MailSend) -> SendResult:
        # Forced failure modes (mail-v1 §5.2), so a consumer's dispatcher can
        # prove its transient-vs-permanent retry split against a real surface.
        if self.fail == "503":
            raise RuntimeError("forced failure: mail.fail = 503")
        if self.fail == "429":
            raise RateLimited(60)

        if self.sender and _strip_plus(msg.from_addr) != _strip_plus(self.sender):
            raise SenderNotPermitted(f"only {self.sender} may send")

        recipients = [*msg.to, *msg.cc, *msg.bcc]
        unroutable = (
            [a for a in recipients if a.rpartition("@")[2] != self.domain]
            if self.domain
            else []
        )
        if self.fail == "422" or unroutable:
            # All-or-nothing: "it went out" must never mean "most of it went
            # out". The forced mode names the first recipient, as the stub did.
            raise UnroutableRecipients(unroutable or recipients[:1])

        # A dry run proves the configuration and leaves no trace — no spool
        # file, and deliberately no idempotency key consumed (mail-v1 §3.2).
        if msg.dry_run:
            return SendResult(message_id="<dry-run@jetty-mail>", deduped=False)

        existing = self._seen.get(msg.idempotency_key)
        if existing is not None:
            return SendResult(message_id=existing, deduped=True)

        digest = hashlib.sha256(msg.idempotency_key.encode("utf-8")).hexdigest()[:16]
        message_id = f"<{digest}@jetty-mail>"
        row: dict[str, object] = {
            "idempotencyKey": msg.idempotency_key,
            "from": msg.from_addr,
            "to": msg.to,
            **({"cc": msg.cc} if msg.cc else {}),
            **({"bcc": msg.bcc} if msg.bcc else {}),
            **({"replyTo": msg.reply_to} if msg.reply_to else {}),
            "subject": msg.subject,
            "text": msg.text,
            **({"html": msg.html} if msg.html else {}),
            **({"threadKey": msg.thread_key} if msg.thread_key else {}),
            **({"tags": msg.tags} if msg.tags else {}),
            "messageId": message_id,
            "acceptedAt": datetime.now(timezone.utc).isoformat(),
        }
        path = os.path.join(self.spool_dir, f"{digest}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(row, f, indent=2)
        self._seen[msg.idempotency_key] = message_id
        return SendResult(message_id=message_id, deduped=False)
