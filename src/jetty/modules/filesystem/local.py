"""The ``local`` driver (filesystem-v1 §7): the configured root, exactly as
the standard unix filesystem behaves.

``_resolve`` is the only door, and it containment-checks the resolved path —
symlinks followed — before any filesystem operation, so a symlink inside the
root cannot become a door out of it (filesystem-v1 §3). Beyond containment
the driver adds nothing: operations run with the process's own identity,
creation is ``open(2)`` with ``O_CREAT`` under the process umask, replacement
is ``O_TRUNC`` in place (inode, mode, owner, hard links survive), parents are
not created, and the kernel's refusals surface as the typed exceptions the
protocol distinguishes rather than being masked (filesystem-v1 §2).

One check is not the kernel's: only regular files are served. A FIFO would
block the worker indefinitely on ``open``; refusing non-regular files up
front turns that hang into an immediate ``InvalidTarget``.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module

from jetty.modules.filesystem.driver import (
    FileMissing,
    InvalidTarget,
    PermissionDenied,
    WriteResult,
)


def _translate(exc: OSError, path: str) -> Exception:
    """One ``OSError`` → one exception the protocol distinguishes.

    Anything unrecognised is returned unchanged: the surface maps it to
    ``503 upstream_unavailable``, which is the truth for ``EIO``, ``ENOSPC``
    and their kin.
    """
    if isinstance(exc, PermissionError) or exc.errno in (errno.EPERM, errno.EROFS):
        return PermissionDenied(f"permission denied for {path!r}")
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return FileMissing(f"no file at {path!r}")
    if isinstance(exc, IsADirectoryError):
        return InvalidTarget(f"{path!r} is a directory")
    if exc.errno == errno.ELOOP:
        return InvalidTarget(f"{path!r} runs through a symlink loop")
    return exc


class LocalFsDriver:
    def __init__(self, root: str) -> None:
        self.root = root

    def _resolve(self, path: str) -> str:
        """Relative path → absolute path under the root, or a refusal.

        The surface has already validated syntax (no absolute, no ``..``, no
        empty segments); this check is about where symlinks *lead*. The
        RESOLVED path must still sit under the resolved root
        (filesystem-v1 §3). ``realpath`` resolves the existing prefix and
        keeps the rest, so the check also holds for files being created.
        """
        root = os.path.realpath(self.root)
        full = os.path.realpath(os.path.join(root, path))
        if full != root and not full.startswith(root + os.sep):
            raise InvalidTarget(f"{path!r} resolves outside the configured root")
        return full

    def _stat_regular(self, full: str, path: str) -> os.stat_result | None:
        """Stat, insisting on a regular file. None = nothing there (which
        read and write treat differently)."""
        try:
            st = os.stat(full)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _translate(exc, path) from exc
        if stat_module.S_ISDIR(st.st_mode):
            raise InvalidTarget(f"{path!r} is a directory")
        if not stat_module.S_ISREG(st.st_mode):
            raise InvalidTarget(f"{path!r} is not a regular file")
        return st

    def read(self, path: str) -> bytes:
        full = self._resolve(path)
        st = self._stat_regular(full, path)
        if st is None:
            raise FileMissing(f"no file at {path!r}")
        try:
            with open(full, "rb") as f:
                return f.read()
        except OSError as exc:
            raise _translate(exc, path) from exc

    def write(self, path: str, content: bytes) -> WriteResult:
        full = self._resolve(path)
        st = self._stat_regular(full, path)
        try:
            with open(full, "wb") as f:
                f.write(content)
        except OSError as exc:
            raise _translate(exc, path) from exc
        return WriteResult(size=len(content), created=st is None)
