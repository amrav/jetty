"""Port broker.

Four spec forms per named port (parsed by `config.parse_port_spec`):

  "auto"       — bind :0 and let the kernel pick.
  8000         — exactly 8000; occupied means `up` refuses to start. We never
                 reclaim a port: that would mean killing a process we did not
                 start.
  "8000+"      — prefer 8000, scan upward for the first free port.
  "8000-8020"  — the same scan, bounded; exhausting the range is an error.

Probe sockets are held open (and listening, so a later probe in the same
batch cannot double-allocate a port) until the whole batch is done. There is
still a window between releasing a probe and the service binding — on a
loopback dev box that race is vanishingly rare, and losing it surfaces as a
service bind failure handled by the restart policy. Probes set SO_REUSEADDR
so a port in TIME_WAIT counts as free, matching what the service's own bind
will conclude.
"""

from __future__ import annotations

import socket

from .config import parse_port_spec


class PortError(RuntimeError):
    pass


def _try_bind(port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def allocate_ports(spec: dict[str, int | str]) -> dict[str, int]:
    allocated: dict[str, int] = {}
    holds: list[socket.socket] = []
    fixed: dict[int, str] = {}
    try:
        for name, want in spec.items():
            parsed = parse_port_spec(want)
            # Config validation catches literal duplicates, but env-rendered
            # specs can only collide HERE — and the bind failure they'd
            # produce reads as "someone else holds this port" when the
            # someone is our own probe socket. Name the real problem.
            if parsed != "auto" and parsed[0] == parsed[1]:
                if parsed[0] in fixed:
                    raise PortError(
                        f"ports.{name} and ports.{fixed[parsed[0]]} both "
                        f"resolve to fixed port {parsed[0]}"
                    )
                fixed[parsed[0]] = name
            if parsed == "auto":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                holds.append(sock)
                sock.bind(("127.0.0.1", 0))
                sock.listen(1)
                allocated[name] = sock.getsockname()[1]
                continue
            low, high = parsed
            for candidate in range(low, high + 1):
                sock = _try_bind(candidate)
                if sock is not None:
                    holds.append(sock)
                    allocated[name] = candidate
                    break
            else:
                if low == high:
                    raise PortError(
                        f"ports.{name}: port {low} is already in use; refusing "
                        'to reclaim it — stop whatever holds it, or use "auto" '
                        f'or "{low}+"'
                    )
                raise PortError(
                    f"ports.{name}: no free port in {low}-{high}"
                )
    finally:
        for sock in holds:
            sock.close()
    return allocated
