"""Multiplexed console output: `[service]`-prefixed lines, one colour per
service — the `concurrently`/foreman idiom, so interleaved output from a
whole instance stays readable.

Colour only when writing to a TTY and NO_COLOR is unset; logs piped to a
file or captured by a test see plain prefixes.
"""

from __future__ import annotations

import os

RESET = "\x1b[0m"
#: Distinguishable on both dark and light terminals; cycles if an instance
#: somehow has more services than this.
_COLORS = [
    "\x1b[36m",  # cyan
    "\x1b[33m",  # yellow
    "\x1b[32m",  # green
    "\x1b[35m",  # magenta
    "\x1b[34m",  # blue
    "\x1b[91m",  # bright red
    "\x1b[96m",  # bright cyan
    "\x1b[93m",  # bright yellow
    "\x1b[92m",  # bright green
    "\x1b[95m",  # bright magenta
]


def want_color(stream) -> bool:
    return stream.isatty() and not os.environ.get("NO_COLOR")


class Prefixer:
    """`[api     ] the line` — names padded to a common width so columns of
    output line up, coloured by the service's stable position in the config."""

    def __init__(self, names: list[str], color: bool):
        width = max((len(n) for n in names), default=0)
        self._prefix = {}
        for i, name in enumerate(names):
            label = f"[{name.ljust(width)}]"
            if color:
                label = f"{_COLORS[i % len(_COLORS)]}{label}{RESET}"
            self._prefix[name] = label

    def label(self, name: str) -> str:
        return self._prefix.get(name, f"[{name}]")

    def format(self, name: str, line: str) -> str:
        return f"{self.label(name)} {line}"


class LineBuffer:
    """Reassemble stream chunks into complete lines; a partial line is held
    until its newline arrives so two services' output never interleaves
    mid-line."""

    def __init__(self) -> None:
        self._partial = b""

    def feed(self, chunk: bytes) -> list[str]:
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        return [line.decode(errors="replace").rstrip("\r") for line in lines]

    def flush(self) -> str | None:
        if not self._partial:
            return None
        line, self._partial = self._partial, b""
        return line.decode(errors="replace")
