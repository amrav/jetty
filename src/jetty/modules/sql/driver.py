"""The internal representation and the ``SqlDriver`` protocol (sql-v1 §4–§5).

The surface (module.py) owns the wire shapes and the dialect gate and
dispatches to a driver; a driver translates these types to whatever the
backing store speaks. Nothing in this file knows about URLs, JSON, or what
is or is not in the wire dialect — a driver receives statements the surface
has already admitted.

Error contract (sql-v1 §6): a driver raises the typed exceptions below for
conditions the protocol distinguishes. Anything else it raises is an
unreachable or unusable backing store: the surface maps it to
``upstream_unavailable``, never to a fabricated success (SPEC.md §1.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

#: The whole value vocabulary of sql-v1 (§4), both directions. BLOBs and
#: structured types are deliberately absent; a driver that meets one in a
#: result raises StatementRejected rather than inventing an encoding.
Value = None | bool | int | float | str


class StatementRejected(Exception):
    """The backing store rejected the statement, or its result left §4's
    value set. Surface: ``400 sql_error``."""


class ConstraintViolation(Exception):
    """A uniqueness, foreign-key, or check constraint stopped a write.
    Surface: ``409 constraint_violation``."""


class MigrationConflict(Exception):
    """The submitted migration list contradicts the applied history
    (sql-v1 §5.3). Surface: ``409 migration_conflict``."""


@dataclass
class Statement:
    sql: str
    params: dict[str, Value] = field(default_factory=dict)


@dataclass
class ResultSet:
    columns: list[str]
    rows: list[list[Value]]


@dataclass
class Migration:
    name: str
    statements: list[str]     # backing-store dialect, one statement per entry


@dataclass
class MigrateResult:
    applied: list[str]
    already_applied: list[str]


class SqlDriver(Protocol):
    #: The DDL dialect `/migrate` expects — `GET /sql/v1/backend` (sql-v1 §5.4).
    dialect: str

    async def open(self) -> None: ...     # raising aborts boot (SPEC.md §1.2)
    async def close(self) -> None: ...
    async def query(self, stmt: Statement) -> ResultSet: ...
    #: Atomic: every statement commits or none does (sql-v1 §5.2).
    async def execute(self, stmts: Sequence[Statement]) -> list[int]: ...
    async def migrate(self, migrations: Sequence[Migration]) -> MigrateResult: ...
    async def ping(self) -> None: ...
