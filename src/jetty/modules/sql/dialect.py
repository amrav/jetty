"""The wire-dialect gate (sql-v1 §2): is this statement in the intersection?

The contract of the `sql` module is that a statement the reference build
accepts would mean the same thing to the GoogleSQL analyzer, so an
application developed against SQLite does not discover on the internal
deployment that its SQL never parsed. This gate is the GoogleSQL half of
that contract; executing on the backing store is the SQLite half, and a
statement must survive both.

sqlglot's BigQuery grammar stands in for the GoogleSQL analyzer here
(sql-v1 §2 leaves the mechanism to the implementation; an internal build
uses the real thing, which is authoritative where the two disagree). An
approximation that rejects locally is strictly cheaper than a parse error
that first appears against the internal deployment — that asymmetry is the
whole reason to gate at all.

The grammar is deliberately lenient on read — it parses several SQLite-only
forms without complaint — so leniency is closed off where it matters: the
statement kinds are a closed set, placeholders other than ``@name`` are
rejected from the tree, and the SQLite-specific DML markers the parser
tolerates (``INSERT OR …``, ``ON CONFLICT``, ``RETURNING``) are rejected by
node. A statement that still slips both jaws of the gate fails on the other
backend's parser, which is the outcome the gate exists to move earlier, not
a correctness hole.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

#: What the gate admits, per endpoint (sql-v1 §2). Everything else — DDL,
#: MERGE, transaction control, pragmas — goes through /migrate or not at all.
KIND_QUERY = "query"
KIND_DML = "dml"

_DML_NODES = (exp.Insert, exp.Update, exp.Delete)


class OutsideIntersection(Exception):
    """The statement is not in the wire dialect; the message names why."""


@dataclass
class Classified:
    kind: str                  # KIND_QUERY | KIND_DML
    param_names: frozenset[str]


def classify(sql: str) -> Classified:
    """Admit one GoogleSQL statement and report its kind and parameters.

    Raises OutsideIntersection for anything sql-v1 §2 excludes. Never touches
    a database: this is the pre-flight gate, and the backing store's own
    rejection (`sql_error`) is a different, later failure.
    """
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except sqlglot.errors.SqlglotError as exc:
        # sqlglot's message pinpoints line/column; it contains only the SQL
        # the client itself sent, so echoing it leaks nothing new (§1.4).
        raise OutsideIntersection(f"not GoogleSQL: {exc}") from exc

    trees = [t for t in statements if t is not None]
    if len(trees) != 1:
        raise OutsideIntersection(
            f"exactly one statement per entry, got {len(trees)}"
        )
    tree = trees[0]

    for node in tree.find_all(exp.Placeholder):
        text = f":{node.name}" if node.name else "?"
        raise OutsideIntersection(
            f"placeholder {text} is not GoogleSQL; parameters are @name"
        )

    if isinstance(tree, exp.Insert) and tree.args.get("alternative"):
        raise OutsideIntersection(
            f"INSERT OR {tree.args['alternative']} is not GoogleSQL"
        )
    for node_type, form in ((exp.OnConflict, "ON CONFLICT"), (exp.Returning, "RETURNING")):
        if next(tree.find_all(node_type), None) is not None:
            raise OutsideIntersection(f"{form} is not GoogleSQL")

    names = set()
    for node in tree.find_all(exp.Parameter):
        name = node.name or str(node.this)
        names.add(name)

    if isinstance(tree, exp.Query):
        kind = KIND_QUERY
    elif isinstance(tree, _DML_NODES):
        kind = KIND_DML
    else:
        raise OutsideIntersection(
            f"{type(tree).__name__.upper()} is not accepted here: "
            "/query takes one SELECT, /exec takes INSERT/UPDATE/DELETE, "
            "and schema changes go through /migrate"
        )

    return Classified(kind=kind, param_names=frozenset(names))
