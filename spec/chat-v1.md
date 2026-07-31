# Jetty module: `chat` — v1

Mount: `/chat` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4.

Serves a subset of the public Google Chat REST API (v1) so that a client or
library written against that API can reach an internal chat service by changing
only its base URL.

---

## 1. Mount

The chat surface is a foreign protocol served under the module's mount prefix
on the control listener (SPEC.md §2.1). The emulated API's URL layout appears
verbatim below the prefix: `/chat/v1/…`, and `/chat/upload/v1/…` for media
upload. This is exactly what a stock Google Chat client produces when its base
URL is set to the sidecar's address plus `/chat`, upload paths included.

The module declares no listener of its own; `modules[].listener` is absent in
`GET /v1/meta` (SPEC.md §4.2).

The surface conforms to the API it emulates, not to SPEC.md §3. Error responses
**MUST** use the emulated API's error shape and **MUST NOT** use the envelope in
SPEC.md §3.1:

```json
{ "error": { "code": 404, "message": "space not found", "status": "NOT_FOUND" } }
```

SPEC.md §1 applies to the surface in full.

---

## 2. No authentication

This module performs **no authentication**. Identity, authorization, and
auditing for chat operations happen elsewhere in the deployment; by the time a
request reaches this surface it is trusted. The transport is the access
control (SPEC.md §1.5): a unix socket's file mode, or a loopback bind.

Consequently:

- The surface **MUST NOT** require OAuth, an API key, or any credential.
- Stock Google Chat client libraries insist on attaching an `Authorization`
  header. The surface **MUST** accept and ignore it, and **MUST NOT** forward
  it to the driver or any upstream (SPEC.md §1.4 applies to it regardless).
- The module defines no per-caller identity. Where the emulated API attributes
  an action to the calling user, the attribution is the driver's concern
  (e.g. a configured service identity).

---

## 3. Emulated surface

A subset of the Google Chat REST API v1. An enabled module **MUST** implement
every method below. A request matching the emulated API's URL layout but not
this subset **MUST** receive the emulated API's error with status
`UNIMPLEMENTED` (code `501`); an unrecognisable path is `NOT_FOUND`.

| Method | HTTP |
|---|---|
| `spaces.list` | `GET /chat/v1/spaces` |
| `spaces.get` | `GET /chat/v1/spaces/{space}` |
| `spaces.create` | `POST /chat/v1/spaces` |
| `spaces.findDirectMessage` | `GET /chat/v1/spaces:findDirectMessage` |
| `spaces.members.list` | `GET /chat/v1/spaces/{space}/members` |
| `spaces.messages.list` | `GET /chat/v1/spaces/{space}/messages` |
| `spaces.messages.get` | `GET /chat/v1/spaces/{space}/messages/{message}` |
| `spaces.messages.create` | `POST /chat/v1/spaces/{space}/messages` |
| `spaces.messages.patch` | `PATCH /chat/v1/spaces/{space}/messages/{message}` |
| `spaces.messages.delete` | `DELETE /chat/v1/spaces/{space}/messages/{message}` |
| `spaces.messages.search` | `POST /chat/v1/spaces/-/messages:search` |
| `spaces.messages.reactions.create` | `POST /chat/v1/spaces/{space}/messages/{message}/reactions` |
| `spaces.messages.reactions.list` | `GET /chat/v1/spaces/{space}/messages/{message}/reactions` |
| `spaces.messages.reactions.delete` | `DELETE /chat/v1/spaces/{space}/messages/{message}/reactions/{reaction}` |
| `media.upload` | `POST /chat/upload/v1/spaces/{space}/attachments:upload` |

Resource names inside request and response bodies are unprefixed, exactly as
the emulated API defines them: the mount prefix exists in URLs only.

Resource names follow the emulated API: `spaces/{space}`,
`spaces/{space}/messages/{message}`,
`spaces/{space}/messages/{message}/reactions/{reaction}`. Responses carry full
resource names, never bare IDs.

List and search methods support the emulated API's `pageSize` and `pageToken`
parameters and return `nextPageToken` when more results exist.

### 3.1 Threading and replies

`spaces.messages.create` supports the emulated API's threading contract:

- **Reply in thread** — the request body sets `thread.name` (an existing
  thread's resource name) or `thread.threadKey` (a caller-chosen key), together
  with the `messageReplyOption` query parameter
  (`REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` or `REPLY_MESSAGE_OR_FAIL`). Without
  it, the message starts a new thread.
- **Quote-reply** — the request body sets `quotedMessageMetadata.name` to the
  quoted message's resource name **and** `quotedMessageMetadata.lastUpdateTime`
  to that message's last-update (or create) timestamp. Per the emulated API,
  the request **MUST** fail if the timestamp does not match the latest version
  of the quoted message, and a message may only quote a message in the same
  thread or a root message.

### 3.2 Search

`spaces.messages.search` follows the emulated API: the path's parent segment
**MUST** be the literal `spaces/-` (search spans every space visible to the
service; any other parent is the emulated `INVALID_ARGUMENT` error), and the
request body carries a required `filter` string, with optional `pageSize`,
`pageToken`, and `orderBy` (`createTime` ascending/descending).

### 3.3 Editing

`spaces.messages.patch` applies the fields named in the `updateMask` query
parameter. An implementation **MUST** support `updateMask=text`; other paths
are optional and follow §3.4 when unsupported.

### 3.4 Translation fidelity

If a request specifies a field or parameter the driver cannot honour, the
implementation **MUST** reject the request with the emulated API's
`INVALID_ARGUMENT` error, naming the field. It **MUST NOT** drop, substitute,
or approximate it, and **MUST NOT** offer a mode that does so.

### 3.5 Uploads

`media.upload` accepts the emulated API's upload protocol on the
`/chat/upload/v1` path and returns an `attachmentDataRef` usable in a subsequent
`spaces.messages.create`. The size cap is `chat.upload_max_bytes`; an
oversized upload is the emulated API's `INVALID_ARGUMENT` error, not a
truncated write.

---

## 4. Driver interface

The surface translates requests into one internal representation and
dispatches to a **driver**.

```python
class ChatDriver(Protocol):
    async def list_spaces(self, page: Page) -> SpacePage: ...
    async def get_space(self, space: str) -> Space | None: ...
    async def create_space(self, req: SpaceCreate) -> Space: ...
    async def find_direct_message(self, user: str) -> Space | None: ...
    async def list_members(self, space: str, page: Page) -> MemberPage: ...
    async def list_messages(self, space: str, page: Page) -> MessagePage: ...
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
```

A driver returns `None`/`False` for a resource that does not exist and raises
for an unreachable upstream. The surface maps the former to the emulated
`NOT_FOUND` and the latter to the emulated `UNAVAILABLE`; an implementation
**MUST NOT** conflate them (SPEC.md §1.2).

Drivers defined alongside this document:

| Driver | Behaviour |
|---|---|
| `mock` | In-memory spaces, messages, and reactions seeded from configuration, for development, CI, and conformance runs. Performs no network I/O. |
| `passthrough` | Forwards to the public Google Chat API using a configured service credential. |

Additional drivers implement the same Protocol without modification to this
module or its surface.

---

## 5. Configuration

| Key | Default | Meaning |
|---|---|---|
| `chat.enabled` | `false` | Mount the module. |
| `chat.driver` | `mock` | Upstream driver to use. |
| `chat.upload_max_bytes` | `26214400` | §3.5 cap (25 MiB). |
