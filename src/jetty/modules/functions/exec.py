"""The ``exec`` driver (functions-v1 §6.1): one child process per call.

Request payload in on stdin, response payload out on stdout, exit status
says whether it worked. This is the driver that makes "experiment quickly"
real: a function is prototyped as a program in any language and named in
config; a private driver replaces it later without the application
changing.

What is deliberately NOT here: any inspection of the payload (it is not
ours), any logging of stdout (it is the payload), and any fallback when a
child misbehaves (a non-zero exit is a 502, a timeout is a 503, never a
200 — SPEC.md §1.2).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from jetty.modules.functions.driver import FunctionFailed, UnknownFunction

log = logging.getLogger("jetty.functions.exec")

#: Stderr is the program author's debugging channel; it is relayed to the
#: log, but bounded, so a runaway child cannot flood it.
_STDERR_LOG_BYTES = 4096


#: functions-v1 §2, applied to config keys at boot.
_NAME = re.compile(r"^[a-z0-9_.-]{1,128}$")


class ExecFunctionSettings(BaseModel):
    """One `[modules.functions.exec.<name>]` table (functions-v1 §3)."""

    model_config = ConfigDict(extra="forbid")

    command: list[str]
    timeout_s: float = 30.0


class ExecSettings(BaseModel):
    """The whole `[modules.functions]` table as the exec driver reads it —
    a typo'd key must fail boot, same as core."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    driver: str = "exec"
    exec: dict[str, ExecFunctionSettings] = Field(default_factory=dict)


def build(settings: Mapping[str, Any]) -> "ExecFunctionDriver":
    """The registered factory for ``driver = "exec"``."""
    cfg = ExecSettings.model_validate(dict(settings))
    for fn_name in cfg.exec:
        if not _NAME.match(fn_name):
            raise ValueError(
                f"functions.exec: {fn_name!r} is not a valid function name "
                "(functions-v1 §2)"
            )
    return ExecFunctionDriver(
        [
            ExecFunction(name=fn_name, command=tuple(fn.command), timeout_s=fn.timeout_s)
            for fn_name, fn in cfg.exec.items()
        ]
    )


@dataclass(frozen=True)
class ExecFunction:
    name: str
    command: tuple[str, ...]
    timeout_s: float


def _resolve_executable(argv0: str) -> None:
    """functions-v1 §3: a missing executable aborts boot, not every call."""
    if os.sep in argv0:
        if not (os.path.isfile(argv0) and os.access(argv0, os.X_OK)):
            raise ValueError(f"command {argv0!r} is not an executable file")
    elif shutil.which(argv0) is None:
        raise ValueError(f"command {argv0!r} not found on PATH")


class ExecFunctionDriver:
    def __init__(self, functions: list[ExecFunction]) -> None:
        self._functions: dict[str, ExecFunction] = {}
        for fn in functions:
            if not fn.command:
                raise ValueError(f"function {fn.name!r}: command must not be empty")
            if fn.timeout_s <= 0:
                raise ValueError(f"function {fn.name!r}: timeout_s must be > 0")
            _resolve_executable(fn.command[0])
            self._functions[fn.name] = fn

    def functions(self) -> list[str]:
        return sorted(self._functions)

    async def call(self, name: str, payload: bytes) -> bytes:
        fn = self._functions.get(name)
        if fn is None:
            raise UnknownFunction(name)

        env = dict(os.environ, JETTY_FUNCTION=name)
        # An OSError here (ENOENT, EACCES, EAGAIN) propagates untyped: the
        # surface reads that as "could not be reached" → 503.
        proc = await asyncio.create_subprocess_exec(
            *fn.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload), timeout=fn.timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(
                f"function {name!r} did not finish within {fn.timeout_s}s"
            ) from None

        if stderr:
            # Relayed as the program's own output (functions-v1 §6.1); the
            # payload never goes through here, only stderr.
            level = logging.WARNING if proc.returncode != 0 else logging.DEBUG
            log.log(
                level,
                "function %s stderr: %s",
                name,
                stderr[:_STDERR_LOG_BYTES].decode("utf-8", "replace"),
            )
        if proc.returncode != 0:
            raise FunctionFailed(f"function {name!r} exited {proc.returncode}")
        return stdout
