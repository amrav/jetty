"""Gates: external conditions checked by running a command.

Exit 0 = satisfied. Failure to even run the check (missing binary, timeout)
counts as unsatisfied — fail closed, same argument as jetty core's boot-time
probes: a gate that cannot be evaluated must not silently pass.

Results are cached per gate for `recheck_seconds` so several blocked services
polling the same credential probe don't stampede it.
"""

from __future__ import annotations

import asyncio
import time

from .config import GateConfig


class GateSet:
    def __init__(
        self,
        gates: dict[str, tuple[GateConfig, list[str]]],
        cwd: str | None = None,
    ):
        self._gates = gates
        #: Checks run from the config file's directory, like everything else
        #: a config describes — a `test -f flag` means the same flag wherever
        #: the supervisor was launched from.
        self._cwd = cwd
        self._cache: dict[str, tuple[float, bool]] = {}
        self._locks = {name: asyncio.Lock() for name in gates}

    async def satisfied(
        self, names: list[str], refresh: bool = False
    ) -> tuple[bool, list[str]]:
        """(all satisfied?, names of failing gates)."""
        failing = [n for n in names if not await self._check(n, refresh)]
        return (not failing, failing)

    def min_recheck(self, names: list[str]) -> float:
        return min(self._gates[n][0].recheck_seconds for n in names)

    def continuous(self, names: list[str]) -> list[str]:
        """The subset of `names` marked `continuous = true`."""
        return [n for n in names if self._gates[n][0].continuous]

    def close_after(self, name: str) -> int:
        return self._gates[name][0].close_after

    async def _check(self, name: str, refresh: bool) -> bool:
        cfg, argv = self._gates[name]
        async with self._locks[name]:
            cached = self._cache.get(name)
            if (
                cached is not None
                and not refresh
                and time.monotonic() - cached[0] < cfg.recheck_seconds
            ):
                return cached[1]
            ok = await self._run(argv, cfg.timeout_seconds)
            self._cache[name] = (time.monotonic(), ok)
            return ok

    async def _run(self, argv: list[str], timeout: float) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return False
        try:
            return await asyncio.wait_for(proc.wait(), timeout) == 0
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False
