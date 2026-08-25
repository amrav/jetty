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

## Wire mapping

| fsspec call | filesystem-v1 |
|---|---|
| `cat_file` / `open(…, "rb")` | `GET /filesystem/v1/files/{path}` |
| `pipe_file` / `open(…, "wb")` | `PUT /filesystem/v1/files/{path}` |
| `rm_file` / `rm` | `DELETE /filesystem/v1/files/{path}` |
| `mv` | `POST /filesystem/v1/rename` (atomic, server-side) |
| `cp_file` / `copy` | `POST /filesystem/v1/copy` |
| `exists` / `info` | `HEAD /filesystem/v1/files/{path}` |
| `gettmpdir` | `POST /filesystem/v1/tmpdir` |

`gettmpdir` is this backend's extension — fsspec defines no scratch-space
API. It has `mkdtemp(3)` semantics: each call returns a **new** private
directory (mode `0700` server-side), so concurrent clients cannot collide
in a shared scratch path.

Errors map onto Python's own: `not_found` → `FileNotFoundError`,
`permission_denied` → `PermissionError`, `invalid_request` → `ValueError`;
transport and 5xx failures → `OSError`.

## Scope and limitations

Everything follows from filesystem-v1 being a **whole-file** API:

- Reads and writes buffer the entire file in memory; `open` returns an
  in-memory file that uploads on `close`. Writes land atomically on the
  sidecar's side.
- There is no directory listing or stat, so `ls`, `glob`, `find`, `walk`,
  and friends raise `NotImplementedError`. `exists`/`info` answer for
  files, not directories.
- Parent directories are not created (there is no general-purpose `mkdir`
  on the wire); writing into a missing directory raises `FileNotFoundError`.
  The one directory-creating call is `gettmpdir()`; its directory is
  removable with `rm_file` once emptied.
