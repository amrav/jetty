# `jetty.fsspec` — fsspec backend for the `filesystem` module

An [fsspec](https://filesystem-spec.readthedocs.io/) backend for the
sidecar's [`filesystem` module](../spec/filesystem-v1.md). Install the
extra: `pip install jetty[fsspec]`.

A client, not a module: nothing here runs inside the sidecar, and the
sidecar never imports it. It talks to a **running** sidecar over the
published wire contract, so it works against any conformant implementation,
not just this repository's. The transport is `http.client` directly — over
the sidecar's unix socket by default, TCP when configured — so `fsspec`
itself is the only dependency the extra adds.

## Why

fsspec is the Python ecosystem's pluggable-filesystem interface — the layer
behind `s3fs` and `gcsfs`, accepted by pandas, pyarrow, dask, xarray, and
friends. With this backend, code written against fsspec switches between a
local directory and a jetty-served tree by configuration, with no code
changes:

```python
import fsspec

fs = fsspec.filesystem("jetty")                        # default socket
fs = fsspec.filesystem("jetty", uds="/run/jetty.sock") # explicit socket
fs = fsspec.filesystem("jetty", tcp="127.0.0.1:7241")  # TCP listener

fs.pipe_file("notes.txt", b"hello")
fs.cat_file("notes.txt")
with fs.open("reports/q3.csv") as f:
    ...

scratch = fs.gettmpdir()                 # fresh private dir, mkdtemp(3)
fs.pipe_file(f"{scratch}/stage.parquet", blob)

# or by URL, in any fsspec-aware library:
pd.read_csv("jetty://reports/q3.csv",
            storage_options={"uds": "/run/jetty.sock"})
```

Paths are relative to the module's configured root; the sidecar enforces
containment (filesystem-v1 §3).

## When the sidecar does not offer the module

Jetty modules are opt-in, and the filesystem module — like every module —
is disabled unless configured. By default this backend detects that with
one `GET /v1/meta` probe per instance (the supported discovery path,
SPEC.md §4.2) and falls back to the **normal local filesystem**: the same
relative `jetty://` paths resolve against the working directory with plain
`open(2)` semantics, and `gettmpdir()` becomes a `mkdtemp` under the
working directory. Only file access degrades; the sidecar's other modules
(e.g. `sql` over its sqlite driver) keep going through jetty untouched.

Pass `local_fallback=False` to require the remote module: the backend then
raises `OSError` naming the missing module. An **unreachable** sidecar
always raises, in both configurations — silently going local there would
hide a misconfigured socket.

| sidecar state | default (`local_fallback=True`) | `local_fallback=False` |
|---|---|---|
| filesystem module enabled | remote (wire mapping below) | remote |
| sidecar up, module disabled/absent | local filesystem | `OSError` |
| sidecar unreachable | `OSError` | `OSError` |

## Wire mapping

| fsspec call | filesystem-v1 |
|---|---|
| `cat_file` / `open(…, "rb")` | `GET /filesystem/v1/files/{path}` |
| `pipe_file` / `open(…, "wb")` | `PUT /filesystem/v1/files/{path}` |
| `rm_file` / `rm` | `DELETE /filesystem/v1/files/{path}` |
| `mv` | `POST /filesystem/v1/rename` (atomic, server-side) |
| `cp_file` / `copy` | `POST /filesystem/v1/copy` |
| `exists` / `info` / `modified` | `GET /filesystem/v1/stat/{path}` |
| `gettmpdir` | `POST /filesystem/v1/tmpdir` |

`gettmpdir` is this backend's extension — fsspec defines no scratch-space
API. It has `mkdtemp(3)` semantics: each call returns a **new** private
directory, so concurrent clients cannot collide in a shared scratch path.
Treat the returned path as opaque: where the scratch area lives is the
sidecar implementation's choice (the reference sidecar uses `tmp/` under
its root, mode `0700`).

Errors map onto Python's own: `not_found` → `FileNotFoundError`,
`permission_denied` → `PermissionError`, `invalid_request` → `ValueError`;
transport and 5xx failures → `OSError`.

## Scope and limitations

Everything follows from filesystem-v1 being a **whole-file** API:

- Reads and writes buffer the entire file in memory; `open` returns an
  in-memory file that uploads on `close`. Writes land atomically on the
  sidecar's side.
- There is no directory listing, so `ls`, `glob`, `find`, `walk`, and
  friends raise `NotImplementedError`. `exists`/`info`/`modified` are
  backed by the wire `stat` and answer for any path, directories included.
- Parent directories are not created (there is no general-purpose `mkdir`
  on the wire); writing into a missing directory raises `FileNotFoundError`.
  The one directory-creating call is `gettmpdir()`; its directory is
  removable with `rm_file` once emptied.
