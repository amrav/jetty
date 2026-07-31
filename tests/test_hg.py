"""The `hg` module against real repositories (spec/hg-v1.md).

Fixtures are actual Mercurial repos seeded under `create_tempdir()`; the
`mercurial` dev dependency puts an `hg` script next to the interpreter, so
nothing here assumes a system-wide install. Every seeding invocation runs
with `HGPLAIN=1` and an empty `HGRCPATH` — the same neutralisation the
module itself applies — so a developer's ~/.hgrc cannot shape fixtures.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from absl.testing import absltest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.server import create_app

#: The venv's own hg first (where `pip install mercurial` puts it), PATH as
#: fallback for environments that install it some other way.
_HG = (
    lambda venv_hg: venv_hg if os.path.exists(venv_hg) else shutil.which("hg")
)(os.path.join(os.path.dirname(sys.executable), "hg"))

_ENV = dict(os.environ, HGPLAIN="1", HGRCPATH="")


@absltest.skipUnless(_HG, "no hg executable available")
class HgModuleTest(absltest.TestCase):
    """One seeded repo per test, shaped to exercise every status code:

    rev 0  a.txt, sub/b.txt added
    rev 1  a.txt modified; c.txt added; a2.txt copied from a.txt;
           sub/b.txt removed
    wdir   c.txt modified (M); d.txt added-tracked-uncommitted (A);
           untracked.txt present but never added (?)
    """

    def setUp(self):
        super().setUp()
        self.socket_path = os.path.join(
            self.create_tempdir().full_path, "jetty.sock"
        )
        self.repo = self.create_tempdir().full_path
        self._hg("init")
        self._write("a.txt", "alpha v1\n")
        self._write("sub/b.txt", "bee\n")
        self._hg("add", "-q", "a.txt", "sub/b.txt")
        self._commit("first")
        self._write("a.txt", "alpha v2\n")
        self._write("c.txt", "cee v1\n")
        self._hg("add", "-q", "c.txt")
        self._hg("cp", "-q", "a.txt", "a2.txt")
        self._hg("rm", "-q", "sub/b.txt")
        self._commit("second", user="Bob <b@example.com>", date="20 -3600")
        self._write("c.txt", "cee v2\n")
        self._write("d.txt", "dee\n")
        self._hg("add", "-q", "d.txt")
        self._write("untracked.txt", "stray\n")
        self.node0, self.node1 = self._nodes()

    def _hg(self, *args: str) -> str:
        proc = subprocess.run(
            [_HG, *args],
            cwd=self.repo,
            env=_ENV,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout

    def _commit(
        self, message: str, user: str = "Alice <a@example.com>", date: str = "10 0"
    ) -> None:
        self._hg("commit", "-q", "-m", message, "-u", user, "-d", date)

    def _write(self, rel: str, content: str) -> None:
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _nodes(self) -> list[str]:
        return self._hg("log", "-T", "{node}\n", "-r", "0:1").splitlines()

    def build(self, **overrides) -> TestClient:
        settings = {"enabled": True, "repo": self.repo, "hg_bin": _HG, **overrides}
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": {"hg": settings}}
        )
        return TestClient(create_app(cfg))

    # --- /repo -----------------------------------------------------------

    def test_repo_summary(self):
        with self.build() as c:
            body = c.get("/hg/v1/repo").json()
        self.assertEqual(body["tip"], self.node1)
        self.assertEqual(body["wdir_parents"], [self.node1])
        self.assertEqual(body["branch"], "default")
        self.assertTrue(body["dirty"])
        self.assertEqual(os.path.realpath(body["root"]), os.path.realpath(self.repo))

    def test_untracked_files_alone_are_not_dirty(self):
        self._hg("revert", "-q", "--all", "--no-backup")
        # untracked.txt survives the revert; d.txt's add was reverted so its
        # on-disk copy is now untracked too. Neither makes the checkout dirty.
        with self.build() as c:
            self.assertFalse(c.get("/hg/v1/repo").json()["dirty"])

    # --- /changesets -----------------------------------------------------

    def test_log_newest_first_with_full_nodes(self):
        with self.build() as c:
            body = c.get("/hg/v1/changesets").json()
        self.assertEqual(
            [e["node"] for e in body["changesets"]], [self.node1, self.node0]
        )
        self.assertIsNone(body["next"])
        newest = body["changesets"][0]
        self.assertEqual(newest["user"], "Bob <b@example.com>")
        self.assertEqual(newest["parents"], [self.node0])
        self.assertEqual(newest["date"], "1970-01-01T01:00:20+01:00")
        # The root changeset's null parent is filtered, not surfaced.
        self.assertEqual(body["changesets"][1]["parents"], [])

    def test_log_pagination_cursor(self):
        with self.build() as c:
            first = c.get("/hg/v1/changesets", params={"limit": 1}).json()
            self.assertEqual([e["node"] for e in first["changesets"]], [self.node1])
            self.assertEqual(first["next"], self.node0)
            second = c.get(
                "/hg/v1/changesets", params={"limit": 1, "start": first["next"]}
            ).json()
        self.assertEqual([e["node"] for e in second["changesets"]], [self.node0])
        self.assertIsNone(second["next"])

    def test_log_path_filter(self):
        with self.build() as c:
            body = c.get("/hg/v1/changesets", params={"path": "c.txt"}).json()
        self.assertEqual([e["node"] for e in body["changesets"]], [self.node1])

    def test_log_user_filter(self):
        with self.build() as c:
            body = c.get("/hg/v1/changesets", params={"user": "Alice"}).json()
        self.assertEqual([e["node"] for e in body["changesets"]], [self.node0])

    # --- /changesets/{rev} -----------------------------------------------

    def test_changeset_detail_files_and_copies(self):
        with self.build() as c:
            body = c.get("/hg/v1/changesets/tip").json()
        self.assertEqual(body["node"], self.node1)
        by_path = {f["path"]: f for f in body["files"]}
        self.assertEqual(by_path["a.txt"]["status"], "M")
        self.assertEqual(by_path["c.txt"]["status"], "A")
        self.assertEqual(by_path["sub/b.txt"]["status"], "R")
        self.assertEqual(by_path["a2.txt"]["status"], "A")
        self.assertEqual(by_path["a2.txt"]["copy_source"], "a.txt")
        self.assertNotIn("copy_source", by_path["c.txt"])

    def test_short_prefix_resolves_and_response_carries_full_node(self):
        with self.build() as c:
            body = c.get(f"/hg/v1/changesets/{self.node0[:8]}").json()
        self.assertEqual(body["node"], self.node0)

    # --- /changesets/{rev}/diff ------------------------------------------

    def test_diff_is_git_style(self):
        with self.build() as c:
            r = c.get("/hg/v1/changesets/tip/diff")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/x-diff", r.headers["content-type"])
        self.assertIn("diff --git a/a.txt b/a.txt", r.text)
        self.assertIn("copy from a.txt", r.text)

    def test_diff_narrowed_to_one_path(self):
        with self.build() as c:
            text = c.get(
                "/hg/v1/changesets/tip/diff", params={"path": "a.txt"}
            ).text
        self.assertIn("a.txt", text)
        self.assertNotIn("c.txt", text)

    # --- /status ---------------------------------------------------------

    def test_status_default_distinguishes_tracked_from_untracked(self):
        """The A / ? split: added-and-tracked is not the same answer as
        present-but-untracked, and both differ from committed (absent)."""
        with self.build() as c:
            files = c.get("/hg/v1/status").json()["files"]
        by_path = {f["path"]: f["status"] for f in files}
        self.assertEqual(
            by_path, {"c.txt": "M", "d.txt": "A", "untracked.txt": "?"}
        )

    def test_status_between_two_revisions(self):
        with self.build() as c:
            files = c.get(
                "/hg/v1/status", params={"from": "0", "to": "1"}
            ).json()["files"]
        by_path = {f["path"]: f["status"] for f in files}
        self.assertEqual(
            by_path,
            {"a.txt": "M", "a2.txt": "A", "c.txt": "A", "sub/b.txt": "R"},
        )

    def test_status_to_without_from_means_what_that_rev_changed(self):
        with self.build() as c:
            pair = c.get(
                "/hg/v1/status", params={"from": "0", "to": "1"}
            ).json()
            change = c.get("/hg/v1/status", params={"to": "1"}).json()
        self.assertEqual(change, pair)

    # --- /files ----------------------------------------------------------

    def test_file_content_at_each_revision(self):
        with self.build() as c:
            self.assertEqual(c.get("/hg/v1/files/0/a.txt").text, "alpha v1\n")
            self.assertEqual(c.get("/hg/v1/files/1/a.txt").text, "alpha v2\n")
            # Committed content, not the working directory's modification.
            self.assertEqual(c.get("/hg/v1/files/tip/c.txt").text, "cee v1\n")

    def test_file_content_guesses_media_type(self):
        with self.build() as c:
            r = c.get("/hg/v1/files/0/a.txt")
        self.assertIn("text/plain", r.headers["content-type"])

    def test_file_untracked_at_revision_is_not_found(self):
        with self.build() as c:
            r = c.get("/hg/v1/files/0/c.txt")  # exists only from rev 1
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

    # --- errors and injection --------------------------------------------

    def test_unknown_revision_is_not_found(self):
        with self.build() as c:
            for url in (
                "/hg/v1/changesets/zzzzz",
                "/hg/v1/changesets/zzzzz/diff",
                "/hg/v1/files/zzzzz/a.txt",
            ):
                r = c.get(url)
                self.assertEqual(r.status_code, 404, url)
                self.assertEqual(r.json()["error"]["code"], "not_found", url)

    def test_revset_syntax_in_rev_is_rejected(self):
        """The charset gate (spec §2): operators must never reach a revset."""
        with self.build() as c:
            for rev in ("tip~1", "0:1", "all()", "tip or 0", "'0'"):
                r = c.get("/hg/v1/changesets", params={"start": rev})
                self.assertEqual(r.status_code, 400, rev)
                self.assertEqual(r.json()["error"]["code"], "invalid_request", rev)

    def test_traversing_path_is_rejected(self):
        with self.build() as c:
            r = c.get("/hg/v1/files/0/../secrets")
            self.assertEqual(r.status_code, 400)
            r = c.get("/hg/v1/changesets", params={"path": "a/../../etc"})
            self.assertEqual(r.status_code, 400)

    # --- lifecycle -------------------------------------------------------

    def test_disabled_module_answers_module_disabled(self):
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": {}}
        )
        with TestClient(create_app(cfg)) as c:
            r = c.get("/hg/v1/repo")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "module_disabled")

    def test_missing_repo_setting_fails_at_build(self):
        cfg = Config.model_validate(
            {
                "listener": {"uds": self.socket_path},
                "modules": {"hg": {"enabled": True}},
            }
        )
        with self.assertRaises(Exception):
            create_app(cfg)

    def test_unknown_setting_key_fails_at_build(self):
        with self.assertRaises(Exception):
            create_app(
                Config.model_validate(
                    {
                        "listener": {"uds": self.socket_path},
                        "modules": {
                            "hg": {"enabled": True, "repo": self.repo, "rep0": "x"}
                        },
                    }
                )
            )

    def test_non_repository_aborts_boot(self):
        """SPEC.md §1.2: serve correctly or refuse to start."""
        not_a_repo = self.create_tempdir().full_path
        with self.assertRaisesRegex(RuntimeError, "cannot open repository"):
            self.build(repo=not_a_repo).__enter__()


if __name__ == "__main__":
    absltest.main()
