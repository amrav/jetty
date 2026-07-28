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

from absl.testing import absltest

import jetty

#: Directory the child process needs on PYTHONPATH to import jetty. Derived
#: from the package this test already imported rather than from the repository
#: layout, so it is right for a source checkout and an installed wheel alike.
IMPORT_ROOT = str(Path(jetty.__file__).resolve().parent.parent)

STARTUP_TIMEOUT_S = 20
SHUTDOWN_TIMEOUT_S = 10


def uds_get(sock: str, path: str) -> tuple[int, str]:
    """Minimal HTTP/1.1 GET over a unix socket, no client library needed."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(sock)
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


class ProcessTestCase(absltest.TestCase):
    """Runs jetty as a real process inside a temp dir owned by absltest.

    Every path here comes from `create_tempdir()`: the runner picks the temp
    root, so nothing may assume /tmp exists or is writable.

    Socket paths are then kept *relative* to that directory, and the process —
    both this one and the child — runs from inside it. AF_UNIX caps a socket
    path at 108 bytes, while an absl temp path already spends most of that
    budget on the test class and method name; a build environment that sets a
    deep TEST_TMPDIR (Bazel's is nested under the execroot) would push an
    absolute socket path over the limit and fail with a bewildering ENAMETOOLONG.
    Relative paths sidestep the limit entirely without giving up the managed
    temp dir.
    """

    def setUp(self):
        super().setUp()
        self.workdir = self.create_tempdir()
        # absltest creates temp dirs through os.makedirs, so their mode is
        # whatever the runner's umask allows (commonly 0775). jetty refuses to
        # bind a socket in a directory group or other can write to, which is
        # the behaviour under test — so make the directory private, rather than
        # depending on the umask of whoever runs the suite.
        os.chmod(self.workdir.full_path, 0o700)
        origin = os.getcwd()
        self.addCleanup(os.chdir, origin)
        os.chdir(self.workdir.full_path)
        # Make jetty importable in the child wherever it lives for us.
        existing = os.environ.get("PYTHONPATH")
        self.env = {
            **os.environ,
            "PYTHONPATH": (
                f"{IMPORT_ROOT}{os.pathsep}{existing}" if existing else IMPORT_ROOT
            ),
        }

    def write_config(self, sock: str, mode: int = 0o660) -> str:
        """Write jetty.toml into the temp dir; `sock` is relative to it."""
        return self.workdir.create_file(
            "jetty.toml",
            content=(
                f'[listener]\nuds = "{sock}"\nuds_mode = {mode:#o}\n\n'
                "[modules.reference]\nenabled = true\n"
            ),
        ).full_path

    def spawn(self, config: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "jetty.cli", "--config", config],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.env,
        )
        self.addCleanup(self.terminate, proc)
        return proc

    def terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        if proc.stdout:
            proc.stdout.close()

    def assert_still_running(self, proc: subprocess.Popen) -> None:
        """Fail with the child's output if it died. Never read a live pipe:
        `stdout.read()` blocks until EOF, which for a healthy server is never.
        """
        if proc.poll() is not None:
            output = proc.stdout.read().decode() if proc.stdout else ""
            self.fail(f"jetty exited early (code {proc.returncode}):\n{output}")

    def wait_for_path(self, proc: subprocess.Popen, path: str) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if os.path.exists(path):
                return
            self.assert_still_running(proc)
            time.sleep(0.1)
        self.fail(f"jetty did not create {path} within {STARTUP_TIMEOUT_S}s")

    def serve(self, sock: str = "jetty.sock", mode: int = 0o660) -> str:
        """Start jetty and return the socket path once it answers requests."""
        proc = self.spawn(self.write_config(sock, mode))
        self.wait_for_path(proc, sock)
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while True:
            try:
                uds_get(sock, "/healthz")
                return sock
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                self.assert_still_running(proc)
                time.sleep(0.1)


class UdsServerTest(ProcessTestCase):

    def setUp(self):
        super().setUp()
        self.sock = self.serve()

    def test_uds_socket_permissions(self):
        """SPEC.md §1.5: mode 0660 or tighter — no access for other users.

        Regression guard: uvicorn chmods UDS sockets to 0666 when given `uds=`,
        so jetty binds the socket itself and passes the fd.
        """
        mode = stat.S_IMODE(os.stat(self.sock).st_mode)
        self.assertEqual(0o660, mode, msg=f"socket mode is {mode:#o}, expected 0o660")
        self.assertFalse(mode & 0o007, msg="socket is accessible to other users")

    def test_serves_core_endpoints_over_uds(self):
        status, body = uds_get(self.sock, "/healthz")
        self.assertEqual(200, status)
        self.assertIn('"ok":true', body.replace(" ", ""))

        status, body = uds_get(self.sock, "/v1/meta")
        self.assertEqual(200, status)
        self.assertIn('"reference"', body)

    def test_disabled_module_404s_over_uds(self):
        status, body = uds_get(self.sock, "/auth/v1/identify")
        self.assertEqual(404, status)
        # The envelope, not Starlette's {"detail": "Not Found"} — this is how
        # the gap was originally spotted, over a live socket.
        self.assertIn('"error"', body)
        self.assertNotIn("detail", body)


class SocketDirectoryTest(ProcessTestCase):
    """The socket's directory is part of the access control (SPEC.md §1.5)."""

    def test_refuses_a_world_writable_socket_directory(self):
        """The default socket path is under /tmp, which is world-writable.

        A directory another user can write to lets them unlink our socket and
        bind their own at the same path, so clients would connect to theirs.
        Binding inside one must be refused rather than silently accepted.
        """
        sockdir = self.workdir.mkdir("jetty")
        os.chmod(sockdir.full_path, 0o777)  # mkdir's mode is masked by umask
        config = self.write_config("jetty/jetty.sock")

        proc = subprocess.run(
            [sys.executable, "-m", "jetty.cli", "--config", config],
            capture_output=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(
            2, proc.returncode, msg=proc.stdout.decode() + proc.stderr.decode()
        )
        self.assertIn(b"writable by group or other", proc.stderr)

    def test_creates_a_missing_socket_directory_privately(self):
        sock = "nested/jetty/jetty.sock"
        proc = self.spawn(self.write_config(sock))
        self.wait_for_path(proc, sock)
        self.assertEqual(0o700, stat.S_IMODE(os.stat(os.path.dirname(sock)).st_mode))


if __name__ == "__main__":
    absltest.main()
