"""The ``local`` driver (filesystem-v1 §7): the configured root, with the
standard unix filesystem's semantics and link-level atomic mutation.

``_resolve`` is the only door, and it containment-checks the resolved path —
symlinks followed — before any filesystem operation: the path must land
under the root or under a scratch directory this process created and still
owns (filesystem-v1 §3, §5.6), so a symlink inside either cannot become a
door out of it. Scratch directories are ``mkdtemp(3)`` in the process's
temporary directory, never under the root, and are remembered by identity
(``st_dev``/``st_ino``) rather than by name: the temporary directory is
shared, world-writable space, and a name alone would let whoever recreates a
deleted scratch directory inherit every later read and write. Beyond
containment the driver adds nothing the kernel does not: operations run with the
process's own identity, and its refusals surface as the typed exceptions the
protocol distinguishes rather than being masked (filesystem-v1 §2).

Every mutation is link-level, which is where the atomicity comes from:

- a **write** (and a copy's destination) is a same-directory temporary file,
  fsynced, then ``rename(2)``d into place — a concurrent reader sees the old
  content or the new in full, and a crash mid-write leaves the old file;
- a **rename** is ``rename(2)`` itself; one that cannot be atomic (``EXDEV``:
  a filesystem boundary inside the root) is refused, never degraded to
  copy-plus-delete;
- a **delete** is ``unlink(2)``.

The visible consequence is that directory write permission governs every
mutation, exactly as it does for ``mv(1)`` and ``rm(1)``.

Two checks are not the kernel's: only regular files are served (a FIFO would
block the worker indefinitely on ``open``), and a copy onto the same file is
refused up front rather than half-done.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat as stat_module
import tempfile

from jetty.modules.filesystem.driver import (
    FileMissing,
    InvalidTarget,
    PermissionDenied,
    RenameResult,
    StatResult,
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
        #: filesystem-v1 §5.6: every scratch directory this process has
        #: handed out — resolved path → the (st_dev, st_ino) it was created
        #: with. Entries leave when the directory is deleted through this
        #: driver or found replaced (``_scratch_alive``).
        self._scratch: dict[str, tuple[int, int]] = {}

    def _resolve(self, path: str) -> str:
        """Absolute path → resolved path inside the authority, or a refusal.

        The surface has already validated syntax (absolute, no ``..``, no
        empty segments); this check is about where the path *leads*. The
        RESOLVED path (symlinks followed) must sit under the resolved root
        or under a live scratch directory of this process (filesystem-v1
        §3). ``realpath`` resolves the existing prefix and keeps the rest,
        so the check also holds for files being created.
        """
        full = os.path.realpath(path)
        root = os.path.realpath(self.root)
        if full == root or full.startswith(root + os.sep):
            return full
        scratch = self._scratch_ancestor(full)
        if scratch is not None and self._scratch_alive(scratch):
            return full
        raise InvalidTarget(
            f"{path!r} lies outside the root and every scratch directory "
            "this server created"
        )

    def _scratch_ancestor(self, full: str) -> str | None:
        """The remembered scratch directory ``full`` is or lies under, if any."""
        cursor = full
        while True:
            if cursor in self._scratch:
                return cursor
            parent = os.path.dirname(cursor)
            if parent == cursor:
                return None
            cursor = parent

    def _scratch_alive(self, directory: str) -> bool:
        """Is what sits at this path still the directory mkdtemp made?

        Scratch space is shared, world-writable storage: once the directory
        is gone — deleted by its caller, aged out by the host's tmpfiles
        cleaner — any local user may create one of the same name, and
        honouring the name would hand that squatter every later read and
        write (filesystem-v1 §5.6). So the record is checked against the
        object now at the path: a real directory (not a symlink), the same
        ``(st_dev, st_ino)`` as created, owned by this process's effective
        uid — an inode number can be recycled, a uid cannot be forged. A
        mismatch retires the record, not just the request.
        """
        try:
            st = os.lstat(directory)
        except OSError:
            self._scratch.pop(directory, None)
            return False
        alive = (
            stat_module.S_ISDIR(st.st_mode)
            and st.st_uid == os.geteuid()
            and (st.st_dev, st.st_ino) == self._scratch[directory]
        )
        if not alive:
            self._scratch.pop(directory, None)
        return alive

    def _stat_regular(self, full: str, path: str) -> os.stat_result | None:
        """Stat, insisting on a regular file. None = nothing there (which
        the operations treat differently)."""
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

    def _require(self, full: str, path: str) -> os.stat_result:
        st = self._stat_regular(full, path)
        if st is None:
            raise FileMissing(f"no file at {path!r}")
        return st

    def _open_temp(self, directory: str, mode: int) -> tuple[int, str]:
        """A fresh ``O_EXCL`` temporary next to the target. The mode argument
        is subject to the process umask, exactly as in ``open(2)`` — which is
        how a created file ends up ``0666``-as-modified-by-umask without this
        driver ever reading the umask."""
        while True:
            tmp = os.path.join(directory, f".jetty-tmp-{secrets.token_hex(8)}")
            try:
                return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), tmp
            except FileExistsError:
                continue

    def _replace(
        self, full: str, path: str, content: bytes, create_mode: int = 0o666
    ) -> WriteResult:
        """Temp + fsync + ``rename(2)``: the whole atomic-write story
        (filesystem-v1 §2). ``create_mode`` is the pre-umask mode when
        nothing exists at the path — ``0666`` for a plain write, the source's
        bits for a copy."""
        st = self._stat_regular(full, path)
        try:
            fd, tmp = self._open_temp(
                os.path.dirname(full), create_mode if st is None else 0o600
            )
        except OSError as exc:
            raise _translate(exc, path) from exc
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            if st is not None:
                # Preserve the replaced file's exact bits: chmod, not the
                # open mode, so the umask cannot re-mask them.
                os.chmod(tmp, stat_module.S_IMODE(st.st_mode))
            os.rename(tmp, full)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise _translate(exc, path) from exc
        return WriteResult(size=len(content), created=st is None)

    def read(self, path: str) -> bytes:
        full = self._resolve(path)
        self._require(full, path)
        try:
            with open(full, "rb") as f:
                return f.read()
        except OSError as exc:
            raise _translate(exc, path) from exc

    def write(self, path: str, content: bytes) -> WriteResult:
        full = self._resolve(path)
        return self._replace(full, path, content)

    def delete(self, path: str) -> None:
        full = self._resolve(path)
        try:
            st = os.stat(full)
        except FileNotFoundError:
            raise FileMissing(f"no file at {path!r}") from None
        except OSError as exc:
            raise _translate(exc, path) from exc
        try:
            if stat_module.S_ISDIR(st.st_mode):
                # The cleanup half of mkdtemp (filesystem-v1 §5.3): an empty
                # directory goes by rmdir(2); a populated one is refused.
                os.rmdir(full)
                # A scratch directory that is gone is an outsider again.
                self._scratch.pop(full, None)
            elif not stat_module.S_ISREG(st.st_mode):
                raise InvalidTarget(f"{path!r} is not a regular file")
            else:
                os.unlink(full)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise InvalidTarget(f"{path!r} is not empty") from exc
            raise _translate(exc, path) from exc

    def mkdtemp(self) -> str:
        """filesystem-v1 §5.6: a fresh mkdtemp(3) directory in the process's
        temporary directory — never under the root — 0700 as modified by the
        umask, remembered by identity for the life of this process. Returns
        its absolute (resolved) path, which is also the record's key."""
        try:
            full = os.path.realpath(tempfile.mkdtemp(prefix="jetty-"))
            st = os.lstat(full)
        except OSError as exc:
            raise _translate(exc, "tmpdir") from exc
        self._scratch[full] = (st.st_dev, st.st_ino)
        return full

    def stat(self, path: str) -> StatResult:
        """stat(2), symlinks followed. Any object type: a stat carries no
        blocking risk, so the regular-files gate deliberately stays out."""
        full = self._resolve(path)
        try:
            st = os.stat(full)
        except FileNotFoundError:
            raise FileMissing(f"no file at {path!r}") from None
        except OSError as exc:
            raise _translate(exc, path) from exc
        if stat_module.S_ISREG(st.st_mode):
            kind = "file"
        elif stat_module.S_ISDIR(st.st_mode):
            kind = "directory"
        else:
            kind = "other"
        return StatResult(
            type=kind,
            size=st.st_size,
            mode=stat_module.S_IMODE(st.st_mode),
            mtime=st.st_mtime,
        )

    def rename(self, src: str, dst: str) -> RenameResult:
        src_full = self._resolve(src)
        dst_full = self._resolve(dst)
        self._require(src_full, src)
        dst_st = self._stat_regular(dst_full, dst)
        try:
            os.rename(src_full, dst_full)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise InvalidTarget(
                    f"{src!r} -> {dst!r} crosses a filesystem boundary; "
                    "an atomic rename is impossible"
                ) from exc
            raise _translate(exc, f"{src} -> {dst}") from exc
        return RenameResult(created=dst_st is None)

    def copy(self, src: str, dst: str) -> WriteResult:
        src_full = self._resolve(src)
        dst_full = self._resolve(dst)
        if src_full == dst_full:
            raise InvalidTarget(f"{src!r} and {dst!r} are the same file")
        src_st = self._require(src_full, src)
        try:
            with open(src_full, "rb") as f:
                content = f.read()
        except OSError as exc:
            raise _translate(exc, src) from exc
        # cp(1)'s creation rule: a fresh destination takes the source's
        # permission bits (umask applied); an existing one keeps its own.
        return self._replace(
            dst_full, dst, content, create_mode=stat_module.S_IMODE(src_st.st_mode)
        )
