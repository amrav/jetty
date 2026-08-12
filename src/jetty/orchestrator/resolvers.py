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
import os
import time

from .config import ResolverConfig

_OUTPUT_LIMIT = 1 << 20  # a resolver that prints a megabyte is broken
_STDERR_TAIL = 2000


class ResolveError(RuntimeError):
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
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}
        #: For the registry / `status`: last successful resolution per resolver.
        self.state: dict[str, dict] = {}

    async def context_for(self, bin_names: set[str]) -> dict[str, str]:
        """`{bin.<name>}` context entries for one spawn. Raises ResolveError."""
        ctx: dict[str, str] = {}
        for rname in sorted({self._by_bin[b] for b in bin_names}):
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
            binaries = await self._run(rname, cfg)
            self._cache[rname] = (time.monotonic(), binaries)
            self.state[rname] = {"binaries": binaries, "resolved_at": time.time()}
            return binaries

    async def _run(self, rname: str, cfg: ResolverConfig) -> dict[str, str]:
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
            if not os.path.exists(path):
                raise ResolveError(
                    f"resolver {rname!r} returned {bin_name}={path}, which does "
                    "not exist"
                )
        return binaries
