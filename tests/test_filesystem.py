"""The filesystem module against a real directory tree (spec/filesystem-v1.md).

Assertions are black-box through the HTTP surface: unix permission semantics
(directory bits govern mutation), rename(2)-atomic replacement, delete /
rename / copy, symlink containment, and the SPEC.md §3.1 envelope with the
module's own code.
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

    def delete(self, client, rel: str):
        return client.delete(f"/filesystem/v1/files/{rel}")

    def rename(self, client, src: str, dst: str):
        return client.post("/filesystem/v1/rename", json={"from": src, "to": dst})

    def copy(self, client, src: str, dst: str):
        return client.post("/filesystem/v1/copy", json={"from": src, "to": dst})

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

    def test_head_reports_size_without_body(self):
        self.seed("notes.txt", b"hello\nworld\n")
        r = self.build().head("/filesystem/v1/files/notes.txt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["content-length"], "12")
        self.assertEqual(r.content, b"")

    def test_head_missing_file_is_not_found(self):
        r = self.build().head("/filesystem/v1/files/ghost.txt")
        self.assertEqual(r.status_code, 404)

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

    def test_replace_is_atomic_new_inode_same_mode(self):
        path = self.seed("cfg.ini", b"old contents, longer than the new")
        os.chmod(path, 0o604)
        before = os.stat(path)
        r = self.write(self.build(), "cfg.ini", b"new")
        self.assertEqual(r.json(), {"size": 3, "created": False})
        after = os.stat(path)
        # A write lands by rename(2): a fresh inode carrying the replaced
        # file's permission bits (filesystem-v1 §2).
        self.assertNotEqual(after.st_ino, before.st_ino)
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
    def test_read_only_file_in_writable_directory_is_replaced(self):
        # Atomic replacement is a rename: the directory's permissions govern,
        # exactly as mv(1) — the file's own bits do not protect it
        # (filesystem-v1 §2).
        path = self.seed("readonly.txt", b"look, don't touch")
        os.chmod(path, 0o444)
        r = self.write(self.build(), "readonly.txt", b"replaced anyway")
        self.assertEqual(r.status_code, 200, r.text)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"replaced anyway")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o444)

    @absltest.skipUnless(_NONROOT, "permission bits do not bind root")
    def test_replace_in_unwritable_directory_is_permission_denied(self):
        path = self.seed("locked/notes.txt", b"original")
        locked = os.path.dirname(path)
        os.chmod(locked, 0o555)
        self.addCleanup(os.chmod, locked, 0o700)
        r = self.write(self.build(), "locked/notes.txt", b"vandalism")
        self.assert_error(r, 403, "permission_denied")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"original")

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


class AtomicityTest(FilesystemTestCase):
    """filesystem-v1 §2: writes land by same-directory temp + rename(2)."""

    def test_new_file_honors_umask(self):
        self.addCleanup(os.umask, os.umask(0o027))
        self.write(self.build(), "fresh.txt", b"x")
        mode = stat.S_IMODE(os.stat(os.path.join(self.root, "fresh.txt")).st_mode)
        self.assertEqual(mode, 0o640)

    def test_no_temporary_residue(self):
        client = self.build()
        self.write(client, "a.txt", b"one")
        self.write(client, "a.txt", b"two")
        self.assertEqual(os.listdir(self.root), ["a.txt"])

    def test_hard_links_detach(self):
        path = self.seed("a.txt", b"shared")
        os.link(path, os.path.join(self.root, "b.txt"))
        self.write(self.build(), "a.txt", b"solo")
        with open(os.path.join(self.root, "b.txt"), "rb") as f:
            self.assertEqual(f.read(), b"shared")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"solo")

    def test_write_through_symlink_updates_target_keeps_link(self):
        self.seed("real.txt", b"v1")
        alias = os.path.join(self.root, "alias.txt")
        os.symlink(os.path.join(self.root, "real.txt"), alias)
        r = self.write(self.build(), "alias.txt", b"v2")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(os.path.islink(alias))
        with open(os.path.join(self.root, "real.txt"), "rb") as f:
            self.assertEqual(f.read(), b"v2")


class DeleteTest(FilesystemTestCase):

    def test_deletes_a_file(self):
        self.seed("doomed.txt")
        r = self.delete(self.build(), "doomed.txt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"deleted": True})
        self.assertFalse(os.path.exists(os.path.join(self.root, "doomed.txt")))

    def test_missing_file_is_not_found(self):
        self.assert_error(self.delete(self.build(), "ghost"), 404, "not_found")

    def test_empty_directory_is_deleted(self):
        # The cleanup half of tmpdir (filesystem-v1 §5.3): rmdir(2).
        os.makedirs(os.path.join(self.root, "adir"))
        r = self.delete(self.build(), "adir")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(os.path.exists(os.path.join(self.root, "adir")))

    def test_non_empty_directory_is_invalid_request(self):
        self.seed("adir/occupant.txt", b"x")
        self.assert_error(self.delete(self.build(), "adir"), 400, "invalid_request")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "adir")))

    def test_traversal_is_invalid_request(self):
        r = self.build().delete("/filesystem/v1/files/%2e%2e/x")
        self.assert_error(r, 400, "invalid_request")

    def test_through_symlink_deletes_target_not_link(self):
        # Symlinks are transparent in this namespace (filesystem-v1 §2): the
        # resolved target goes; the link itself stays, now dangling.
        path = self.seed("real.txt", b"x")
        os.symlink(path, os.path.join(self.root, "alias.txt"))
        r = self.delete(self.build(), "alias.txt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.islink(os.path.join(self.root, "alias.txt")))

    @absltest.skipUnless(_NONROOT, "permission bits do not bind root")
    def test_in_unwritable_directory_is_permission_denied(self):
        path = self.seed("locked/f.txt", b"x")
        locked = os.path.dirname(path)
        os.chmod(locked, 0o555)
        self.addCleanup(os.chmod, locked, 0o700)
        r = self.delete(self.build(), "locked/f.txt")
        self.assert_error(r, 403, "permission_denied")
        self.assertTrue(os.path.exists(path))


class RenameTest(FilesystemTestCase):

    def test_renames_a_file(self):
        self.seed("old.txt", b"cargo")
        r = self.rename(self.build(), "old.txt", "new.txt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"created": True})
        self.assertFalse(os.path.lexists(os.path.join(self.root, "old.txt")))
        with open(os.path.join(self.root, "new.txt"), "rb") as f:
            self.assertEqual(f.read(), b"cargo")

    def test_replaces_destination_atomically(self):
        src = self.seed("src.txt", b"winner")
        os.chmod(src, 0o604)
        self.seed("dst.txt", b"loser")
        r = self.rename(self.build(), "src.txt", "dst.txt")
        self.assertEqual(r.json(), {"created": False})
        dst = os.path.join(self.root, "dst.txt")
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), b"winner")
        # rename(2) moves the inode: the source's bits travel with it.
        self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o604)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "src.txt")))

    def test_onto_itself_is_a_noop(self):
        self.seed("same.txt", b"still here")
        r = self.rename(self.build(), "same.txt", "same.txt")
        self.assertEqual(r.json(), {"created": False})
        with open(os.path.join(self.root, "same.txt"), "rb") as f:
            self.assertEqual(f.read(), b"still here")

    def test_missing_source_is_not_found(self):
        self.assert_error(self.rename(self.build(), "ghost", "x"), 404, "not_found")

    def test_missing_destination_directory_is_not_found(self):
        self.seed("a.txt")
        r = self.rename(self.build(), "a.txt", "no/dir/b.txt")
        self.assert_error(r, 404, "not_found")
        self.assertTrue(os.path.exists(os.path.join(self.root, "a.txt")))

    def test_directory_source_is_invalid_request(self):
        os.makedirs(os.path.join(self.root, "adir"))
        self.assert_error(self.rename(self.build(), "adir", "b"), 400, "invalid_request")

    def test_directory_destination_is_invalid_request(self):
        self.seed("a.txt")
        os.makedirs(os.path.join(self.root, "adir"))
        self.assert_error(self.rename(self.build(), "a.txt", "adir"), 400, "invalid_request")

    def test_traversal_in_destination_is_invalid_request(self):
        self.seed("a.txt")
        r = self.rename(self.build(), "a.txt", "../escapee")
        self.assert_error(r, 400, "invalid_request")
        self.assertTrue(os.path.exists(os.path.join(self.root, "a.txt")))

    def test_unknown_body_field_is_invalid_request(self):
        self.seed("a.txt")
        r = self.build().post(
            "/filesystem/v1/rename",
            json={"from": "a.txt", "to": "b.txt", "overwrite": False},
        )
        self.assert_error(r, 400, "invalid_request")


class CopyTest(FilesystemTestCase):

    def test_copies_a_file(self):
        self.seed("orig.txt", b"twin material")
        r = self.copy(self.build(), "orig.txt", "twin.txt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"size": 13, "created": True})
        for name in ("orig.txt", "twin.txt"):
            with open(os.path.join(self.root, name), "rb") as f:
                self.assertEqual(f.read(), b"twin material")

    def test_created_copy_takes_source_bits(self):
        # cp(1)'s rule: a fresh destination gets the source's mode & ~umask.
        self.addCleanup(os.umask, os.umask(0o022))
        path = self.seed("tool.sh", b"#!/bin/sh\n")
        os.chmod(path, 0o750)
        r = self.copy(self.build(), "tool.sh", "tool2.sh")
        self.assertEqual(r.status_code, 200, r.text)
        mode = stat.S_IMODE(os.stat(os.path.join(self.root, "tool2.sh")).st_mode)
        self.assertEqual(mode, 0o750)

    def test_existing_destination_keeps_its_own_bits(self):
        self.seed("src.txt", b"payload")
        dst = self.seed("dst.txt", b"old")
        os.chmod(dst, 0o604)
        r = self.copy(self.build(), "src.txt", "dst.txt")
        self.assertEqual(r.json(), {"size": 7, "created": False})
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), b"payload")
        self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o604)

    def test_onto_itself_is_invalid_request(self):
        self.seed("same.txt", b"x")
        r = self.copy(self.build(), "same.txt", "same.txt")
        self.assert_error(r, 400, "invalid_request")

    def test_via_symlink_onto_itself_is_invalid_request(self):
        path = self.seed("real.txt", b"x")
        os.symlink(path, os.path.join(self.root, "alias.txt"))
        r = self.copy(self.build(), "alias.txt", "real.txt")
        self.assert_error(r, 400, "invalid_request")

    def test_missing_source_is_not_found(self):
        self.assert_error(self.copy(self.build(), "ghost", "x"), 404, "not_found")

    def test_directory_source_is_invalid_request(self):
        os.makedirs(os.path.join(self.root, "adir"))
        self.assert_error(self.copy(self.build(), "adir", "b"), 400, "invalid_request")

    def test_missing_destination_directory_is_not_found(self):
        self.seed("a.txt")
        self.assert_error(self.copy(self.build(), "a.txt", "no/dir/b"), 404, "not_found")


class TmpdirTest(FilesystemTestCase):
    """filesystem-v1 §5.6: mkdtemp(3) under the root's scratch area."""

    def tmpdir(self, client):
        return client.post("/filesystem/v1/tmpdir")

    def test_creates_a_fresh_private_directory(self):
        self.addCleanup(os.umask, os.umask(0o022))
        r = self.tmpdir(self.build())
        self.assertEqual(r.status_code, 200, r.text)
        rel = r.json()["path"]
        self.assertTrue(rel.startswith("tmp/"), rel)
        full = os.path.join(self.root, rel)
        self.assertTrue(os.path.isdir(full))
        self.assertEqual(stat.S_IMODE(os.stat(full).st_mode), 0o700)

    def test_each_call_is_distinct(self):
        client = self.build()
        a = self.tmpdir(client).json()["path"]
        b = self.tmpdir(client).json()["path"]
        self.assertNotEqual(a, b)

    def test_scratch_lifecycle(self):
        client = self.build()
        rel = self.tmpdir(client).json()["path"]
        self.assertEqual(
            self.write(client, f"{rel}/scratch.txt", b"work").status_code, 200
        )
        self.assertEqual(self.read(client, f"{rel}/scratch.txt").content, b"work")
        # Populated: refuses to go...
        self.assert_error(self.delete(client, rel), 400, "invalid_request")
        # ...emptied: goes.
        self.assertEqual(self.delete(client, f"{rel}/scratch.txt").status_code, 200)
        r = self.delete(client, rel)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(os.path.exists(os.path.join(self.root, rel)))


if __name__ == "__main__":
    absltest.main()
