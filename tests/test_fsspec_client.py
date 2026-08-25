"""The jetty.fsspec client against a live sidecar on a real unix socket.

The client (the `fsspec` extra) never touches module code in-process — it
speaks the published wire contract — so these tests are the integration
point: real HTTP over a real UDS against the real filesystem module,
uvicorn serving in a background thread. Assertions cover the fsspec surface (cat/pipe/open/mv/cp/rm,
exists/info) and the error mapping onto Python's exception vocabulary.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime, timezone

from absl.testing import absltest

try:
    import fsspec
    from jetty.fsspec import JettyFileSystem
    _HAVE_FSSPEC = True
except ImportError:
    _HAVE_FSSPEC = False

import uvicorn

from jetty.config import Config
from jetty.server import create_app

_NONROOT = os.geteuid() != 0


@absltest.skipUnless(_HAVE_FSSPEC, "fsspec is not installed")
class JettyFsspecTest(absltest.TestCase):
    """One live sidecar per test: uvicorn on a UDS under create_tempdir.

    The socket path is *relative*, with the test chdir'd into the tempdir —
    the same dodge test_listener.py uses: AF_UNIX caps a socket path at 108
    bytes, and an absl temp path plus a long test-method name exceeds it.
    The relative path also means every test shares the literal option string
    "jetty.sock", so the fs instances opt out of fsspec's instance cache
    (skip_instance_cache) — a cached instance would point at a previous
    test's dead socket.
    """

    def setUp(self):
        super().setUp()
        base = self.create_tempdir().full_path
        cwd = os.getcwd()
        os.chdir(base)
        self.addCleanup(os.chdir, cwd)
        self.root = os.path.join(base, "root")
        os.makedirs(self.root)
        self.sock = "jetty.sock"
        cfg = Config.model_validate(
            {
                "listener": {"uds": self.sock},
                "modules": {"filesystem": {"enabled": True, "root": self.root}},
            }
        )
        server = uvicorn.Server(
            uvicorn.Config(create_app(cfg), uds=self.sock, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(self._stop, server, thread)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.exists(self.sock):
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.connect(self.sock)
                    break
                except OSError:
                    pass
                finally:
                    probe.close()
            time.sleep(0.02)
        else:
            self.fail("sidecar did not come up on its socket")
        self.fs = JettyFileSystem(uds=self.sock, skip_instance_cache=True)

    @staticmethod
    def _stop(server, thread):
        server.should_exit = True
        thread.join(timeout=5)

    def disk(self, rel: str) -> bytes:
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()

    # --- the fsspec surface --------------------------------------------

    def test_pipe_and_cat_roundtrip(self):
        self.fs.pipe_file("notes.txt", b"hello over the socket")
        self.assertEqual(self.fs.cat_file("notes.txt"), b"hello over the socket")
        self.assertEqual(self.disk("notes.txt"), b"hello over the socket")

    def test_cat_file_range_is_sliced_locally(self):
        self.fs.pipe_file("r.txt", b"0123456789")
        self.assertEqual(self.fs.cat_file("r.txt", start=2, end=5), b"234")
        self.assertEqual(self.fs.cat_file("r.txt", start=-3), b"789")

    def test_open_text_write_then_read(self):
        with self.fs.open("greeting.txt", "w") as f:
            f.write("hello, text mode\n")
        with self.fs.open("greeting.txt", "r") as f:
            self.assertEqual(f.read(), "hello, text mode\n")

    def test_open_append(self):
        self.fs.pipe_file("log.txt", b"one\n")
        with self.fs.open("log.txt", "ab") as f:
            f.write(b"two\n")
        self.assertEqual(self.fs.cat_file("log.txt"), b"one\ntwo\n")

    def test_open_exclusive_raises_when_present(self):
        self.fs.pipe_file("taken.txt", b"x")
        with self.assertRaises(FileExistsError):
            self.fs.open("taken.txt", "xb")

    def test_mv_is_server_side_rename(self):
        self.fs.pipe_file("a.txt", b"cargo")
        self.fs.mv("a.txt", "b.txt")
        self.assertFalse(self.fs.exists("a.txt"))
        self.assertEqual(self.fs.cat_file("b.txt"), b"cargo")

    def test_cp_file(self):
        self.fs.pipe_file("orig.txt", b"twin")
        self.fs.cp_file("orig.txt", "copy.txt")
        self.assertEqual(self.fs.cat_file("orig.txt"), b"twin")
        self.assertEqual(self.fs.cat_file("copy.txt"), b"twin")

    def test_rm_file(self):
        self.fs.pipe_file("doomed.txt", b"x")
        self.fs.rm_file("doomed.txt")
        self.assertFalse(os.path.exists(os.path.join(self.root, "doomed.txt")))

    def test_exists(self):
        self.assertFalse(self.fs.exists("ghost.txt"))
        self.fs.pipe_file("real.txt", b"x")
        self.assertTrue(self.fs.exists("real.txt"))

    def test_info_reports_size_without_downloading(self):
        self.fs.pipe_file("sized.bin", b"\x00" * 1234)
        info = self.fs.info("sized.bin")
        self.assertEqual(info["size"], 1234)
        self.assertEqual(info["type"], "file")

    def test_ls_is_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.fs.ls("")

    def test_gettmpdir_scratch_lifecycle(self):
        d = self.fs.gettmpdir()
        # The path is opaque (where scratch space lives is the sidecar's
        # choice); all that matters is that it works as a path.
        self.assertTrue(d)
        self.assertFalse(d.startswith("/"), d)
        self.fs.pipe_file(f"{d}/scratch.txt", b"work")
        self.assertEqual(self.fs.cat_file(f"{d}/scratch.txt"), b"work")
        self.fs.rm_file(f"{d}/scratch.txt")
        self.fs.rm_file(d)  # empty now: rmdir(2) on the sidecar
        self.assertFalse(os.path.exists(os.path.join(self.root, d)))

    def test_gettmpdir_is_fresh_each_call(self):
        self.assertNotEqual(self.fs.gettmpdir(), self.fs.gettmpdir())

    def test_info_carries_stat_fields(self):
        self.fs.pipe_file("meta.bin", b"\x00" * 64)
        os.chmod(os.path.join(self.root, "meta.bin"), 0o640)
        info = self.fs.info("meta.bin")
        self.assertEqual(info["size"], 64)
        self.assertEqual(info["type"], "file")
        self.assertEqual(info["mode"], "0640")

    def test_info_and_exists_answer_for_directories(self):
        d = self.fs.gettmpdir()
        self.assertTrue(self.fs.exists(d))
        self.assertEqual(self.fs.info(d)["type"], "directory")

    def test_modified(self):
        self.fs.pipe_file("m.txt", b"x")
        delta = datetime.now(timezone.utc) - self.fs.modified("m.txt")
        self.assertLess(abs(delta.total_seconds()), 10)

    # --- error mapping --------------------------------------------------

    def test_missing_file_is_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.fs.cat_file("ghost.txt")
        with self.assertRaises(FileNotFoundError):
            self.fs.rm_file("ghost.txt")
        with self.assertRaises(FileNotFoundError):
            self.fs.info("ghost.txt")

    def test_write_into_missing_directory_is_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.fs.pipe_file("no/such/dir/f.txt", b"x")

    def test_traversal_is_value_error(self):
        with self.assertRaises(ValueError):
            self.fs.cat_file("../outside.txt")

    @absltest.skipUnless(_NONROOT, "permission bits do not bind root")
    def test_permission_denied_is_permission_error(self):
        os.chmod(self.root, 0o555)
        self.addCleanup(os.chmod, self.root, 0o700)
        with self.assertRaises(PermissionError):
            self.fs.pipe_file("new.txt", b"x")

    # --- fsspec integration --------------------------------------------

    def test_registered_with_fsspec(self):
        fs = fsspec.filesystem("jetty", uds=self.sock, skip_instance_cache=True)
        self.assertIsInstance(fs, JettyFileSystem)

    def test_url_open_through_fsspec(self):
        opts = {"uds": self.sock, "skip_instance_cache": True}
        with fsspec.open("jetty://url.txt", "wb", **opts) as f:
            f.write(b"by url")
        self.assertEqual(self.disk("url.txt"), b"by url")
        with fsspec.open("jetty://url.txt", "rb", **opts) as f:
            self.assertEqual(f.read(), b"by url")


if __name__ == "__main__":
    absltest.main()
