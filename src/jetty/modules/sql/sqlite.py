"""The reference driver: one SQLite file (sql-v1 §3).

One connection, serialized by a lock — a sidecar fronts one co-located
application, and SQLite's own write model is a single writer anyway, so a
pool would add failure modes without adding throughput. WAL keeps readers
from blocking behind the writer in other processes sharing the file.

Error translation happens on SQLite's primary result codes
(``sqlite_errorname``), not on exception classes alone: OperationalError
covers both "no such table" (the client's fault, ``sql_error``) and
"disk I/O error" (the store's fault, ``upstream_unavailable``), and
fail-closed means never blaming the client for a broken store.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Sequence

from jetty.modules.sql.driver import (
    ConstraintViolation,
    Migration,
    MigrateResult,
    MigrationConflict,
    ResultSet,
    Statement,
    StatementRejected,
    Value,
)

_LEDGER = "_jetty_sql_migrations"

#: Primary result codes that mean "the statement was wrong", as opposed to
#: "the store is in trouble". Everything not listed propagates and becomes
#: upstream_unavailable at the surface — the fail-closed default.
_REJECTION_CODES = {"SQLITE_ERROR", "SQLITE_RANGE", "SQLITE_MISMATCH"}


def _translate(exc: sqlite3.Error) -> Exception | None:
    """The typed-exception mapping, or None to let the store's trouble
    propagate as unavailability."""
    if isinstance(exc, sqlite3.IntegrityError):
        return ConstraintViolation(str(exc))
    if isinstance(exc, sqlite3.ProgrammingError):
        return StatementRejected(str(exc))
    if getattr(exc, "sqlite_errorname", "") in _REJECTION_CODES:
        return StatementRejected(str(exc))
    return None


class SqliteDriver:
    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        # isolation_level=None: no implicit transactions; every transaction
        # below is an explicit BEGIN IMMEDIATE so atomicity is visible in
        # this file rather than delegated to driver magic.
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def ping(self) -> None:
        async with self._lock:
            self._db().execute("SELECT 1")

    async def query(self, stmt: Statement) -> ResultSet:
        async with self._lock:
            conn = self._db()
            # sql-v1 §2: /query MUST NOT write. The dialect gate already
            # admitted only a SELECT; query_only makes the store enforce it
            # too, so a gate bug cannot become a write.
            conn.execute("PRAGMA query_only=ON")
            try:
                cur = conn.execute(stmt.sql, stmt.params)
                columns = [d[0] for d in cur.description or []]
                rows = [[_wire_value(v) for v in row] for row in cur.fetchall()]
            except sqlite3.Error as exc:
                raise _translate(exc) or exc from exc
            finally:
                conn.execute("PRAGMA query_only=OFF")
        return ResultSet(columns=columns, rows=rows)

    async def execute(self, stmts: Sequence[Statement]) -> list[int]:
        async with self._lock:
            conn = self._db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                counts = []
                for stmt in stmts:
                    counts.append(conn.execute(stmt.sql, stmt.params).rowcount)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise _translate(exc) or exc from exc
        return counts

    async def migrate(self, migrations: Sequence[Migration]) -> MigrateResult:
        async with self._lock:
            conn = self._db()
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_LEDGER} ("
                "  pos INTEGER PRIMARY KEY,"
                "  name TEXT NOT NULL UNIQUE,"
                "  applied_at TEXT NOT NULL)"
            )
            applied = [
                row[0]
                for row in conn.execute(f"SELECT name FROM {_LEDGER} ORDER BY pos")
            ]
            names = [m.name for m in migrations]
            _check_history(applied, names)

            newly = []
            for pos, migration in enumerate(migrations):
                if pos < len(applied):
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in migration.statements:
                        conn.execute(statement)
                    conn.execute(
                        f"INSERT INTO {_LEDGER} (pos, name, applied_at)"
                        "  VALUES (?, ?, datetime('now'))",
                        (pos, migration.name),
                    )
                    conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    conn.execute("ROLLBACK")
                    translated = _translate(exc)
                    if translated is not None:
                        raise StatementRejected(
                            f"migration {migration.name!r}: {translated}"
                        ) from exc
                    raise
                newly.append(migration.name)
        return MigrateResult(applied=newly, already_applied=applied)

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("sqlite driver is not open")
        return self._conn


def _check_history(applied: list[str], names: list[str]) -> None:
    """sql-v1 §5.3: the submitted list must begin with the applied history."""
    if len(set(names)) != len(names):
        dupe = next(n for n in names if names.count(n) > 1)
        raise MigrationConflict(f"migration name {dupe!r} appears twice")
    if len(applied) > len(names):
        raise MigrationConflict(
            f"{len(applied)} migrations are applied but only "
            f"{len(names)} were submitted; migrations cannot be removed"
        )
    for pos, (have, want) in enumerate(zip(applied, names)):
        if have != want:
            raise MigrationConflict(
                f"position {pos} is applied as {have!r} but submitted as "
                f"{want!r}; applied history cannot be rewritten"
            )


def _wire_value(value: object) -> Value:
    """sql-v1 §4: JSON scalars only, or a typed refusal — never an encoding."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise StatementRejected(
        f"result contains a {type(value).__name__} value, which is outside "
        "sql-v1's value set (§4)"
    )
