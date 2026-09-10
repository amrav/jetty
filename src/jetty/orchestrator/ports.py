"""Port broker.

Four spec forms per named port (parsed by `config.parse_port_spec`):

  "auto"       — bind :0 and let the kernel pick.
  8000         — exactly 8000; occupied means `up` refuses to start. We never
                 reclaim a port: that would mean killing a process we did not
                 start.
  "8000+"      — prefer 8000, scan upward for the first free port.
  "8000-8020"  — the same scan, bounded; exhausting the range is an error.

A port counts as free only when it binds on every loopback family the host
has — 127.0.0.1 and, where the box has an IPv6 loopback, ::1. Services pick
their family (uvicorn defaults to 127.0.0.1, node to `::`, many to
`localhost` and whatever that resolves to), and a listener on either
wildcard conflicts with a loopback bind in the same family, so probing both
is what makes "free" mean "bindable however the service chooses to bind".
A host without IPv6 loopback probes IPv4 only.

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

# Loopback address per family, in probe order. IPv4 goes first so that an
# "auto" allocation asks the IPv4 stack for a kernel-picked port and then
# confirms it on IPv6, rather than the other way round.
_LOOPBACKS: tuple[tuple[socket.AddressFamily, str], ...] = (
    (socket.AF_INET, "127.0.0.1"),
    (socket.AF_INET6, "::1"),
)

# How many kernel-picked ports an "auto" allocation may reject (free on
# IPv4, held on IPv6) before giving up. Each rejected candidate stays held
# for the batch so the kernel cannot hand it back, which makes the retry
# loop converge instead of spinning on one port.
_AUTO_ATTEMPTS = 64


class PortError(RuntimeError):
    pass


def _loopbacks() -> tuple[tuple[socket.AddressFamily, str], ...]:
    """The loopback families this host can bind, IPv4 first."""
    have = []
    for family, addr in _LOOPBACKS:
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:  # family not compiled in / disabled
            continue
        try:
            sock.bind((addr, 0))
        except OSError:  # e.g. ::1 not configured on the loopback interface
            continue
        else:
            have.append((family, addr))
        finally:
            sock.close()
    return tuple(have)


def _bind_one(family: socket.AddressFamily, addr: str, port: int) -> socket.socket | None:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        # Probe the IPv6 stack on its own. A dual-stack bind would reserve
        # the IPv4 side too, and the IPv4 probe already owns that answer.
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    try:
        sock.bind((addr, port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def _try_bind(
    port: int, loopbacks: tuple[tuple[socket.AddressFamily, str], ...]
) -> list[socket.socket] | None:
    """Hold `port` on every loopback family, or on none of them."""
    held: list[socket.socket] = []
    for family, addr in loopbacks:
        sock = _bind_one(family, addr, port)
        if sock is None:
            for s in held:
                s.close()
            return None
        held.append(sock)
    return held


def _bind_auto(
    name: str,
    loopbacks: tuple[tuple[socket.AddressFamily, str], ...],
    holds: list[socket.socket],
) -> int:
    """Kernel-picked port that is free on every loopback family."""
    first_family, first_addr = loopbacks[0]
    for _ in range(_AUTO_ATTEMPTS):
        lead = _bind_one(first_family, first_addr, 0)
        if lead is None:  # ephemeral range exhausted on the lead family
            break
        holds.append(lead)
        port = lead.getsockname()[1]
        rest = _try_bind(port, loopbacks[1:])
        if rest is not None:
            holds.extend(rest)
            return port
        # `lead` stays in `holds`: the kernel must not pick this port again
        # in this batch.
    raise PortError(f'ports.{name}: no port free on every loopback family for "auto"')


def allocate_ports(spec: dict[str, int | str]) -> dict[str, int]:
    allocated: dict[str, int] = {}
    holds: list[socket.socket] = []
    fixed: dict[int, str] = {}
    loopbacks = _loopbacks()
    if not loopbacks:
        raise PortError("no loopback address to probe ports on")
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
                allocated[name] = _bind_auto(name, loopbacks, holds)
                continue
            low, high = parsed
            for candidate in range(low, high + 1):
                socks = _try_bind(candidate, loopbacks)
                if socks is not None:
                    holds.extend(socks)
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
