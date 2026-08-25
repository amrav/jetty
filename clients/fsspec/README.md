# jetty-fsspec

An [fsspec](https://filesystem-spec.readthedocs.io/) backend for the Jetty
sidecar's [`filesystem` module](../../spec/filesystem-v1.md).

**Standalone by design.** This package depends on `fsspec` and the Python
standard library, nothing else — in particular it does not import the jetty
package. It speaks the published wire contract (`spec/filesystem-v1.md`)
over HTTP, so it works against any conformant implementation, not just the
reference sidecar. HTTP over the unix socket is done with `http.client`
directly; there is no HTTP-library dependency.

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
- Parent directories are not created (`mkdir` is not part of the wire
  contract); writing into a missing directory raises `FileNotFoundError`.

## Licence

Apache-2.0.
