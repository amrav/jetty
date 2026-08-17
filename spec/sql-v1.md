# Jetty module: `sql` — v1

**Status: experimental.** This specification is subject to breaking changes
without warning: endpoints, wire shapes, error codes, and the dialect rules
may all change while the `v1` path segment stays where it is. SPEC.md §6's
freeze applies to a *published* `api_version`, and this one is not published
yet. Do not build against it anything you are unwilling to update.

Mount: `/sql/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4, which this document does not restate.

The `sql` module gives an application one relational storage surface that
works unchanged in both deployments: against the reference build it fronts a
local SQLite file; against an internal build it fronts whatever store the
private driver speaks. The application's SQL crosses the boundary verbatim
in both cases, which is only sound if that SQL means the same thing on both
sides — so the wire dialect is defined as the **intersection of GoogleSQL
and SQLite** (§2), and a conformant implementation refuses statements it can
tell are outside it.

SPEC.md §7's "not a database" still holds: the sidecar stores nothing on its
own account. This module is a doorway; the data lives in the backing store
the driver fronts, under that store's durability rules, and disabling the
module leaves the store untouched.

Statement text and parameter values routinely contain user data. An
implementation **MUST NOT** log parameter values at any level, and **SHOULD
NOT** log statement text above `debug` (SPEC.md §1.4 applied to data).

---

## 1. Scope

In scope: parameterized single-statement reads, atomic multi-statement write
batches, sequential named migrations, and backend-dialect discovery.

Deliberately out of scope for v1: BLOB values, arrays, structs, and every
other non-scalar type; transactions spanning requests; cursors, streaming,
and pagination of result sets; prepared-statement handles; DDL through
`/query` or `/exec` (schema changes go through `/migrate`, §5.3).

---

## 2. The wire dialect

A statement sent to `/query` or `/exec` **MUST** be:

- **one** statement — no `;`-separated compounds;
- valid **GoogleSQL** — it would be accepted by the
  [GoogleSQL](https://github.com/google/googlesql) analyzer;
- valid for the **backing store** — it executes there.

An implementation **MUST** reject a statement it can determine is not
GoogleSQL with `400 unsupported_sql`, before touching the backing store. How
it determines this is its own business: an internal implementation **SHOULD**
use the real analyzer; the reference implementation approximates with a
GoogleSQL-compatible parser, and the analyzer is authoritative where they
disagree. A statement that passes the gate but is then rejected by the
backing store is `400 sql_error`.

`/query` accepts exactly one **query** (`SELECT`, including `WITH` and set
operations) and **MUST NOT** write; an implementation **SHOULD** also enforce
read-onlyness at the store. `/exec` accepts **DML only** — `INSERT`,
`UPDATE`, `DELETE`. Anything else — DDL, `MERGE`, transaction control,
pragmas — is `400 unsupported_sql` on both endpoints.

### 2.1 Parameters

Parameters are named: `@name` in the statement, values in the request's
`params` object. Positional placeholders (`?`) and other sigils (`:name`,
`$name`) **MUST** be rejected with `400 unsupported_sql` — they are not
GoogleSQL.

The set of names a statement references **MUST** equal the set of keys in
`params`; a missing or unused parameter is `400 invalid_request` naming the
parameter (names only — never values, §0).

### 2.2 Known semantic divergences (informative)

The intersection is syntactic; a statement can parse on both sides and still
behave differently. Applications should know about:

- **Division.** `/` on two integers is float division in GoogleSQL and
  integer division in SQLite. Avoid bare integer `/`: make one operand a
  float (`1.0 * x / y`), which reads the same on both sides.
- **Typing.** GoogleSQL columns are strictly typed; SQLite columns are
  affinities and will store what they are given. Constraint on the backend,
  not the wire; do not rely on the store to reject a wrongly-typed value.
- **Booleans.** `TRUE`/`FALSE` parse on both sides, but SQLite stores and
  returns them as `1`/`0`. Treat `0/1/false/true` interchangeably when
  reading boolean columns.
- **Integer precision.** Values are JSON numbers (§4); a client in a
  53-bit-number language corrupts integers above 2^53. Keep identifiers
  under 2^53 or carry them as strings in your schema.

---

## 3. Configuration

```toml
[modules.sql]
enabled = true
driver = "sqlite"
path = "/var/lib/app/app.sqlite"   # required by the sqlite driver
```

| Key | Type | Required | Notes |
|---|---|---|---|
| `driver` | string | no, default `"sqlite"` | This repository ships `sqlite`. An unavailable driver name **MUST** abort boot (SPEC.md §1.2), never serve a stand-in. |
| `path` | string | for `sqlite` | The database file; created on first use. A missing parent directory or unopenable file **MUST** abort boot, not serve errors. |

---

## 4. Values

The value vocabulary is the JSON scalars, both directions:

| JSON | SQL |
|---|---|
| `null` | `NULL` |
| `true` / `false` | `BOOL` (§2.2 for what comes back) |
| number (integral) | `INT64` |
| number (fractional) | `FLOAT64` |
| string | `STRING` |

Nothing else crosses the wire. A query whose result contains a value outside
this vocabulary (a BLOB, most commonly) is `400 sql_error`, not a lossy
encoding.

Result sets are `columns` (names, in select-list order) plus `rows` (arrays
in the same order). Column names for unaliased expressions are
backend-assigned and differ between backends; alias anything you intend to
read by name.

---

## 5. Endpoints

### 5.1 `POST /sql/v1/query` — one read

```json
{ "sql": "SELECT id, title FROM tasks WHERE owner = @owner", "params": { "owner": "avarma" } }
```

`200`:

```json
{ "columns": ["id", "title"], "rows": [[1, "write the spec"], [2, "ship it"]] }
```

### 5.2 `POST /sql/v1/exec` — one atomic write batch

```json
{
  "statements": [
    { "sql": "INSERT INTO tasks (title, owner) VALUES (@t, @o)", "params": { "t": "x", "o": "avarma" } },
    { "sql": "UPDATE counters SET n = n + 1 WHERE key = @k", "params": { "k": "tasks" } }
  ]
}
```

`200`: `{ "rowcounts": [1, 1] }` — affected rows, per statement, in order.

The batch is atomic: either every statement commits or none does (**MUST**).
An empty `statements` list is `400 invalid_request`. A constraint stopping
any statement is `409 constraint_violation` and rolls back the whole batch.

### 5.3 `POST /sql/v1/migrate` — sequential named migrations

Schema changes have no useful GoogleSQL∩SQLite form — the type systems do
not intersect — so migrations are written in the **backing store's own
dialect** (discover it via §5.4, ship one migration list per dialect) and
are not gated by §2. One statement per array entry.

```json
{
  "migrations": [
    { "name": "0001-tasks", "statements": ["CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, owner TEXT NOT NULL)"] },
    { "name": "0002-counters", "statements": ["CREATE TABLE counters (key TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0)"] }
  ]
}
```

`200`: `{ "applied": ["0002-counters"], "already_applied": ["0001-tasks"] }`

Semantics: the implementation keeps a ledger of applied migration names, in
order. The submitted list **MUST** begin with exactly the applied history —
same names, same order; anything else (a renamed, reordered, or removed
entry, or a ledger longer than the list) is `409 migration_conflict`, and
nothing is applied. The unapplied suffix is then applied in order, each
migration atomically. A failing migration stops the walk with `400
sql_error`: migrations before it in this request remain applied — each is
durable once applied — and the response is the error, never a
partial-success `200` (SPEC.md §1.2).

Sending the full list every time is therefore idempotent, and is the
intended calling convention: apply-on-startup, every startup.

### 5.4 `GET /sql/v1/backend` — dialect discovery

`200`: `{ "dialect": "sqlite" }`

Names the DDL dialect `/migrate` expects. Defined values: `"sqlite"`,
`"googlesql"`; a driver for another store defines its own name. Clients use
this to select a migration list, and for nothing else.

---

## 6. Errors

Additional codes beyond SPEC.md §3.1, all with `retryable: false`:

| `code` | Status | Meaning |
|---|---|---|
| `unsupported_sql` | 400 | Statement is outside the wire dialect (§2): not GoogleSQL, not single, wrong statement kind for the endpoint, or non-`@name` placeholders. |
| `sql_error` | 400 | The backing store rejected the statement, or the result left §4's value set. |
| `constraint_violation` | 409 | A uniqueness, foreign-key, or check constraint stopped a write. |
| `migration_conflict` | 409 | The submitted migration list contradicts the applied history (§5.3). |

Standard mapping:

| Condition | Response |
|---|---|
| Malformed body; unknown field; empty `exec` batch; parameter set mismatch (§2.1) | `400 invalid_request` |
| Backing store unreachable, locked out, out of space, or corrupt | `503 upstream_unavailable` |
| Fault within the sidecar | `500 internal_error` |
