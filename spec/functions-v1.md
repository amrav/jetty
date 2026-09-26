# Jetty module: `functions` — v1

**Status: experimental.** This specification is subject to breaking changes
without warning: endpoints, wire shapes, and error codes may all change
while the `v1` path segment stays where it is. SPEC.md §6's freeze applies
to a *published* `api_version`, and this one is not published yet. Do not
build against it anything you are unwilling to update.

Mount: `/functions/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4, which this document does not restate.

The `functions` module calls a **named function** with an opaque request
payload and returns its opaque response payload. Jetty interprets neither.
It exists so that an application can reach a capability its own
specification defines before any Jetty module models it: the application
names the function, writes its request and response formats into its own
spec, and a driver on the sidecar side implements it — against a script in
development, against whatever the local environment provides internally.
Nothing routed through this module is part of Jetty's contract; each
function's meaning belongs entirely to the application that defined it.

This module is a staging area, not a destination. A function that stabilises
should graduate to a module of its own, with a specification under `spec/`,
at which point it leaves this surface.

Payloads routinely contain user data, and may contain whatever an
application chose to put in them, credentials included. An implementation
**MUST NOT** log request or response payloads at any level (SPEC.md §1.4
applied to data); function names **MAY** be logged.

---

## 1. Scope

In scope: one request/response call per function invocation; discovery of
the functions this sidecar provides.

Deliberately out of scope for v1: streaming in either direction; sessions
or state spanning calls; any interpretation of the payload, including
content negotiation, schema validation, or an error format inside it;
caller identity (an application that needs the caller's forwarded headers
carries them inside the payload, under its own spec); idempotency, which is
the application's to define for any function with side effects.

---

## 2. Function names

A function name **MUST** match `[a-z0-9_.-]{1,128}` — a single path
segment, so it never needs percent-encoding. Any other name is
`400 invalid_request`, before the driver is consulted.

Names are opaque to Jetty. The recommended convention is
`<application>.<function>[.v<N>]`: the leading segment keeps several
applications sharing one sidecar from colliding, and the application owns
versioning of its own functions, so a breaking change is a new name.

---

## 3. Configuration

```toml
[modules.functions]
enabled = true
driver = "exec"

[modules.functions.exec."planner.next_slot"]
command = ["/srv/hooks/planner-next-slot.py"]
timeout_s = 30
```

| Key | Type | Required | Notes |
|---|---|---|---|
| `driver` | string | no, default `"exec"` | A driver name this build registers, or a `package.module:factory` import path naming one (§6). This repository ships `exec`. A name that resolves to nothing **MUST** abort boot (SPEC.md §1.2), never serve a stand-in. |

Every other key in the table belongs to the selected driver, which
validates them; an unknown key **MUST** abort boot in the driver's schema
as it would in the module's. The `exec` driver's keys (§6.1):

| Key | Type | Required | Notes |
|---|---|---|---|
| `exec.<name>` | table | no | One entry per function the driver provides. A name outside §2 **MUST** abort boot. |
| `exec.<name>.command` | string[] | yes | Argument vector to run. An executable that cannot be found **MUST** abort boot, not fail every call. |
| `exec.<name>.timeout_s` | number | no, default `30` | Wall-clock budget for one call, greater than zero. |

A library-backed driver is selected the same way and reads its own keys:

```toml
[modules.functions]
enabled = true
driver = "planner_gateway.jetty:driver"

[modules.functions.gateway]
endpoint = "…"
```

---

## 4. Endpoints

Payloads cross **raw** — never JSON-wrapped, never base64. v1 imposes no
size ceiling; a payload crosses in one request and is held in memory at
both ends, which is the cost of having no streaming (§1).

### 4.1 `POST /functions/v1/call/{name}` — call one function

The request body is the request payload, raw. Any `Content-Type`, or none,
is accepted and not interpreted (this module's override of SPEC.md §2.2);
an empty body is a valid, empty payload.

`200`: the response payload, raw, as `application/octet-stream`. An empty
payload is a `200` with an empty body.

**The HTTP status is Jetty's layer; the bytes are the application's.** A
function that ran and produced a response payload is answered `200`
whatever that payload means — a failure the application's spec encodes
inside the payload is still a `200` here. Every non-`2xx` carries the
SPEC.md §3.1 envelope and reports a condition of Jetty or its driver (§5),
never a condition of the function's own semantics.

### 4.2 `GET /functions/v1/list` — the functions this sidecar provides

`200`:

```json
{ "functions": ["planner.book", "planner.next_slot"] }
```

Names in ascending order. This is `GET /v1/meta` applied one level down: a
client verifies at startup that the functions it requires exist, instead of
discovering a missing one mid-request. A name absent from this list is
answered `404 unknown_function` by §4.1.

---

## 5. Errors

One additional code beyond SPEC.md §3.1, `retryable: false`:

| `code` | Status | Meaning |
|---|---|---|
| `unknown_function` | 404 | No function of that name is provided by this sidecar's driver. Distinct from `not_found` so a client can feature-detect on it. |

Standard mapping:

| Condition | Response |
|---|---|
| Name outside §2 | `400 invalid_request` |
| The function ran and failed to produce a response (for `exec`: a non-zero exit) | `502 upstream_error` |
| The function could not be run or did not finish within its budget (for `exec`: the process could not be spawned, or the timeout elapsed) | `503 upstream_unavailable` |
| Fault within the sidecar | `500 internal_error` |

A driver failure is never a fabricated `200` (SPEC.md §1.2). Per SPEC.md
§1.2 a client treats `5xx` as *abort*; whether a function is safe to retry
is a fact about that function, defined by the application's spec.

---

## 6. Driver interface

The surface validates the wire contract — §2's name syntax — and
dispatches to a **driver**, which owns the set of functions it provides and
their execution.

```python
class FunctionDriver(Protocol):
    def functions(self) -> list[str]: ...
    async def call(self, name: str, payload: bytes) -> bytes: ...

DriverFactory = Callable[[Mapping[str, Any]], FunctionDriver]
```

A driver raises `UnknownFunction` for a name it does not provide, and
`FunctionFailed` when the function ran and produced no response; anything
else it raises is a backend that could not be reached, which the surface
maps to `503 upstream_unavailable` — never to a fabricated success
(SPEC.md §1.2). `call` is a coroutine; a driver doing blocking I/O keeps
it off the event loop.

A driver **MAY** define `async startup()` and `async shutdown()`; the
module awaits them at boot and at exit. A `startup` that raises aborts
boot — a driver that cannot open its backend refuses to serve rather than
serving errors forever (SPEC.md §1.2).

A driver is built by a **factory** that receives the whole
`[modules.functions]` table (§3) and returns the driver, or raises to abort
boot. The module finds a factory in one of two ways:

- **by registered name** — `jetty.modules.functions.module.register_driver(name, factory)`,
  for a build that vendors this repository and adds a driver of its own;
  `exec` is registered this way;
- **by import path** — `driver = "package.module:attribute"`, imported at
  boot, for a driver that ships as a library alongside the sidecar. The
  attribute **MUST** be a callable factory; a path that does not import or
  does not name one aborts boot.

Drivers defined alongside this document:

| Driver | Behaviour |
|---|---|
| `exec` | Runs one configured command per function (§6.1). Performs no network I/O of its own. |

Drivers for other backends implement the same Protocol privately, in
either form above, without modification to this module or its surface.

### 6.1 The `exec` driver

For each configured function (§3), a call runs `command` as a child
process with the sidecar's own process identity and environment, plus
`JETTY_FUNCTION` set to the function name so one program can serve several
functions. The request payload is written to the child's standard input,
which is then closed; standard output, read to end of file, is the response
payload.

| Child outcome | Response |
|---|---|
| Exit status `0` | `200` with standard output as the payload, empty or not |
| Any other exit status | `502 upstream_error`; standard output is discarded |
| Not finished within `timeout_s` | `503 upstream_unavailable`; the child is killed |
| Could not be spawned | `503 upstream_unavailable` |

The child's standard error is relayed to the sidecar's log — it is the
program's own output, and keeping credentials and payload content out of it
is the program author's responsibility (SPEC.md §1.4). Standard output is
never logged.

Because the command is configuration, and the sidecar's configuration is
written by the sidecar's operator, this driver introduces no execution
authority the operator did not already have; the same stance as the
`filesystem` module's use of the process identity.
