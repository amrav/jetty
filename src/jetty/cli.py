"""`jetty` entrypoint.

Exit codes follow the convention that a supervisor can act on:

  0  clean shutdown
  2  configuration error — a bug in the deployment, NOT restartable. A
     supervisor that retries a bad config just burns CPU; make it loud once.
"""

from __future__ import annotations

import argparse
import os
import socket
import stat
import sys
from pathlib import Path

import uvicorn

from jetty import __version__
from jetty.config import Config
from jetty.modules.registry import UnknownModuleError, known_modules
from jetty.server import create_app


def _fail(message: str) -> None:
    print(f"jetty: {message}", file=sys.stderr)
    raise SystemExit(2)


def bind_uds(path: Path, mode: int) -> socket.socket:
    """Bind a unix socket at `path` with exactly `mode` (SPEC.md §1.5).

    The socket is never even briefly world-accessible: umask constrains the
    mode at creation, and the explicit chmod afterwards pins it regardless of
    what the inherited umask happened to be.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # A stale socket file from an unclean exit makes bind() fail with
    # EADDRINUSE even though nothing is listening.
    if path.exists() and stat.S_ISSOCK(path.stat().st_mode):
        path.unlink()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o777 & ~mode)
    try:
        sock.bind(str(path))
    finally:
        os.umask(previous)
    os.chmod(path, mode)
    sock.listen(128)
    return sock


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jetty", description=__doc__)
    parser.add_argument("--config", "-c", required=True, help="path to jetty.toml")
    parser.add_argument("--version", action="version", version=f"jetty {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config and exit without binding a listener",
    )
    args = parser.parse_args(argv)

    path = Path(args.config)
    if not path.is_file():
        _fail(f"config file not found: {path}")

    try:
        config = Config.load(path)
    except Exception as e:  # noqa: BLE001 - surface any config problem as exit 2
        _fail(f"invalid config: {e}")

    try:
        app = create_app(config)
    except UnknownModuleError as e:
        _fail(f"{e}")
    except Exception as e:  # noqa: BLE001
        _fail(f"failed to build application: {e}")

    enabled = [m.name for m in app.state.jetty.modules]
    if not enabled:
        # Not fatal: a sidecar with no modules still serves /healthz and
        # /v1/meta, which is a legitimate way to stage a rollout. But it is
        # almost always a mistake, so say so.
        print(
            f"jetty: warning: no modules enabled (known: {', '.join(known_modules())})",
            file=sys.stderr,
        )

    if args.check:
        print(f"jetty: config OK; modules enabled: {', '.join(enabled) or '(none)'}")
        return

    listener = config.listener
    if listener.uds:
        sock = bind_uds(Path(listener.uds), listener.uds_mode)
        try:
            # Hand uvicorn the already-bound fd rather than `uds=`. uvicorn's
            # own UDS path hardcodes `uds_perms = 0o666` and chmods AFTER
            # binding (uvicorn/config.py bind_socket), which would silently
            # widen the socket to world-writable and break SPEC.md §1.5. This
            # was caught by inspecting a running socket, not by a unit test —
            # hence test_uds_socket_permissions.
            uvicorn.run(
                app, fd=sock.fileno(), log_level=config.log.level, access_log=False
            )
        finally:
            sock.close()
            Path(listener.uds).unlink(missing_ok=True)
    else:
        host, _, port = listener.tcp.rpartition(":")
        uvicorn.run(
            app,
            host=host.strip("[]") or "127.0.0.1",
            port=int(port),
            log_level=config.log.level,
            access_log=False,
        )


if __name__ == "__main__":
    main()
