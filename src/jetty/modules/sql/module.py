"""The sql-v1 surface: wire shapes, the dialect gate, and error mapping.

Unlike `chat`, this is not a foreign protocol: requests and errors use the
SPEC.md §3 envelope. sql-v1 §6 adds four module codes, which SPEC.md §3.1
permits a module to define; the route class below renders them in the same
envelope shape, with the same closed-set-by-construction discipline as
`jetty.errors`.

Fail-closed (SPEC.md §1.2) at this boundary means: a statement is gated
before it travels, a driver failure that is not the client's fault is
`upstream_unavailable`, and no partial result ever leaves as a `200`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Mapping

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from jetty.errors import ErrorCode, JettyError
from jetty.modules.base import Module
from jetty.modules.sql import dialect
from jetty.modules.sql.driver import (
    ConstraintViolation,
    Migration,
    MigrationConflict,
    SqlDriver,
    Statement,
    StatementRejected,
    Value,
)

log = logging.getLogger("jetty.sql")

#: sql-v1 §6: the module's own codes. Status and retryability are paired
#: here so a handler cannot mismatch them — same argument as jetty.errors.
_SEMANTICS: dict[str, tuple[int, bool]] = {
    "unsupported_sql": (400, False),
    "sql_error": (400, False),
    "constraint_violation": (409, False),
    "migration_conflict": (409, False),
}


class SqlApiError(Exception):
    """A sql-v1 §6 module error, rendered in the SPEC.md §3.1 envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status, self.retryable = _SEMANTICS[code]

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "retryable": self.retryable,
                }
            },
        )


class _SqlRoute(APIRoute):
    """Render module codes; let everything else reach the app handlers.

    JettyError and request-validation failures already produce the correct
    envelope at app level — this surface speaks the native protocol, so only
    the module's own codes need local treatment.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except SqlApiError as exc:
                return exc.response()

        return wrapped


# --- configuration ---------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SqlSettings(_Strict):
    enabled: bool = False
    driver: str = "sqlite"
    path: str                       # required: where the sqlite file lives


# --- wire shapes -----------------------------------------------------------

class WireStatement(_Strict):
    sql: str
    params: dict[str, Value] = Field(default_factory=dict)


class ExecRequest(_Strict):
    #: sql-v1 §5.2: an empty batch is invalid_request — an atomic write of
    #: nothing is almost certainly a caller bug, and 200-ing it would hide it.
    statements: list[WireStatement] = Field(min_length=1)


class WireMigration(_Strict):
    name: str = Field(min_length=1)
    statements: list[str]


class MigrateRequest(_Strict):
    migrations: list[WireMigration] = Field(default_factory=list)


# --- the module ------------------------------------------------------------

class SqlModule(Module):
    name = "sql"
    api_version = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.config = SqlSettings.model_validate(dict(settings))
        if self.config.driver != "sqlite":
            # An internal driver belongs to an internal build; naming one
            # here must fail at boot, not silently serve SQLite under its
            # label (SPEC.md §1.2, same posture as chat).
            raise ValueError(
                f"sql.driver {self.config.driver!r} is not available; "
                "this build ships: sqlite"
            )
        from jetty.modules.sql.sqlite import SqliteDriver

        self.driver: SqlDriver = SqliteDriver(self.config.path)

    async def startup(self) -> None:
        await self.driver.open()
        await self.driver.ping()

    async def shutdown(self) -> None:
        try:
            await self.driver.close()
        except Exception:           # shutdown MUST NOT raise (Module contract)
            log.exception("sql driver close failed")

    async def _drive(self, coro: Coroutine) -> Any:
        """One driver call, with sql-v1 §6's error mapping applied."""
        try:
            return await coro
        except ConstraintViolation as exc:
            raise SqlApiError("constraint_violation", str(exc)) from exc
        except MigrationConflict as exc:
            raise SqlApiError("migration_conflict", str(exc)) from exc
        except StatementRejected as exc:
            raise SqlApiError("sql_error", str(exc)) from exc
        except (SqlApiError, JettyError):
            raise
        except Exception as exc:
            log.warning("sql driver failure: %r", exc)
            raise JettyError(
                ErrorCode.UPSTREAM_UNAVAILABLE, "backing store unavailable"
            ) from exc

    def _gate(self, wire: WireStatement, expect: str) -> Statement:
        """sql-v1 §2: admit one intersection statement of the right kind,
        with exactly the parameters it references."""
        try:
            classified = dialect.classify(wire.sql)
        except dialect.OutsideIntersection as exc:
            raise SqlApiError("unsupported_sql", str(exc)) from exc

        if classified.kind != expect:
            hint = (
                "/query takes one SELECT"
                if expect == dialect.KIND_QUERY
                else "/exec takes INSERT, UPDATE, or DELETE"
            )
            raise SqlApiError("unsupported_sql", f"wrong statement kind: {hint}")

        provided = frozenset(wire.params)
        if provided != classified.param_names:
            missing = sorted(classified.param_names - provided)
            unused = sorted(provided - classified.param_names)
            parts = [f"missing: {', '.join(missing)}"] if missing else []
            parts += [f"unused: {', '.join(unused)}"] if unused else []
            # Parameter names only — values never enter a message (§1.4).
            raise JettyError(
                ErrorCode.INVALID_REQUEST,
                f"parameter set does not match the statement ({'; '.join(parts)})",
            )
        return Statement(sql=wire.sql, params=dict(wire.params))

    def router(self) -> APIRouter:
        router = APIRouter(route_class=_SqlRoute)

        @router.post("/query")
        async def query(body: WireStatement) -> dict:
            stmt = self._gate(body, dialect.KIND_QUERY)
            result = await self._drive(self.driver.query(stmt))
            return {"columns": result.columns, "rows": result.rows}

        @router.post("/exec")
        async def execute(body: ExecRequest) -> dict:
            stmts = [self._gate(w, dialect.KIND_DML) for w in body.statements]
            counts = await self._drive(self.driver.execute(stmts))
            return {"rowcounts": counts}

        @router.post("/migrate")
        async def migrate(body: MigrateRequest) -> dict:
            migrations = [
                Migration(name=m.name, statements=list(m.statements))
                for m in body.migrations
            ]
            result = await self._drive(self.driver.migrate(migrations))
            return {
                "applied": result.applied,
                "already_applied": result.already_applied,
            }

        @router.get("/backend")
        async def backend() -> dict:
            return {"dialect": self.driver.dialect}

        return router
