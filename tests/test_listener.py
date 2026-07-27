"""End-to-end listener tests: a real process, a real socket, real requests.

These exist because the TestClient cannot catch transport-layer bugs. The
socket-permission test in particular is guarding against a live one: uvicorn's
own UDS path hardcodes `0o666` and chmods after binding, so a sidecar that
looked correct in every unit test was shipping a world-writable socket that
anything on the host could talk to.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _write_config(tmp_path: Path, sock: Path, mode: int = 0o660) -> Path:
    cfg = tmp_path / "jetty.toml"
    cfg.write_text(
        f'[listener]\nuds = "{sock}"\nuds_mode = {mode:#o}\n\n'
        "[modules.reference]\nenabled = true\n",
        encoding="utf-8",
    )
    return cfg


def _uds_get(sock: Path, path: str) -> tuple[int, str]:
    """Minimal HTTP/1.1 GET over a unix socket, no client library needed."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(sock))
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    raw = b"".join(chunks).decode()
    head, _, body = raw.partition("\r\n\r\n")
    status = int(head.split()[1])
    return status, body


@pytest.fixture
def server(tmp_path: Path):
    sock = tmp_path / "jetty.sock"
    cfg = _write_config(tmp_path, sock)
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "jetty.cli", "--config", str(cfg)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if sock.exists():
            try:
                _uds_get(sock, "/healthz")
                break
            except OSError:
                pass
        if proc.poll() is not None:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise AssertionError(f"jetty exited early:\n{out}")
        time.sleep(0.1)
    else:
        proc.kill()
        raise AssertionError("jetty did not start in time")
    yield sock
    proc.terminate()
    proc.wait(timeout=10)


def test_uds_socket_permissions(server: Path):
    """SPEC.md §1.5: mode 0660 or tighter — no access for other users.

    Regression guard: uvicorn chmods UDS sockets to 0666 when given `uds=`,
    so jetty binds the socket itself and passes the fd.
    """
    mode = stat.S_IMODE(server.stat().st_mode)
    assert mode == 0o660, f"socket mode is {mode:#o}, expected 0o660"
    assert not mode & 0o007, "socket is accessible to other users"


def test_serves_core_endpoints_over_uds(server: Path):
    status, body = _uds_get(server, "/healthz")
    assert status == 200
    assert '"ok":true' in body.replace(" ", "")

    status, body = _uds_get(server, "/v1/meta")
    assert status == 200
    assert '"reference"' in body


def test_disabled_module_404s_over_uds(server: Path):
    status, body = _uds_get(server, "/auth/v1/identify")
    assert status == 404
    # The envelope, not Starlette's {"detail": "Not Found"} — this is how the
    # gap was originally spotted, over a live socket.
    assert '"error"' in body
    assert "detail" not in body
