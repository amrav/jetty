# Jetty module: `filesystem` — v1

**Status: experimental.** This specification is subject to breaking changes
without warning: endpoints, wire shapes, error codes, and the semantics in §2
may all change while the `v1` path segment stays where it is. SPEC.md §6's
freeze applies to a *published* `api_version`, and this one is not published
yet. Do not build against it anything you are unwilling to update.

Mount: `/filesystem/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4, which this document does not restate.

The `filesystem` module reads, writes, renames, copies, and deletes **whole
files** under one configured root directory. It exists so an OSS binary can keep files on whatever storage
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

In scope: reading one file's entire content; writing — creating or replacing
— one file's entire content; deleting one file or one empty directory;
renaming one file; copying one file; creating a scratch directory (§5.6);
reading one path's metadata (§5.7). Writes, renames, and copies land
atomically (§2).

Deliberately out of scope for v1: directory listing; general-purpose mkdir
(scratch directories, §5.6, are the only directory creation); byte ranges,
streaming, and partial updates; permission changes (chmod/chown); locks and
leases; watch/notify; extended attributes.

---

## 2. Unix semantics

- **Identity.** Every operation executes with the sidecar's own process
  identity. The kernel's permission evaluation over the standard mode bits
  is the access control; a refusal is `403 permission_denied` (§6) and
  **MUST NOT** be masked. The module holds no per-user credentials and
  performs no impersonation (SPEC.md §1.1) — a deployment wanting different
  authority runs the sidecar as a different user.
- **Creation.** A write to a path with no file creates it as `open(2)` with
  `O_CREAT` would: mode `0666` as modified by the process umask. A copy
  creates its destination with the **source's** permission bits, as modified
  by the umask — what `cp(1)` does.
- **Atomic replacement.** A write or copy lands as a same-directory
  temporary file, fsynced, then moved into place with `rename(2)`; a rename
  is `rename(2)` itself. A reader concurrent with any of them sees the old
  content or the new in full, never a mixture, and a crash mid-operation
  leaves the old file in place. The replacement is a **new inode**: the
  replaced file's permission bits are preserved onto it, ownership becomes
  the sidecar's own, and a hard link to the old content keeps the old
  content. A rename that cannot be atomic — the destination lies across a
  filesystem boundary inside the root (`EXDEV`) — is refused
  `400 invalid_request`, never degraded to copy-plus-delete.
- **Directory permission governs mutation.** Because every mutation is
  link-level (`rename(2)`, `unlink(2)`), creating, replacing, renaming, and
  deleting all require write permission on the **directory**. A read-only
  file in a writable directory can be replaced, renamed over, or deleted —
  exactly as `mv(1)` and `rm(1)`; a writable file in an unwritable directory
  cannot be.
- **Parents are not created.** An operation whose destination lies in a
  directory that does not exist is `404 not_found`, as the syscall would say
  `ENOENT`.
- **Symlinks are followed**, for every operation and on both sides of the
  two-path ones — subject to the containment rule in §3. The namespace is
  transparent: a delete or rename addressed through a symlink acts on the
  resolved target, and the link itself stays.
- **Regular files only** for read, write, rename, and copy: any path naming
  a directory, FIFO, socket, or device node is `400 invalid_request` (a FIFO
  would otherwise block the worker indefinitely — the refusal is deliberate,
  not an omission). Delete additionally accepts an **empty directory**
  (`rmdir(2)`), so a scratch directory (§5.6) can be cleaned up.
- **No locking.** Individual operations are atomic (above), but v1 defines
  no locks, leases, or transactions spanning operations; between requests,
  the last writer wins.

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

File content crosses **raw** — never JSON-wrapped, never base64 (§5.1,
§5.2); the two-path operations take ordinary JSON bodies (SPEC.md §2.2
applies to them). v1 imposes no size ceiling; a whole file crosses in one
request and is held in memory at both ends, which is the cost of having
neither ranges nor streaming (§1).

### 5.1 `GET /filesystem/v1/files/{path}` — read one file

The file's entire content as raw bytes, `Content-Type` guessed from the file
name, `application/octet-stream` when unguessable. An empty file is a `200`
with an empty body.

The endpoint also answers `HEAD`, identically but bodiless, with
`Content-Length` reporting the size — existence and size without moving the
file.

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

### 5.3 `DELETE /filesystem/v1/files/{path}` — delete one file

`unlink(2)` for a file; `rmdir(2)` for an **empty** directory — the cleanup
half of §5.6's scratch directories. A non-empty directory is
`400 invalid_request`. Deleting a path that does not exist is
`404 not_found` — the truth of `ENOENT`, not an idempotent `200`.

`200`:

```json
{ "deleted": true }
```

### 5.4 `POST /filesystem/v1/rename` — rename one file, atomically

```json
{ "from": "drafts/report.txt", "to": "final/report.txt" }
```

Both fields are §3 paths. An existing destination is replaced atomically
(§2); with nothing at the destination the rename is a plain move. Unknown
fields are rejected `400` (SPEC.md §6).

`200`:

```json
{ "created": true }
```

`created` is `true` iff nothing existed at `to` before the rename. Renaming
a file onto itself is the syscall's no-op success, answered with
`created: false`.

### 5.5 `POST /filesystem/v1/copy` — copy one file

```json
{ "from": "template.ini", "to": "instance.ini" }
```

Reads `from` whole, then writes `to` by §2's atomic path — so the
destination, too, is never observable half-written. The source is read once;
a writer racing the copy affects which full content is copied, never its
integrity. Copying a file onto itself (after symlink resolution) is
`400 invalid_request`.

`200`:

```json
{ "size": 2048, "created": false }
```

### 5.6 `POST /filesystem/v1/tmpdir` — a fresh scratch directory

`mkdtemp(3)` semantics: creates a fresh, uniquely-named scratch directory
and returns its path, ready for §5.2 writes. Each call returns a new
directory, private to its caller by construction; this is the deliberate
alternative to handing every client one shared scratch path and inheriting
its collisions. The request takes no body.

Where the scratch area lives is the implementation's choice. The returned
path is an ordinary §3 path to build on, and clients **MUST** treat it as
opaque — never predict, hard-code, or reconstruct it. The reference `local`
driver uses `tmp/` under the root, mode `0700` as modified by the umask;
another implementation may place scratch space anywhere in its namespace.

`200`:

```json
{ "path": "tmp/k2x9a3f8" }
```

Nothing expires a scratch directory: cleanup is the caller's, by deleting
its files (§5.3) and then the directory itself (empty-directory delete,
§5.3). A deployment may of course place `root` on storage with its own
expiry.

### 5.7 `GET /filesystem/v1/stat/{path}` — one path's metadata

`stat(2)`, symlinks followed (§2). Works on any existing object — metadata
carries no blocking risk, so the regular-files-only rule (§2) does not
apply here; `type` says what was found.

`200`:

```json
{ "type": "file", "size": 2048, "mode": "0644", "mtime": "2026-08-25T12:00:00+00:00" }
```

| field | meaning |
|---|---|
| `type` | `"file"`, `"directory"`, or `"other"` (FIFO, socket, device). |
| `size` | `st_size` in bytes. Meaningful for files; for anything else it is whatever the store reports. |
| `mode` | The permission bits, octal, as `chmod` would write them. |
| `mtime` | Last content modification, RFC 3339. |

A missing path is `404 not_found`. Existence checking is a `stat` and a
switch on the status code; there is no separate exists endpoint.

---

## 6. Errors

One additional code beyond SPEC.md §3.1, `retryable: false`:

| `code` | Status | Meaning |
|---|---|---|
| `permission_denied` | 403 | The kernel refused the operation for the sidecar's process identity (`EACCES`, `EPERM`, `EROFS`). |

Standard mapping:

| Condition | Response |
|---|---|
| Path fails §3's rules or resolves outside the root; path names something other than a regular file; symlink loop; a rename that would cross a filesystem boundary; a copy of a file onto itself; a delete of a non-empty directory | `400 invalid_request` |
| No file at the path, or a missing directory on the way to it | `404 not_found` |
| Any other filesystem failure (`EIO`, `ENOSPC`, …) | `503 upstream_unavailable` |

---

## 7. Driver interface

The surface validates the wire contract — §3's path syntax — and
dispatches to a **driver**, which owns containment, the filesystem
operations, and §2's semantics against its own store.

```python
class FsDriver(Protocol):
    def read(self, path: str) -> bytes: ...
    def write(self, path: str, content: bytes) -> WriteResult: ...
    def delete(self, path: str) -> None: ...
    def rename(self, src: str, dst: str) -> RenameResult: ...
    def copy(self, src: str, dst: str) -> WriteResult: ...
    def mkdtemp(self) -> str: ...          # §5.6; returns the relative path
    def stat(self, path: str) -> StatResult: ...       # §5.7
```

Methods are synchronous — a driver does blocking I/O, and the surface keeps
it off the event loop. A driver raises `FileMissing`, `PermissionDenied`,
or `InvalidTarget` for the conditions §6 enumerates; anything
else it raises is a failing store, which the surface maps to
`503 upstream_unavailable` — never to a fabricated success (SPEC.md §1.2).

Drivers defined alongside this document:

| Driver | Behaviour |
|---|---|
| `local` | The configured root on the local filesystem, exactly as §2 describes — temp-file-and-`rename(2)` writes, `rename(2)` renames, `unlink(2)` deletes; scratch directories (§5.6) under `tmp/`. Performs no network I/O. |

Drivers for other storage implement the same Protocol privately without
modification to this module or its surface. Naming a driver this build does
not ship fails at boot.
