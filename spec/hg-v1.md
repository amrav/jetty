# Jetty module: `hg` — v1

Mount: `/hg/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4, which this document does not restate.

The `hg` module exposes a **read-only** view of the local Mercurial
repositories under one configured root directory: their history, the
content of any file at any revision, and the state of the working
directory. It exists so an OSS binary can ask VCS questions without
carrying a Mercurial client, filesystem access, or knowledge of the host's
VCS configuration.

Every request names its repository with the `repo` parameter (§2a); the
configured root is the module's entire filesystem authority, and nothing
outside it is reachable regardless of what a request asks for.

It is read-only as a matter of protocol, not of politeness: v1 defines no
endpoint that mutates the repository, the store, or the working directory,
and an implementation **MUST NOT** add one under this module name.

---

## 1. Scope

In scope: repository summary, changeset log, one changeset with its file
status list, changeset diffs, working-directory / revision-pair status, and
file content at a revision.

Deliberately out of scope for v1: manifests (full file listings at a
revision), branch/bookmark/tag enumeration, annotate/blame, phases as a
queryable resource, revset queries, subrepositories, and anything touching a
second repository (push/pull/incoming/outgoing).

---

## 2. Revision addressing

Every `{rev}` below accepts: a full 40-hex nodeid, an unambiguous short
prefix, a local revision number, `tip`, `.` (working-directory parent), a
branch name (its tipmost head), or a bookmark name.

An implementation **MUST** reject a revision containing characters outside
`[A-Za-z0-9._/-]` with `400 invalid_request` before it reaches any query
engine. Everything excluded (quotes, parentheses, whitespace, `: ~ ^ ! +`)
is revset or range syntax; accepting it would turn every `{rev}` parameter
into a query language.

Responses always carry full 40-hex nodeids, never the alias the client used.
Local revision numbers appear alongside (`rev`) for display but are **not**
stable across clones; clients **MUST NOT** persist them.

An ambiguous short prefix is `400 invalid_request`. An unknown revision is
`404 not_found`.

---

## 2a. Repository addressing

Every endpoint takes a required `repo` query parameter: the repository's
path **relative to the configured root**, e.g. `repo=widget` or
`repo=team/widget`.

An implementation **MUST** reject, with `400 invalid_request`, a `repo`
that is absolute, contains `..`, empty, or `.` segments, or a backslash —
and **MUST** verify that the *resolved* path (symlinks followed) still
lies under the resolved root, so a symlink inside the root cannot become a
door out of it. A `repo` that passes those checks but names no Mercurial
repository is `404 not_found`.

This parameter selects among pre-existing repositories; it **MUST NOT**
create, initialize, or modify anything (the module is read-only, §0).

---

## 3. Configuration

```toml
[modules.hg]
enabled = true
root = "/srv/checkouts"   # required: repositories live under this directory
hg_bin = "hg"             # optional: the Mercurial executable
```

| Key | Type | Required | Notes |
|---|---|---|---|
| `root` | string | yes | Directory whose subdirectories are the servable repositories. A missing directory or an unrunnable `hg_bin` **MUST** abort boot (SPEC.md §1.2), not serve errors. Individual repositories are checked per request — they come and go while the sidecar runs. |
| `hg_bin` | string | no, default `"hg"` | Executable to invoke. |

An implementation that shells out **MUST** neutralise the host user's
Mercurial configuration (for the reference implementation: `HGPLAIN=1`,
empty `HGRCPATH`) so that user aliases, extensions and hooks can neither
reshape output nor run code on behalf of a request.

---

## 4. Common types

### Changeset

```json
{
  "node": "9c3ba5b6a99843a97b5ec04a8660a976506be433",
  "rev": 1,
  "branch": "default",
  "parents": ["40856a19815ae0ef25a89def1f049034877bd0dd"],
  "user": "Bob <b@example.com>",
  "date": "1970-01-01T01:00:20+01:00",
  "desc": "second",
  "phase": "draft"
}
```

`parents` has one entry normally, two for a merge, and never contains the
null node. `date` is ISO 8601 in the author's own UTC offset. `phase` is one
of `public`, `draft`, `secret`.

### File status

```json
{ "path": "a2.txt", "status": "A", "copy_source": "a.txt" }
```

`status` is Mercurial's own alphabet, verbatim:

| Code | Meaning |
|---|---|
| `M` | modified |
| `A` | added (tracked; not yet committed when the comparison involves the working directory) |
| `R` | removed (the VCS knows about the removal) |
| `!` | missing (deleted behind the VCS's back, still tracked) |
| `?` | untracked |

`!` and `?` can only appear when the comparison target is the working
directory. `copy_source` is present exactly when an `A` is a recorded
copy or rename. Clean (`C`) and ignored (`I`) files are not reported in v1.

---

## 5. Endpoints

All endpoints are `GET`, and all take the required `repo` parameter (§2a)
in addition to what each table below lists. The working directory is
addressed only where a row below says so; it is not a valid `{rev}`.

### 5.1 `GET /hg/v1/repo?repo=widget` — summary

```json
{
  "root": "/srv/checkouts/widget",
  "tip": "9c3ba5b6…",
  "wdir_parents": ["9c3ba5b6…"],
  "branch": "default",
  "dirty": true
}
```

`tip` is `null` and `wdir_parents` empty in an empty repository.
`wdir_parents` has two entries during an uncommitted merge. `branch` is the
branch the next commit would land on. `dirty` follows `hg identify`'s `+`:
any `M`/`A`/`R`/`!` — untracked files alone do not make a checkout dirty.

### 5.2 `GET /hg/v1/changesets` — log

| Param | Default | Notes |
|---|---|---|
| `start` | `tip` | List this revision and its ancestors, newest first. |
| `path` | — | Only changesets touching this file (literal path, no patterns). |
| `user` | — | Substring match on the user field. |
| `limit` | 50 | 1–200. |

```json
{ "changesets": [ … ], "next": "40856a19…" }
```

`next` is the cursor: pass it back as `start` for the following page, `null`
when the history is exhausted. It is a nodeid, so a commit landing between
requests cannot shift the page window.

### 5.3 `GET /hg/v1/changesets/{rev}` — one changeset

A Changeset (§4) plus `files`: the file-status list of what it changed,
copies recorded.

### 5.4 `GET /hg/v1/changesets/{rev}/diff` — the change as a diff

Returns `text/x-diff`: a git-style unified diff (rename/copy aware), the
whole changeset or one file with `?path=`. An empty diff is a `200` with an
empty body, not an error.

### 5.5 `GET /hg/v1/status` — what differs

| Param | Default | Meaning |
|---|---|---|
| `from` | `to`'s parent | Comparison base. |
| `to` | `wdir` | Comparison target; `wdir` means the working directory. |

So: bare `/status` answers "what is uncommitted"; `?from=X` answers "what
changed since X"; `?from=X&to=Y` compares two revisions; `?to=Y` answers
"what did Y change". Response: `{ "files": [ … ] }` (§4). `!` and `?`
entries appear only when `to` is the working directory.

### 5.6 `GET /hg/v1/files/{rev}/{path}` — file content

Raw bytes of `path` as of `{rev}`, `Content-Type` guessed from the file
name, `application/octet-stream` when unguessable. `404 not_found`
distinguishes an unknown revision from a path not tracked at that revision
by message; the code is the same. Working-directory content is not served —
read the file.

---

## 6. Errors

No additional error codes beyond SPEC.md §3.1. Mapping:

| Condition | Response |
|---|---|
| Revision fails §2's charset; malformed/traversing path; ambiguous prefix; `repo` fails §2a's rules or escapes the root | `400 invalid_request` |
| Unknown revision; path not tracked at the revision; `repo` naming no repository | `404 not_found` |
| `hg` executable missing; invocation timed out | `503 upstream_unavailable` |
| Any other `hg` failure | `502 upstream_error` |
