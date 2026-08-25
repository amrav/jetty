"""The filesystem module against a real directory tree (spec/filesystem-v1.md).

Assertions are black-box through the HTTP surface: unix permission semantics,
inode-preserving replacement, symlink containment, and the SPEC.md §3.1
envelope with the module's own code.
"""

from __future__ import annotations

import os
import stat

from absl.testing import absltest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.server import create_app

_NONROOT = os.geteuid() != 0  # permission-bit tests are meaningless as root


class FilesystemTestCase(absltest.TestCase):

    def setUp(self):
        super().setUp()
        base = self.create_tempdir().full_path
        self.socket_path = os.path.join(base, "jetty.sock")
        self.root = os.path.join(base, "root")
        os.makedirs(self.root)

    def build(self, **settings) -> TestClient:
        merged = {"enabled": True, "root": self.root, **settings}
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": {"filesystem": merged}}
        )
        return TestClient(create_app(cfg))

    def seed(self, rel: str, content: bytes = b"seed") -> str:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def read(self, client, rel: str):
        return client.get(f"/filesystem/v1/files/{rel}")

    def write(self, client, rel: str, content: bytes):
        return client.put(f"/filesystem/v1/files/{rel}", content=content)

    def assert_error(self, response, status, code, retryable=False):
        """The SPEC.md §3.1 envelope, including the module's own codes."""
        self.assertEqual(response.status_code, status, response.text)
        err = response.json()["error"]
        self.assertEqual(err["code"], code)
        self.assertEqual(err["retryable"], retryable)
        self.assertIsInstance(err["message"], str)


class ModuleLifecycleTest(FilesystemTestCase):

    def test_disabled_by_default_is_module_disabled(self):
        cfg = Config.model_validate({"listener": {"uds": self.socket_path}})
        client = TestClient(create_app(cfg))
        r = client.get("/filesystem/v1/files/a.txt")
        self.assert_error(r, 404, "module_disabled")

    def test_meta_lists_the_module(self):
        r = self.build().get("/v1/meta")
        self.assertIn(
            {"name": "filesystem", "api_version": "v1", "mount": "/filesystem"},
            r.json()["modules"],
        )

    def test_unavailable_driver_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "nfs"):
            self.build(driver="nfs")

    def test_missing_root_key_fails_boot(self):
        with self.assertRaisesRegex(Exception, "root"):
            cfg = Config.model_validate(
                {
                    "listener": {"uds": self.socket_path},
                    "modules": {"filesystem": {"enabled": True}},
                }
            )
            create_app(cfg)

    def test_unknown_config_key_fails_boot(self):
        with self.assertRaises(Exception):
            self.build(chmod=True)

    def test_missing_root_directory_aborts_startup(self):
        client = self.build(root=os.path.join(self.root, "nope"))
        with self.assertRaisesRegex(Exception, "not a directory"):
            with client:
                pass


class ReadTest(FilesystemTestCase):

    def test_reads_entire_file(self):
        self.seed("notes.txt", b"hello\nworld\n")
        r = self.read(self.build(), "notes.txt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, b"hello\nworld\n")
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))

    def test_binary_content_and_octet_stream_fallback(self):
        blob = bytes(range(256)) + b"\x00\xff" * 64
        self.seed("data.bin", blob)
        r = self.read(self.build(), "data.bin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, blob)
        self.assertEqual(r.headers["content-type"], "application/octet-stream")

    def test_empty_file_is_200_empty_body(self):
        self.seed("empty", b"")
        r = self.read(self.build(), "empty")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"")

    def test_nested_path(self):
        self.seed("team/a/b.txt", b"nested")
        r = self.read(self.build(), "team/a/b.txt")
        self.assertEqual(r.content, b"nested")

    def test_missing_file_is_not_found(self):
        self.assert_error(self.read(self.build(), "ghost.txt"), 404, "not_found")

    def test_missing_intermediate_directory_is_not_found(self):
        self.assert_error(self.read(self.build(), "no/dir/f.txt"), 404, "not_found")

    def test_directory_is_invalid_request(self):
        os.makedirs(os.path.join(self.root, "adir"))
        self.assert_error(self.read(self.build(), "adir"), 400, "invalid_request")

    def test_fifo_is_refused_not_hung(self):
        os.mkfifo(os.path.join(self.root, "pipe"))
        self.assert_error(self.read(self.build(), "pipe"), 400, "invalid_request")


class WriteTest(FilesystemTestCase):

    def test_creates_a_file(self):
        r = self.write(self.build(), "new.txt", b"fresh")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"size": 5, "created": True})
        with open(os.path.join(self.root, "new.txt"), "rb") as f:
            self.assertEqual(f.read(), b"fresh")

    def test_replaces_preserving_inode_and_mode(self):
        path = self.seed("cfg.ini", b"old contents, longer than the new")
        os.chmod(path, 0o604)
        before = os.stat(path)
        r = self.write(self.build(), "cfg.ini", b"new")
        self.assertEqual(r.json(), {"size": 3, "created": False})
        after = os.stat(path)
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o604)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_empty_body_writes_empty_file(self):
        r = self.write(self.build(), "zero", b"")
        self.assertEqual(r.json(), {"size": 0, "created": True})
        self.assertEqual(os.path.getsize(os.path.join(self.root, "zero")), 0)

    def test_round_trip_binary(self):
        client = self.build()
        blob = os.urandom(4096)
        self.write(client, "blob.bin", blob)
        self.assertEqual(self.read(client, "blob.bin").content, blob)

    def test_parents_are_not_created(self):
        r = self.write(self.build(), "no/such/dir/f.txt", b"x")
        self.assert_error(r, 404, "not_found")
        self.assertFalse(os.path.exists(os.path.join(self.root, "no")))

    def test_directory_target_is_invalid_request(self):
        os.makedirs(os.path.join(self.root, "adir"))
        self.assert_error(self.write(self.build(), "adir", b"x"), 400, "invalid_request")


class PermissionTest(FilesystemTestCase):
    """filesystem-v1 §2: the kernel's refusal surfaces as 403, never masked."""

    @absltest.skipUnless(_NONROOT, "permission bits do not bind root")
    def test_unreadable_file_is_permission_denied(self):
        path = self.seed("secret", b"clearance required")
        os.chmod(path, 0o000)
        self.assert_error(self.read(self.build(), "secret"), 403, "permission_denied")

    @absltest.skipUnless(_NONROOT, "permission bits do not bind root")
    def test_unwritable_file_is_permission_denied(self):
        path = self.seed("readonly.txt", b"look, don't touch")
        os.chmod(path, 0o444)
        r = self.write(self.build(), "readonly.txt", b"vandalism")
        self.assert_error(r, 403, "permission_denied")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"look, don't touch")

    @absltest.skipUnless(_NONROOT, "permission bits do not bind root")
    def test_create_in_unwritable_directory_is_permission_denied(self):
        locked = os.path.join(self.root, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o500)
        r = self.write(self.build(), "locked/new", b"x")
        self.assert_error(r, 403, "permission_denied")


class ContainmentTest(FilesystemTestCase):
    """filesystem-v1 §3: the root is the module's entire authority."""

    def setUp(self):
        super().setUp()
        # A juicy target outside the root that no request may ever reach.
        self.outside = os.path.join(os.path.dirname(self.root), "outside.txt")
        with open(self.outside, "w") as f:
            f.write("out of bounds")

    def test_dotdot_is_invalid_request(self):
        # %2e%2e defeats client-side URL normalization; the server must still
        # refuse what arrives.
        r = self.build().get("/filesystem/v1/files/%2e%2e/outside.txt")
        self.assert_error(r, 400, "invalid_request")

    def test_absolute_path_is_invalid_request(self):
        # Encoded so the leading slash survives into the path parameter.
        r = self.build().get("/filesystem/v1/files/%2Fetc%2Fhosts")
        self.assert_error(r, 400, "invalid_request")

    def test_backslash_is_invalid_request(self):
        r = self.build().get("/filesystem/v1/files/a%5Cb")
        self.assert_error(r, 400, "invalid_request")

    def test_dot_segment_is_invalid_request(self):
        r = self.build().get("/filesystem/v1/files/a/%2e/b")
        self.assert_error(r, 400, "invalid_request")

    def test_escaping_symlink_is_invalid_request(self):
        os.symlink(self.outside, os.path.join(self.root, "door"))
        self.assert_error(self.read(self.build(), "door"), 400, "invalid_request")

    def test_escaping_symlink_write_is_invalid_request(self):
        os.symlink(self.outside, os.path.join(self.root, "door"))
        r = self.write(self.build(), "door", b"graffiti")
        self.assert_error(r, 400, "invalid_request")
        with open(self.outside) as f:
            self.assertEqual(f.read(), "out of bounds")

    def test_escaping_symlinked_directory_is_invalid_request(self):
        os.symlink(os.path.dirname(self.outside), os.path.join(self.root, "updir"))
        r = self.read(self.build(), "updir/outside.txt")
        self.assert_error(r, 400, "invalid_request")

    def test_internal_symlink_is_followed(self):
        self.seed("real.txt", b"the real thing")
        os.symlink(
            os.path.join(self.root, "real.txt"), os.path.join(self.root, "alias.txt")
        )
        r = self.read(self.build(), "alias.txt")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"the real thing")

    def test_symlink_loop_is_invalid_request(self):
        os.symlink("loop", os.path.join(self.root, "loop"))
        self.assert_error(self.read(self.build(), "loop"), 400, "invalid_request")


if __name__ == "__main__":
    absltest.main()
