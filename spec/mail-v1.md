# Jetty module: `mail` — v1

Mount: `/mail` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4.

Accepts a fully-composed outbound message and delivers it through whatever the
local environment provides. The surface performs no templating, no scheduling,
no retry policy of its own, and no address-book lookups: the caller composes,
the driver delivers.

Clients resolve every path beneath a configured base URL — the sidecar's
address plus `/mail`. The mount prefix exists in URLs only; nothing in a
request or response body carries it.

---

## 1. Mount and error shape

The mail surface is a foreign protocol served under the module's mount prefix
on the control listener (SPEC.md §2.1): `/mail/v1/send` and `/mail/healthz`.
The module declares no listener of its own.

The surface conforms to the relay contract, not to SPEC.md §3. Every non-2xx
response carries the relay contract's envelope and **MUST NOT** use the
envelope in SPEC.md §3.1:

```json
{ "error": "<machine_slug>", "detail": "<safe human text>" }
```

`detail` is optional, may be shown to operators verbatim, and **MUST NOT**
contain credential material (SPEC.md §1.4). Clients switch on the status code
and `error` slug, never on `detail`.

SPEC.md §1 applies to the surface in full. In particular fail-closed (§1.2):
a driver failure is `503 upstream_unavailable`, a surface bug is
`500 internal_error` (both retryable — 429 and 5xx are the transient errors of
this contract), and neither is ever a fabricated `202`.

---

## 2. Authentication

Transport is the primary access control (SPEC.md §1.5): a unix socket's file
mode, or a loopback bind. Additionally, when `mail.token` is configured, every
`/mail/v1/…` request **MUST** carry `Authorization: Bearer <token>`; a missing
or wrong token is `401 {"error": "missing_or_bad_bearer_token"}`.
`/mail/healthz` never requires the token. A deployment using a TCP listener
**SHOULD** configure `mail.token`.

---

## 3. Endpoints

### 3.1 `POST /mail/v1/send` — deliver one message

```json
{
  "idempotencyKey": "01K3QX9WQ2R4V8YFB7N0ZC5M6T",
  "from": "relay-bot+notify@corp.example",
  "replyTo": ["avarma@corp.example"],
  "to": ["bwu@corp.example"],
  "cc": ["jlin@corp.example"],
  "subject": "Review due tomorrow",
  "text": "Hi bwu, …",
  "html": "<p>Hi bwu,</p>…",
  "threadKey": "reviews/01K3Q…/bwu",
  "tags": ["reviewer-reminder"],
  "dryRun": false
}
```

| field | type | required | meaning |
| --- | --- | --- | --- |
| `idempotencyKey` | string, ≤ 128 bytes | **yes** | Caller-generated, stable across retries of the same message. §3.2. |
| `from` | address | **yes** | Envelope and header sender. A driver that pins its sender **MUST** verify this matches and reject a mismatch (`403`) rather than silently rewrite. |
| `replyTo` | address[] ≤ 5 | no | Where a human reply should go. Absent ⇒ replies go to `from`. |
| `to` | address[] ≥ 1 | **yes** | Primary recipients. |
| `cc` | address[] | no | Visibility recipients. |
| `bcc` | address[] | no | Accepted for completeness. |
| `subject` | string, ≤ 512 chars | **yes** | Single line; embedded CR/LF is rejected (header injection). |
| `text` | string, ≤ 256 KiB | **yes** | UTF-8 plain-text body, always present. |
| `html` | string, ≤ 512 KiB | no | Richer alternative, sent as `multipart/alternative` alongside `text`. |
| `threadKey` | string, ≤ 256 bytes | no | Opaque conversation id; messages sharing one thread together in a mail client. |
| `tags` | string[] ≤ 10, each ≤ 64 bytes | no | Opaque labels for the mail system's own reporting. Never rendered to the recipient. |
| `dryRun` | boolean | no | Validate everything and return the same status codes **without delivering**. A dry run never consumes or records an idempotency key. |

Limits: `to` + `cc` + `bcc` ≤ **100** addresses total. An address is a plain
`local@domain` string, ≤ 320 bytes, no display names. Unknown request fields
are rejected `400` (SPEC.md §6).

Responses:

- `202` — accepted for delivery:

  ```json
  { "messageId": "<20260708.9f2c@corp.example>", "deduped": false }
  ```

  `messageId` is the durable id for this message. `deduped` is `true` iff this
  `idempotencyKey` was already accepted; the message was **not** sent again and
  `messageId` is the original's. A repeat **MUST** stay `202`, not an error.

- `400 {"error": "bad_request"}` — malformed, over-limit, unknown field, or
  CR/LF in `subject`.
- `401 {"error": "missing_or_bad_bearer_token"}` — §2.
- `403 {"error": "sender_not_permitted"}` — `from` is not an address this
  deployment may send as. Not retryable.
- `413 {"error": "message_too_large"}` — over the mail system's own ceiling.
- `422 {"error": "unroutable_recipients", "recipients": [...]}` — one or more
  addresses cannot be delivered to. **Nothing is sent**: all-or-nothing, and
  `recipients` lists exactly the offending addresses. Not retryable.
- `429 {"error": "rate_limited", "retryAfterSeconds": 60}` — back off.
- `503 {"error": "upstream_unavailable"}` — driver or its backend unreachable.

### 3.2 Idempotency

- Keys are retained **≥ 7 days**; within the window a repeated key answers
  `202 {deduped: true}` without a second delivery.
- A key repeated with a *different* body is still a dedup, not an error — the
  answer is for the message actually sent.
- A `dryRun` request neither consumes nor records a key.

### 3.3 Addresses

1. A plus-addressed sender `<local>+<tag>@<domain>` **MUST** be accepted
   whenever `<local>@<domain>` is a permitted sender — plus-addressing is
   never normalized away or rejected.
2. Recipients are never rewritten into another domain: a mistyped address
   fails visibly (`422`) rather than being delivered to a stranger.

### 3.4 `GET /mail/healthz`

`200 {"ok": true}` when the driver can reach its backend; `503` otherwise.
Unauthenticated (§2). This is the relay contract's health check and reports
**upstream** reachability — deliberately unlike the core `/healthz`
(SPEC.md §4.1), which is liveness only. That difference is why the mail
surface carries its own.

*(There are no other endpoints: no address lookup, no mailbox reading, no
bounce webhook. Any other path under `/mail` is `404 {"error": "not_found"}`
in the shape of §1.)*

---

## 4. Logging

- Log every send with `idempotencyKey`, `messageId`, recipient count, and
  outcome.
- **Never** log message bodies or subjects, at any level: they may name
  people. Recipient addresses may be logged; treat them as personal data.

---

## 5. Driver interface

The surface validates the wire contract and dispatches one internal
representation to a **driver**.

```python
class MailDriver(Protocol):
    async def send(self, msg: MailSend) -> SendResult: ...
    async def ping(self) -> None: ...
```

A driver owns delivery, sender policy, recipient routability, and the
idempotency store. It raises `SenderNotPermitted`, `UnroutableRecipients`,
`MessageTooLarge`, or `RateLimited` for the conditions §3.1 enumerates; any
other exception is an unreachable backend, which the surface maps to `503` —
never to a fabricated success (SPEC.md §1.2).

Drivers defined alongside this document:

| Driver | Behaviour |
|---|---|
| `spool` | Writes each accepted message as a JSON file to `mail.spool_dir`, for development, CI, and conformance runs — a consumer's tests read the directory to assert who was mailed. Deterministic message ids; the idempotency store is rebuilt from the spool at boot, so dedup survives a restart for as long as the spool is retained. Performs no network I/O. |

Delivery drivers (corp mail gateways, SMTP) implement the same Protocol
privately without modification to this module or its surface. Naming a driver
this build does not ship fails at boot.

### 5.1 Spool file shape

One JSON object per accepted message, exactly the §3.1 request fields that
were present (addresses fully composed), plus `messageId` and `acceptedAt`
(RFC-3339). `dryRun` requests are never spooled. This shape is contractual:
consumers' test harnesses parse it.

```json
{
  "idempotencyKey": "…", "from": "…", "to": ["…"], "cc": ["…"],
  "subject": "…", "text": "…", "threadKey": "…", "tags": ["…"],
  "messageId": "<…@jetty-mail>", "acceptedAt": "2026-08-17T12:00:00+00:00"
}
```

### 5.2 Forced failures

`mail.fail` forces the spool driver into one failure mode — `"503"`, `"429"`,
or `"422"` — so a consumer's dispatcher can prove its transient-vs-permanent
retry split against a real HTTP surface. Delivery drivers ignore it.

---

## 6. Configuration

| Key | Default | Meaning |
|---|---|---|
| `mail.enabled` | `false` | Mount the module. |
| `mail.driver` | `spool` | Driver to use. |
| `mail.token` | empty | §2 bearer token. Empty = transport is the only ACL. |
| `mail.spool_dir` | — | **Required for `spool`**: where accepted messages land. |
| `mail.sender` | empty | The one permitted `from` (§3.3 plus-addressing applies). Empty = any sender. Spool driver only. |
| `mail.domain` | empty | The one deliverable recipient domain; anything else is `422`. Empty = all routable. Spool driver only. |
| `mail.fail` | empty | §5.2 forced failure mode. Spool driver only. |
