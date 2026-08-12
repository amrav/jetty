"""Binary resolvers: "which binary" answered by a script at spawn time.

A release process moves binaries; the config should not have to move with
it. Each resolver is a command whose stdout names the current path(s); the
supervisor runs it lazily — before a spawn that needs one of its names —
and injects the results as `{bin.<name>}` placeholders.

Pinning is the invariant worth stating: every name a resolver `provides`
comes from ONE invocation of the script, atomically. Two services that share
a resolver and spawn within `cache_seconds` of each other get the same
answer, so a manifest read mid-release cannot hand the control plane one
version and its harness another. What pinning deliberately does NOT do is
touch running processes: a new release is picked up by whichever service
next (re)spawns, never by yanking a binary out from under a live one.

Failure is loud and attributed: exit code, timeout, malformed output, an
unknown or missing name, or a path that does not exist all raise
ResolveError carrying the script's stderr — and the caller treats that as a
spawn failure of the service that asked, so the ordinary restart budget and
backoff apply while a release is mid-publish.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat as stat_mod
import sys
import time
from pathlib import Path

from .config import ResolverConfig

_OUTPUT_LIMIT = 1 << 20  # a resolver that prints a megabyte is broken
_STDERR_TAIL = 2000
_META_SUFFIX = ".src"  # sidecar recording the source's (size, mtime_ns)
_TMP_MAX_AGE = 3600  # a .tmp- older than this is a crashed copy, not a live one


class ResolveError(RuntimeError):
    pass


def bin_root() -> Path:
    env = os.environ.get("JETTY_ORC_BIN_ROOT")
    return Path(env) if env else Path.home() / ".jetty" / "bin"


def materialize(source: str, root: Path, keep_days: float) -> tuple[str, str]:
    """Copy `source` into `root`, cached and vanish-resilient.

    The destination name is derived from the source PATH (basename plus a
    path hash), so the same source never collides with a different one and
    never re-copies while unchanged: a `.src` sidecar records the source's
    (size, mtime_ns), and a matching source is a cache hit. A changed source
    is re-copied via tmp + rename — atomic, and it never writes into a file
    some running process is executing (that would be ETXTBSY; replacing the
    directory entry is not).

    If the source cannot even be stat'd but a copy exists, the copy is used:
    that IS the feature — a network mount disappears precisely when the
    respawn needs the binary.

    Aging: every use touches the copy's mtime, and `_prune` removes pairs
    unused for `keep_days` — so a binary in active rotation never expires,
    and last month's releases do.

    Returns `(local path, content fingerprint)` — the fingerprint is the
    source's `size mtime_ns` (from the sidecar when the source is gone), and
    feeds the resolver generation so an in-place replacement at the same
    path counts as a new release.
    """
    root.mkdir(parents=True, exist_ok=True)
    _prune(root, keep_days)
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    dest = root / f"{os.path.basename(source)}-{digest}"
    meta = Path(str(dest) + _META_SUFFIX)
    try:
        src_stat = os.stat(source)
    except OSError as e:
        if dest.exists():
            print(
                f"jetty-orc: warning: {source} is unreachable ({e.strerror}); "
                f"using cached copy {dest}",
                file=sys.stderr,
            )
            _touch(dest, meta)
            try:
                fingerprint = meta.read_text()
            except OSError:
                fingerprint = "unknown"
            return str(dest), fingerprint
        raise ResolveError(
            f"{source} is unreachable ({e.strerror}) and no cached copy exists"
        ) from None
    if not stat_mod.S_ISREG(src_stat.st_mode):
        raise ResolveError(f"copy = true needs a regular file; {source} is not one")
    fingerprint = f"{src_stat.st_size} {src_stat.st_mtime_ns}"
    try:
        if dest.exists() and meta.read_text() == fingerprint:
            _touch(dest, meta)
            return str(dest), fingerprint
    except OSError:
        pass  # missing/unreadable sidecar: fall through to a fresh copy
    tmp = root / f".tmp-{os.getpid()}-{digest}"
    shutil.copy2(source, tmp)
    os.replace(tmp, dest)
    meta.write_text(fingerprint)
    _touch(dest, meta)
    return str(dest), fingerprint


def _touch(dest: Path, meta: Path) -> None:
    for path in (dest, meta):
        try:
            os.utime(path)
        except OSError:
            pass


def _prune(root: Path, keep_days: float) -> None:
    now = time.time()
    for entry in root.iterdir():
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        stale = (
            age > _TMP_MAX_AGE
            if entry.name.startswith(".tmp-")
            else age > keep_days * 86400
        )
        if stale:
            try:
                entry.unlink()
            except OSError:
                pass


def parse_output(name: str, provides: list[str], stdout: str) -> dict[str, str]:
    """`name=path` lines (blank lines and `#` comments ignored); a resolver
    providing exactly one name may print just the path. Unknown, repeated or
    missing names are errors — order never carries meaning, so a reordered
    echo cannot silently swap two binaries."""
    lines = [
        line.strip()
        for line in stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(provides) == 1 and len(lines) == 1 and "=" not in lines[0]:
        return {provides[0]: lines[0]}
    out: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise ResolveError(
                f"resolver {name!r}: unparseable output line {line!r} "
                "(want name=path)"
            )
        if key not in provides:
            raise ResolveError(
                f"resolver {name!r} returned unknown name {key!r} "
                f"(provides: {', '.join(provides)})"
            )
        if key in out:
            raise ResolveError(f"resolver {name!r} returned {key!r} twice")
        out[key] = value
    missing = [p for p in provides if p not in out]
    if missing:
        raise ResolveError(
            f"resolver {name!r} did not return: {', '.join(missing)}"
        )
    return out


class Resolvers:
    """All of an instance's resolvers, with per-resolver caching and locks."""

    def __init__(self, configs: dict[str, ResolverConfig], argvs: dict[str, list[str]]):
        self._configs = configs
        self._argvs = argvs  # cmd rendered against the static context
        self._by_bin = {
            bin_name: rname
            for rname, cfg in configs.items()
            for bin_name in cfg.provides
        }
        self._locks = {rname: asyncio.Lock() for rname in configs}
        self._cache: dict[str, tuple[float, dict[str, str], dict[str, str]]] = {}
        #: For the registry / `status`: last successful resolution per resolver.
        self.state: dict[str, dict] = {}
        #: Bumped only when a resolution's RESULT differs from the previous
        #: one. This is what group-restart decisions key on: "the release
        #: moved" is a generation change; a crash loop that keeps resolving
        #: the same paths is not, and must not bounce healthy siblings.
        self.generation: dict[str, int] = {}

    def resolver_names(self, bin_names: set[str]) -> set[str]:
        return {self._by_bin[b] for b in bin_names}

    def generations_for(self, bin_names: set[str]) -> dict[str, int]:
        return {
            rname: self.generation.get(rname, 0)
            for rname in self.resolver_names(bin_names)
        }

    async def context_for(self, bin_names: set[str]) -> dict[str, str]:
        """`{bin.<name>}` context entries for one spawn. Raises ResolveError."""
        ctx: dict[str, str] = {}
        for rname in sorted(self.resolver_names(bin_names)):
            binaries = await self._resolve(rname)
            ctx.update({f"bin.{k}": v for k, v in binaries.items()})
        return ctx

    async def _resolve(self, rname: str) -> dict[str, str]:
        cfg = self._configs[rname]
        async with self._locks[rname]:
            cached = self._cache.get(rname)
            if cached is not None:
                age = time.monotonic() - cached[0]
                if cfg.refresh == "instance" or age < cfg.cache_seconds:
                    return cached[1]
            binaries, sources, fingerprints = await self._run(rname, cfg)
            # The generation key includes the content fingerprint, not just
            # the paths: an in-place replacement at the same path is a new
            # release too, and pinned groups must treat it as one.
            if cached is None or cached[1:] != (binaries, fingerprints):
                self.generation[rname] = self.generation.get(rname, 0) + 1
            self._cache[rname] = (time.monotonic(), binaries, fingerprints)
            self.state[rname] = {"binaries": binaries, "resolved_at": time.time()}
            if sources is not None:
                self.state[rname]["copied_from"] = sources
            return binaries

    async def _run(
        self, rname: str, cfg: ResolverConfig
    ) -> tuple[dict[str, str], dict[str, str] | None, dict[str, str]]:
        """(binaries to use, sources if `copy` rewrote them, content
        fingerprints keyed by binary name)."""
        argv = self._argvs[rname]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise ResolveError(f"resolver {rname!r}: cannot run {argv[0]!r}: {e}")
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), cfg.timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ResolveError(
                f"resolver {rname!r} timed out after {cfg.timeout_seconds}s"
            )
        if proc.returncode != 0:
            tail = stderr.decode(errors="replace")[-_STDERR_TAIL:].strip()
            raise ResolveError(
                f"resolver {rname!r} exited {proc.returncode}"
                + (f"; stderr: {tail}" if tail else "")
            )
        binaries = parse_output(
            rname, cfg.provides, stdout[:_OUTPUT_LIMIT].decode(errors="replace")
        )
        for bin_name, path in binaries.items():
            if not os.path.isabs(path):
                raise ResolveError(
                    f"resolver {rname!r} returned {bin_name}={path!r}, which is "
                    "not an absolute path (services run from their own cwd)"
                )
        if cfg.copy_binaries:
            # Copies can be large and the source remote — off the event loop.
            # No existence pre-check here: a vanished source with a cached
            # copy is exactly the case `copy` exists for.
            local, fingerprints = await asyncio.to_thread(
                self._copy_all, rname, binaries, cfg
            )
            return local, binaries, fingerprints
        fingerprints: dict[str, str] = {}
        for bin_name, path in binaries.items():
            try:
                st = os.stat(path)
            except OSError:
                raise ResolveError(
                    f"resolver {rname!r} returned {bin_name}={path}, which does "
                    "not exist"
                ) from None
            fingerprints[bin_name] = f"{st.st_size} {st.st_mtime_ns}"
        return binaries, None, fingerprints

    @staticmethod
    def _copy_all(
        rname: str, binaries: dict[str, str], cfg: ResolverConfig
    ) -> tuple[dict[str, str], dict[str, str]]:
        local: dict[str, str] = {}
        fingerprints: dict[str, str] = {}
        for bin_name, path in binaries.items():
            try:
                local[bin_name], fingerprints[bin_name] = materialize(
                    path, bin_root(), cfg.copy_keep_days
                )
            except ResolveError as e:
                raise ResolveError(f"resolver {rname!r}: {bin_name}: {e}") from None
            except OSError as e:
                raise ResolveError(
                    f"resolver {rname!r}: copying {bin_name}={path} failed: {e}"
                ) from None
        return local, fingerprints
