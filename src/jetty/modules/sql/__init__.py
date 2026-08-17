"""The `sql` module — spec/sql-v1.md.

One relational storage surface an application uses unchanged in both
deployments: the reference build fronts a local SQLite file, an internal
build fronts the internal store, and the application's SQL crosses the
boundary verbatim in both. That is only sound if the SQL means the same
thing on both sides, so the wire dialect is the intersection of GoogleSQL
and SQLite, and the surface refuses statements outside it.

Layout:

- ``driver``  — the internal representation and the ``SqlDriver`` protocol
- ``dialect`` — the wire-dialect gate: is this statement in the intersection?
- ``sqlite``  — the reference driver: one SQLite file
- ``module``  — the surface: wire shapes, the gate, error mapping
"""
