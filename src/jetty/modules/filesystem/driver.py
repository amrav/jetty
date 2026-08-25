"""The ``FsDriver`` protocol and its typed failures (filesystem-v1 §7).

The surface (module.py) validates the wire contract — path syntax — and
dispatches to a driver; a driver owns
containment, the filesystem operations, and the unix semantics of
filesystem-v1 §2 against its own store. Nothing in this file knows about
URLs, JSON, or HTTP.

Methods are synchronous: a driver does blocking I/O, and the surface keeps
it off the event loop (same reasoning as the hg module's sync handlers).

Error contract (filesystem-v1 §7): a driver raises the typed exceptions
below for conditions the protocol distinguishes. Anything else it raises is
a failing store: the surface maps it to ``503 upstream_unavailable``, never
to a fabricated success (SPEC.md §1.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class FileMissing(Exception):
    """No file at the path, or a missing directory on the way to it
    (``ENOENT``, ``ENOTDIR``) → ``404 not_found``."""


class PermissionDenied(Exception):
    """The store refused the operation for the sidecar's own identity
    (``EACCES``, ``EPERM``, ``EROFS``) → ``403 permission_denied``."""


class InvalidTarget(Exception):
    """The path is syntactically fine but names something unservable: it
    resolves outside the driver's authority, hits a symlink loop, or is not
    a regular file → ``400 invalid_request``."""


@dataclass
class WriteResult:
    size: int
    #: True iff no file existed at the path before this write.
    created: bool


@dataclass
class RenameResult:
    #: True iff nothing existed at the destination before the rename.
    created: bool


class FsDriver(Protocol):
    def read(self, path: str) -> bytes: ...
    def write(self, path: str, content: bytes) -> WriteResult: ...
    def delete(self, path: str) -> None: ...
    def rename(self, src: str, dst: str) -> RenameResult: ...
    def copy(self, src: str, dst: str) -> WriteResult: ...
    #: filesystem-v1 §5.6: a fresh private scratch directory under the
    #: store's scratch area; returns its relative path.
    def mkdtemp(self) -> str: ...
