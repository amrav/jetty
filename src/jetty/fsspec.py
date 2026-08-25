"""fsspec backend for the sidecar's ``filesystem`` module.

A client, not a module: nothing here runs inside the sidecar, and the
sidecar never imports it. It talks to a *running* sidecar over the
published wire contract (spec/filesystem-v1.md), so it works against any
conformant implementation, not just this repository's. The transport is
``http.client`` directly — over the sidecar's unix socket by default, TCP
when configured. Needs the ``fsspec`` extra: ``pip install jetty[fsspec]``.

Scope follows filesystem-v1 being a whole-file API: reads and writes buffer
the entire file, ``mv``/``cp_file`` are the sidecar's atomic server-side
rename/copy, ``gettmpdir()`` returns a fresh server-created scratch
directory, and there is no directory listing — ``ls`` and everything built
on it raise ``NotImplementedError``.

When the sidecar does not offer the module — jetty modules are opt-in, and
disabled is the default — the backend falls back to the normal local
filesystem (configurable: ``local_fallback``), so the same ``jetty://``
code runs in both deployments. Only file access degrades; the sidecar's
other modules (sql, mail, …) are untouched by the fallback.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import socket
import stat as stat_module
import tempfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fsspec import register_implementation
from fsspec.spec import AbstractFileSystem

__all__ = ["JettyFileSystem"]

#: The sidecar's default control listener (SPEC.md §2.1).
DEFAULT_UDS = "/tmp/jetty/jetty.sock"

_MOUNT = "/filesystem/v1"


class _UDSConnection(http.client.HTTPConnection):
    """``http.client`` over a unix domain socket. The ``localhost`` host
    only feeds the Host header; the socket path is the real address."""

    def __init__(self, uds_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._uds_path = uds_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._uds_path)
        self.sock = sock


class _JettyFile(io.BytesIO):
    """A whole file in memory. Read modes are pre-filled; write modes upload
    the buffer to the sidecar on ``close`` — which is where a write error
    (permission, missing parent) surfaces."""

    def __init__(self, fs: "JettyFileSystem", path: str, mode: str, initial: bytes) -> None:
        super().__init__(initial)
        self.fs = fs
        self.path = path
        self.mode = mode

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self.mode in ("wb", "ab", "xb"):
                self.fs.pipe_file(self.path, self.getvalue())
        finally:
            super().close()


class JettyFileSystem(AbstractFileSystem):
    """filesystem-v1 as an fsspec filesystem.

    Parameters
    ----------
    uds:
        Path of the sidecar's unix socket. Default: ``/tmp/jetty/jetty.sock``.
    tcp:
        ``host:port`` of a TCP control listener. Mutually exclusive with
        ``uds``.
    timeout:
        Socket timeout in seconds for each request.
    local_fallback:
        When the sidecar is reachable but does not offer the filesystem
        module (disabled or an older build), operate on the normal local
        filesystem instead — same relative paths, resolved against the
        working directory. Default True. Set False to require the remote
        module and fail loudly. An unreachable sidecar always raises,
        either way.
    """

    protocol = "jetty"
    root_marker = ""

    def __init__(
        self,
        uds: str | None = None,
        tcp: str | None = None,
        timeout: float = 60.0,
        local_fallback: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if uds and tcp:
            raise ValueError("configure uds or tcp, not both")
        self.tcp = tcp
        self.uds = uds if uds else (None if tcp else DEFAULT_UDS)
        self.timeout = timeout
        self.local_fallback = local_fallback
        #: None = not yet probed; True = the sidecar serves the module;
        #: False = it does not (fallback or strict error per config).
        self._remote: bool | None = None

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        return super()._strip_protocol(path).lstrip("/")

    # --- transport ------------------------------------------------------

    def _connection(self) -> http.client.HTTPConnection:
        if self.uds:
            return _UDSConnection(self.uds, self.timeout)
        host, _, port = self.tcp.rpartition(":")
        return http.client.HTTPConnection(
            host.strip("[]"), int(port), timeout=self.timeout
        )

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, bytes, str | None]:
        conn = self._connection()
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            conn.request(method, url, body=body, headers=headers)
            resp = conn.getresponse()
            length = resp.getheader("Content-Length")
            return resp.status, resp.read(), length
        finally:
            conn.close()

    @staticmethod
    def _file_url(path: str) -> str:
        return f"{_MOUNT}/files/" + quote(path, safe="/")

    @staticmethod
    def _raise(status: int, data: bytes, path: str) -> None:
        """SPEC.md §3.1 envelope → the Python exception vocabulary."""
        code, message = "", ""
        try:
            err = json.loads(data)["error"]
            code, message = err.get("code", ""), err.get("message", "")
        except (ValueError, KeyError, TypeError):
            pass
        if not code:
            # Bodiless or unparseable error: fall back to the status.
            code = {400: "invalid_request", 403: "permission_denied",
                    404: "not_found"}.get(status, "")
        detail = f"{path}: {message or code or f'HTTP {status}'}"
        if code == "not_found":
            raise FileNotFoundError(detail)
        if code == "permission_denied":
            raise PermissionError(detail)
        if code == "invalid_request":
            raise ValueError(detail)
        raise OSError(f"{detail} (HTTP {status}, code {code or 'unknown'})")

    def _is_remote(self) -> bool:
        """Once per instance: does the sidecar offer the filesystem module?

        ``GET /v1/meta`` is the supported discovery path (SPEC.md §4.2) and
        is build-agnostic: a sidecar that predates the module simply does
        not list it — where a per-request probe could never tell "module
        absent" from "file absent". An *unreachable* sidecar always raises:
        silently going local there would hide a misconfigured socket.
        """
        if self._remote is None:
            where = self.uds or self.tcp
            try:
                status, data, _ = self._request("GET", "/v1/meta")
            except OSError as exc:
                raise OSError(
                    f"no jetty sidecar reachable at {where}: {exc}"
                ) from exc
            names: list[Any] = []
            if status == 200:
                try:
                    names = [m.get("name") for m in json.loads(data)["modules"]]
                except (ValueError, KeyError, TypeError, AttributeError):
                    names = []
            self._remote = "filesystem" in names
        if not self._remote and not self.local_fallback:
            raise OSError(
                f"the jetty sidecar at {self.uds or self.tcp} does not offer "
                "the filesystem module, and local_fallback is off"
            )
        return self._remote

    # --- whole-file operations (filesystem-v1 §5) -----------------------

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kwargs: Any
    ) -> bytes:
        path = self._strip_protocol(path)
        if not self._is_remote():
            with open(path, "rb") as f:
                data = f.read()
        else:
            status, data, _ = self._request("GET", self._file_url(path))
            if status != 200:
                self._raise(status, data, path)
        # The wire is whole-file; a requested range is sliced locally.
        if start is not None or end is not None:
            data = data[start:end]
        return data

    def pipe_file(self, path: str, value: bytes, **kwargs: Any) -> None:
        path = self._strip_protocol(path)
        if not self._is_remote():
            with open(path, "wb") as f:
                f.write(bytes(value))
            return
        status, data, _ = self._request(
            "PUT", self._file_url(path), bytes(value), "application/octet-stream"
        )
        if status != 200:
            self._raise(status, data, path)

    def rm_file(self, path: str) -> None:
        path = self._strip_protocol(path)
        if not self._is_remote():
            if os.path.isdir(path):
                os.rmdir(path)  # matches the wire: empty directories only
            else:
                os.remove(path)
            return
        status, data, _ = self._request("DELETE", self._file_url(path))
        if status != 200:
            self._raise(status, data, path)

    _rm = rm_file

    def _two_path(self, op: str, path1: str, path2: str) -> None:
        src = self._strip_protocol(path1)
        dst = self._strip_protocol(path2)
        body = json.dumps({"from": src, "to": dst}).encode("utf-8")
        status, data, _ = self._request(
            "POST", f"{_MOUNT}/{op}", body, "application/json"
        )
        if status != 200:
            self._raise(status, data, f"{src} -> {dst}")

    def mv(self, path1: str, path2: str, **kwargs: Any) -> None:
        """Atomic server-side rename(2) — never download-reupload-delete."""
        if not self._is_remote():
            os.replace(self._strip_protocol(path1), self._strip_protocol(path2))
            return
        self._two_path("rename", path1, path2)

    def cp_file(self, path1: str, path2: str, **kwargs: Any) -> None:
        """Server-side copy: the content never crosses to the client."""
        if not self._is_remote():
            shutil.copyfile(
                self._strip_protocol(path1), self._strip_protocol(path2)
            )
            return
        self._two_path("copy", path1, path2)

    def gettmpdir(self) -> str:
        """A fresh private scratch directory on the sidecar.

        ``mkdtemp(3)`` semantics, deliberately: each call returns a NEW
        uniquely-named directory, so concurrent clients cannot collide in a
        shared scratch path. The returned path is opaque — where the scratch
        area lives is the sidecar implementation's choice. Write into it
        with ordinary paths under the returned prefix; clean up by removing
        its files and then the directory itself (``rm_file`` works on an
        empty directory).
        """
        if not self._is_remote():
            # Scratch under the working directory, mirroring the reference
            # driver's tmp-under-root; mkdtemp semantics either way.
            return os.path.basename(tempfile.mkdtemp(prefix=".jetty-tmp-", dir="."))
        status, data, _ = self._request("POST", f"{_MOUNT}/tmpdir")
        if status != 200:
            self._raise(status, data, "tmpdir")
        return json.loads(data)["path"]

    # --- metadata, within what a whole-file API can say -----------------

    def _stat(self, path: str) -> dict[str, Any]:
        if not self._is_remote():
            st = os.stat(path or ".")
            if stat_module.S_ISREG(st.st_mode):
                kind = "file"
            elif stat_module.S_ISDIR(st.st_mode):
                kind = "directory"
            else:
                kind = "other"
            return {
                "size": st.st_size,
                "type": kind,
                "mode": f"{stat_module.S_IMODE(st.st_mode):04o}",
                "mtime": datetime.fromtimestamp(
                    st.st_mtime, timezone.utc
                ).isoformat(),
            }
        status, data, _ = self._request(
            "GET", f"{_MOUNT}/stat/" + quote(path, safe="/")
        )
        if status != 200:
            self._raise(status, data, path)
        return json.loads(data)

    def exists(self, path: str, **kwargs: Any) -> bool:
        try:
            self._stat(self._strip_protocol(path))
        except FileNotFoundError:
            return False
        return True

    def info(self, path: str, **kwargs: Any) -> dict[str, Any]:
        path = self._strip_protocol(path)
        row = self._stat(path)
        return {
            "name": path,
            "size": row["size"],
            "type": row["type"],           # "file" | "directory" | "other"
            "mode": row["mode"],
            "mtime": row["mtime"],
        }

    def modified(self, path: str) -> datetime:
        """Last content modification, as the sidecar's stat reports it."""
        return datetime.fromisoformat(
            self._stat(self._strip_protocol(path))["mtime"]
        )

    def ls(self, path: str, detail: bool = True, **kwargs: Any) -> list:
        raise NotImplementedError(
            "filesystem-v1 defines no directory listing; "
            "address files by their full path"
        )

    # --- open -----------------------------------------------------------

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        autocommit: bool = True,
        cache_options: dict | None = None,
        **kwargs: Any,
    ) -> _JettyFile:
        path = self._strip_protocol(path)
        if not self._is_remote():
            return open(path, mode)
        if mode == "rb":
            return _JettyFile(self, path, mode, self.cat_file(path))
        if mode == "wb":
            return _JettyFile(self, path, mode, b"")
        if mode == "xb":
            if self.exists(path):
                raise FileExistsError(path)
            return _JettyFile(self, path, mode, b"")
        if mode == "ab":
            try:
                initial = self.cat_file(path)
            except FileNotFoundError:
                initial = b""
            f = _JettyFile(self, path, mode, initial)
            f.seek(0, io.SEEK_END)
            return f
        raise NotImplementedError(f"mode {mode!r} is not supported")


# `import jetty.fsspec` is enough to make fsspec.filesystem("jetty") work;
# an installed wheel also registers via the fsspec.specs entry point.
register_implementation("jetty", JettyFileSystem, clobber=True)
