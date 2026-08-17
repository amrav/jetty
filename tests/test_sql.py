"""The `sql` module end to end (spec/sql-v1.md).

Every test runs the full stack — surface, dialect gate, sqlite driver —
against a real database file under `create_tempdir()`. The gate tests each
name which side of the intersection the statement fails on, because that is
the module's whole promise: SQLite-only syntax dies here, not on the
internal deployment; GoogleSQL-only syntax dies here, not in production.
"""

from __future__ import annotations

import os

from absl.testing import absltest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.server import create_app

_SCHEMA = [
    {
        "name": "0001-tasks",
        "statements": [
            "CREATE TABLE tasks ("
            "  id INTEGER PRIMARY KEY,"
            "  title TEXT NOT NULL,"
            "  owner TEXT NOT NULL,"
            "  done INTEGER NOT NULL DEFAULT 0)"
        ],
    },
    {
        "name": "0002-owner-unique",
        "statements": ["CREATE UNIQUE INDEX tasks_title ON tasks (title)"],
    },
]


class SqlModuleTest(absltest.TestCase):
    def setUp(self):
        super().setUp()
        tmp = self.create_tempdir().full_path
        self.socket_path = os.path.join(tmp, "jetty.sock")
        self.db_path = os.path.join(tmp, "app.sqlite")

    def build(self, **overrides) -> TestClient:
        settings = {"enabled": True, "path": self.db_path, **overrides}
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": {"sql": settings}}
        )
        return TestClient(create_app(cfg))

    @staticmethod
    def migrate(c: TestClient, migrations=None):
        return c.post("/sql/v1/migrate", json={"migrations": migrations or _SCHEMA})

    @staticmethod
    def insert(c: TestClient, title: str, owner: str = "avarma"):
        return c.post(
            "/sql/v1/exec",
            json={
                "statements": [
                    {
                        "sql": "INSERT INTO tasks (title, owner) VALUES (@t, @o)",
                        "params": {"t": title, "o": owner},
                    }
                ]
            },
        )

    def assert_error(self, response, status: int, code: str):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()["error"]
        self.assertEqual(body["code"], code)
        self.assertIn("retryable", body)

    # --- the happy path --------------------------------------------------

    def test_migrate_write_read_round_trip(self):
        with self.build() as c:
            body = self.migrate(c).json()
            self.assertEqual(body["applied"], ["0001-tasks", "0002-owner-unique"])
            self.assertEqual(body["already_applied"], [])

            self.assertEqual(self.insert(c, "write the spec").json(), {"rowcounts": [1]})
            self.insert(c, "ship it")

            got = c.post(
                "/sql/v1/query",
                json={
                    "sql": "SELECT id, title FROM tasks WHERE owner = @o ORDER BY id",
                    "params": {"o": "avarma"},
                },
            ).json()
            self.assertEqual(got["columns"], ["id", "title"])
            self.assertEqual(got["rows"], [[1, "write the spec"], [2, "ship it"]])

    def test_values_round_trip(self):
        big = 2**60 + 7   # JSON numbers must not lose 64-bit integers server-side
        with self.build() as c:
            self.migrate(c)
            got = c.post(
                "/sql/v1/query",
                json={
                    "sql": "SELECT @i AS i, @f AS f, @s AS s, @n AS n, @b AS b",
                    "params": {"i": big, "f": 1.5, "s": "héllo", "n": None, "b": True},
                },
            ).json()
            # sql-v1 §2.2: sqlite renders TRUE as 1; everything else is exact.
            self.assertEqual(got["rows"], [[big, 1.5, "héllo", None, 1]])

    def test_backend_reports_dialect(self):
        with self.build() as c:
            self.assertEqual(c.get("/sql/v1/backend").json(), {"dialect": "sqlite"})

    def test_meta_advertises_module(self):
        with self.build() as c:
            modules = c.get("/v1/meta").json()["modules"]
        self.assertIn(
            {"name": "sql", "api_version": "v1", "mount": "/sql"}, modules
        )

    # --- the dialect gate (spec §2) --------------------------------------

    def test_sqlite_only_syntax_is_rejected_before_the_store(self):
        # INSERT OR REPLACE is SQLite; GoogleSQL has no such form. This must
        # fail here, in development, not on the internal deployment.
        with self.build() as c:
            self.migrate(c)
            r = c.post(
                "/sql/v1/exec",
                json={
                    "statements": [
                        {
                            "sql": "INSERT OR REPLACE INTO tasks (title, owner)"
                            " VALUES (@t, @o)",
                            "params": {"t": "x", "o": "y"},
                        }
                    ]
                },
            )
            self.assert_error(r, 400, "unsupported_sql")

    def test_googlesql_only_function_is_rejected_by_the_store(self):
        # SAFE_DIVIDE parses as GoogleSQL, so the gate admits it; SQLite has
        # no such function. The intersection's other jaw: 400 sql_error.
        with self.build() as c:
            self.migrate(c)
            r = c.post(
                "/sql/v1/query", json={"sql": "SELECT SAFE_DIVIDE(1, 0)", "params": {}}
            )
            self.assert_error(r, 400, "sql_error")

    def test_query_rejects_writes(self):
        with self.build() as c:
            self.migrate(c)
            r = c.post("/sql/v1/query", json={"sql": "DELETE FROM tasks"})
            self.assert_error(r, 400, "unsupported_sql")

    def test_exec_rejects_queries_and_ddl(self):
        with self.build() as c:
            self.migrate(c)
            for sql in ("SELECT 1", "CREATE TABLE t (x INTEGER)", "DROP TABLE tasks"):
                r = c.post("/sql/v1/exec", json={"statements": [{"sql": sql}]})
                self.assert_error(r, 400, "unsupported_sql")

    def test_positional_and_colon_placeholders_are_rejected(self):
        with self.build() as c:
            self.migrate(c)
            for sql in (
                "SELECT * FROM tasks WHERE id = ?",
                "SELECT * FROM tasks WHERE id = :id",
            ):
                r = c.post("/sql/v1/query", json={"sql": sql})
                self.assert_error(r, 400, "unsupported_sql")

    def test_compound_statements_are_rejected(self):
        with self.build() as c:
            self.migrate(c)
            r = c.post(
                "/sql/v1/query", json={"sql": "SELECT 1; DELETE FROM tasks"}
            )
            self.assert_error(r, 400, "unsupported_sql")

    def test_parameter_set_must_match_exactly(self):
        with self.build() as c:
            self.migrate(c)
            missing = c.post(
                "/sql/v1/query",
                json={"sql": "SELECT * FROM tasks WHERE owner = @o"},
            )
            self.assert_error(missing, 400, "invalid_request")
            self.assertIn("missing: o", missing.json()["error"]["message"])

            unused = c.post(
                "/sql/v1/query",
                json={"sql": "SELECT * FROM tasks", "params": {"stray": 1}},
            )
            self.assert_error(unused, 400, "invalid_request")
            self.assertIn("unused: stray", unused.json()["error"]["message"])

    def test_blob_results_are_refused_not_encoded(self):
        # randomblob() parses as an anonymous function, so the gate admits
        # it; the BLOB it produces has no sql-v1 §4 representation.
        with self.build() as c:
            self.migrate(c)
            r = c.post("/sql/v1/query", json={"sql": "SELECT randomblob(4)"})
            self.assert_error(r, 400, "sql_error")

    # --- exec semantics (spec §5.2) --------------------------------------

    def test_exec_batch_is_atomic(self):
        with self.build() as c:
            self.migrate(c)
            r = c.post(
                "/sql/v1/exec",
                json={
                    "statements": [
                        {
                            "sql": "INSERT INTO tasks (title, owner) VALUES (@t, @o)",
                            "params": {"t": "survivor?", "o": "x"},
                        },
                        {
                            "sql": "INSERT INTO tasks (title, missing_col)"
                            " VALUES (@t, @o)",
                            "params": {"t": "boom", "o": "x"},
                        },
                    ]
                },
            )
            self.assert_error(r, 400, "sql_error")
            got = c.post("/sql/v1/query", json={"sql": "SELECT COUNT(*) AS n FROM tasks"})
            self.assertEqual(got.json()["rows"], [[0]])

    def test_constraint_violation_is_409(self):
        with self.build() as c:
            self.migrate(c)
            self.insert(c, "once")
            self.assert_error(self.insert(c, "once"), 409, "constraint_violation")

    def test_empty_exec_batch_is_invalid(self):
        with self.build() as c:
            self.assert_error(
                c.post("/sql/v1/exec", json={"statements": []}), 400, "invalid_request"
            )

    def test_unknown_request_field_is_invalid(self):
        with self.build() as c:
            r = c.post(
                "/sql/v1/query", json={"sql": "SELECT 1", "sql2": "SELECT 2"}
            )
            self.assert_error(r, 400, "invalid_request")

    # --- migrations (spec §5.3) ------------------------------------------

    def test_migrate_is_idempotent(self):
        with self.build() as c:
            self.migrate(c)
            body = self.migrate(c).json()
            self.assertEqual(body["applied"], [])
            self.assertEqual(
                body["already_applied"], ["0001-tasks", "0002-owner-unique"]
            )

    def test_migrate_applies_only_the_new_suffix(self):
        extended = _SCHEMA + [
            {"name": "0003-notes", "statements": ["CREATE TABLE notes (t TEXT)"]}
        ]
        with self.build() as c:
            self.migrate(c)
            body = self.migrate(c, extended).json()
            self.assertEqual(body["applied"], ["0003-notes"])

    def test_rewritten_history_is_a_conflict(self):
        renamed = [dict(_SCHEMA[0], name="0001-renamed"), _SCHEMA[1]]
        with self.build() as c:
            self.migrate(c)
            self.assert_error(self.migrate(c, renamed), 409, "migration_conflict")

    def test_shortened_history_is_a_conflict(self):
        with self.build() as c:
            self.migrate(c)
            self.assert_error(self.migrate(c, [_SCHEMA[0]]), 409, "migration_conflict")

    def test_failing_migration_keeps_earlier_ones(self):
        broken = _SCHEMA + [
            {"name": "0003-broken", "statements": ["CREATE TABLE ("]}
        ]
        with self.build() as c:
            self.assert_error(self.migrate(c, broken), 400, "sql_error")
            # 0001/0002 are durable; resubmitting the good prefix confirms it.
            body = self.migrate(c).json()
            self.assertEqual(body["applied"], [])
            self.assertEqual(
                body["already_applied"], ["0001-tasks", "0002-owner-unique"]
            )

    # --- boot behaviour --------------------------------------------------

    def test_unknown_driver_fails_at_boot(self):
        with self.assertRaisesRegex(Exception, "corp.*not available"):
            self.build(driver="corp")

    def test_missing_parent_directory_fails_at_boot(self):
        self.db_path = os.path.join(
            self.create_tempdir().full_path, "no", "such", "dir", "app.sqlite"
        )
        with self.assertRaises(Exception):
            self.build().__enter__()

    def test_data_persists_across_restarts(self):
        with self.build() as c:
            self.migrate(c)
            self.insert(c, "durable")
        with self.build() as c:
            got = c.post("/sql/v1/query", json={"sql": "SELECT title FROM tasks"})
            self.assertEqual(got.json()["rows"], [["durable"]])


if __name__ == "__main__":
    absltest.main()
