# Jetty module: `filesystem` — v1

**Status: experimental.** This specification is subject to breaking changes
without warning: endpoints, wire shapes, error codes, and the semantics in §2
may all change while the `v1` path segment stays where it is. SPEC.md §6's
freeze applies to a *published* `api_version`, and this one is not published
yet. Do not build against it anything you are unwilling to update.

Mount: `/filesystem/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4, which this document does not restate.

The `filesystem` module reads and writes **whole files** under one configured
root directory. It exists so an OSS binary can keep files on whatever storage
the host actually provides — a directory on local disk against the reference
build, whatever store a private driver fronts internally — without carrying
mount configuration, storage credentials, or a second client library.

The semantics are deliberately those of the standard unix filesystem (§2):
this module adds no permission model, no metadata store, and no locking of
its own. What the kernel would tell a local process is what the client is
told.

File content is routinely user data. An implementation **MUST NOT** log file
content at any level (SPEC.md §1.4 applied to data); paths **MAY** be logged.

---

## 1. Scope

In scope: reading one file's entire content, and writing — creating or
replacing — one file's entire content.

Deliberately out of scope for v1: directory listing; stat as a queryable
resource; delete, rename, copy, and mkdir; byte ranges, streaming, and
partial updates; permission changes (chmod/chown); locks and leases;
watch/notify; extended attributes.

---

## 2. Unix semantics

- **Identity.** Every operation executes with the sidecar's own process
  identity. The kernel's permission evaluation over the standard mode bits
  is the access control; a refusal is `403 permission_denied` (§6) and
  **MUST NOT** be masked. The module holds no per-user credentials and
  performs no impersonation (SPEC.md §1.1) — a deployment wanting different
  authority runs the sidecar as a different user.
- **Creation.** A write to a path with no file creates it as `open(2)` with
  `O_CREAT` would: mode `0666` as modified by the process umask. The module
  never chmods.
- **Replacement.** A write to an existing file truncates in place
  (`O_TRUNC`), preserving the inode: mode, ownership, and hard links
  survive. Replacement therefore requires write permission on the **file**,
  not on its directory.
- **Parents are not created.** A write into a directory that does not exist
  is `404 not_found`, as `open(2)` would say `ENOENT`.
- **Symlinks are followed**, for reads and writes both — subject to the
  containment rule in §3.
- **Regular files only.** A path naming a directory, FIFO, socket, or device
  node is `400 invalid_request` (a FIFO would otherwise block the worker
  indefinitely — the refusal is deliberate, not an omission).
- **No atomicity, no locking.** A write is truncate-then-write; a reader
  concurrent with a writer can observe a torn state, and the last writer
  wins. v1 defines no coordination; callers that need it bring their own.

---

## 3. Path addressing

`{path}` in §5 is the file's path **relative to the configured root**, e.g.
`notes.txt` or `team/notes.txt`. The root is the module's entire filesystem
authority; nothing outside it is reachable regardless of what a request asks
for.

An implementation **MUST** reject, with `400 invalid_request`, a path that is
empty, absolute, longer than 4096 bytes, or that contains a backslash, a NUL,
or an empty, `.`, or `..` segment — and **MUST** verify that the *resolved*
path (symlinks followed) still lies under the resolved root, so a symlink
inside the root cannot become a door out of it.

---

## 4. Configuration

```toml
[modules.filesystem]
enabled = true
driver = "local"
root = "/srv/files"   # required: the servable tree
```

| Key | Type | Required | Notes |
|---|---|---|---|
| `root` | string | yes | Directory whose contents are servable. A missing directory **MUST** abort boot (SPEC.md §1.2), not serve errors. A read-only root is legitimate — writes then fail per §2, which is the truth. |
| `driver` | string | no, default `"local"` | This repository ships `local`. An unavailable driver name **MUST** abort boot, never serve a stand-in. |

---

## 5. Endpoints

Both endpoints carry the file's content **raw** — never JSON-wrapped, never
base64. Content larger than **32 MiB** (33554432 bytes) is refused in both
directions with `413 too_large`: v1 is a whole-file API, and a file that size
wants ranges and streaming, which are out of scope (§1).

### 5.1 `GET /filesystem/v1/files/{path}` — read one file

The file's entire content as raw bytes, `Content-Type` guessed from the file
name, `application/octet-stream` when unguessable. An empty file is a `200`
with an empty body.

### 5.2 `PUT /filesystem/v1/files/{path}` — write one file

The request body is the file's entire new content, raw. Any `Content-Type`,
or none, is accepted (this module's override of SPEC.md §2.2);
`application/octet-stream` is conventional. An empty body writes an empty
file. Creation and replacement per §2.

`200`:

```json
{ "size": 5, "created": true }
```

`created` is `true` iff no file existed at the path before this write.

---

## 6. Errors

Additional codes beyond SPEC.md §3.1, both `retryable: false`:

| `code` | Status | Meaning |
|---|---|---|
| `permission_denied` | 403 | The kernel refused the operation for the sidecar's process identity (`EACCES`, `EPERM`, `EROFS`). |
| `too_large` | 413 | File or request content over §5's ceiling. |

Standard mapping:

| Condition | Response |
|---|---|
| Path fails §3's rules or resolves outside the root; path names something other than a regular file; symlink loop | `400 invalid_request` |
| No file at the path, or a missing directory on the way to it | `404 not_found` |
| Any other filesystem failure (`EIO`, `ENOSPC`, …) | `503 upstream_unavailable` |

---

## 7. Driver interface

The surface validates the wire contract — §3's path syntax, §5's ceiling —
and dispatches to a **driver**, which owns containment, the filesystem
operations, and §2's semantics against its own store.

```python
class FsDriver(Protocol):
    def read(self, path: str) -> bytes: ...
    def write(self, path: str, content: bytes) -> WriteResult: ...
```

Methods are synchronous — a driver does blocking I/O, and the surface keeps
it off the event loop. A driver raises `FileMissing`, `PermissionDenied`,
`InvalidTarget`, or `TooLarge` for the conditions §6 enumerates; anything
else it raises is a failing store, which the surface maps to
`503 upstream_unavailable` — never to a fabricated success (SPEC.md §1.2).

Drivers defined alongside this document:

| Driver | Behaviour |
|---|---|
| `local` | The configured root on the local filesystem, exactly as §2 describes. Performs no network I/O. |

Drivers for other storage implement the same Protocol privately without
modification to this module or its surface. Naming a driver this build does
not ship fails at boot.
