# Jetty Sidecar Protocol & API Specification

**Spec version: `1.0.0-draft`** · Status: draft · Licence: Apache-2.0

Jetty is a **sidecar**: a co-located process that lets an application talk to
internal infrastructure through a stable, vendor-neutral interface. The
application speaks this specification. An implementation of this specification
translates to whatever the local environment provides.

This document is the contract. It requires only HTTP, JSON, and standard status
codes, and names no vendor, product, or transport beyond those. It is normative;
`MUST`/`SHOULD`/`MAY` carry the meanings in §0.

---

## 0. Conformance language

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** are
to be interpreted as described in RFC 2119.

An implementation is **conformant** if, for every module it advertises in
`GET /v1/meta`, it satisfies this document and that module's specification.

---

## 1. Design invariants

These hold for every module.

### 1.1 Stateless integration

The sidecar **MUST NOT** retain user session state or credentials across
requests. Every identity assertion **MUST** be validated against the upstream
authority on every call.

An implementation **MAY** cache negative upstream *reachability* (circuit
breaking), provided it fails closed (§1.2) while the breaker is open.

Clients **MAY** cache responses under their own policy. The sidecar **MUST NOT**
direct that policy.

### 1.2 Fail-closed

Any upstream failure, timeout, or internal error **MUST** produce a `5xx`
response. An implementation **MUST NOT** return `200` with a degraded, partial,
or assumed result.

Specifically, an implementation **MUST NOT**:

- report "no groups" when the directory is unreachable;
- return a partial group map when only some lookups succeeded;
- present a partial result as a complete one.

Clients **MUST** treat any `5xx` as *deny* for authorization decisions and as
*abort* for data operations.

### 1.3 Canonical identifiers

Usernames and group identifiers are compared after canonicalization: **NFKC
normalization followed by casefolding**.

- The sidecar **MUST** compare canonically.
- The sidecar **MUST** echo the client's exact original string as the key in any
  response map. A request for `"Group-A"` is answered under the key `"Group-A"`.
- `username` in a response body **MUST** be the canonical form.

Clients **SHOULD** canonicalize before comparing identifiers obtained from
different sources.

### 1.4 Credential isolation

Tokens, signatures, cookies, and forwarded headers **MUST NOT** appear in logs,
traces, metrics labels, error messages, or health output, at any log level.
An implementation **MUST NOT** provide a setting that disables this.

Error bodies **MUST NOT** echo credential material, including when the
credential is the malformed input being reported.

### 1.5 Least authority at the transport

- Over a **unix domain socket** (the default), the socket file's permissions are
  the access control. The socket **MUST** be created with mode `0660` or
  tighter.
- Over **TCP**, this specification defines no transport authentication. An
  implementation **MUST NOT** bind a non-loopback address unless explicitly
  configured to, and a TCP listener is reachable by any process that can reach
  the bound address.

---

## 2. Transport

### 2.1 Listeners

An implementation binds one **control listener** carrying this protocol:
`/healthz`, `/v1/meta`, and each enabled module's
`/{module}/{api_version}/…` surface.

A module **MAY** declare an additional listener when it must serve a foreign
protocol whose URL layout would collide with this one. Foreign-protocol
listeners are outside the scope of this document except that §1 continues to
apply to them.

A module **MAY** instead serve a foreign protocol under its own mount prefix
on the control listener, when prefixing does not break that protocol's
clients. Such routes carry the foreign protocol's semantics — including its
error shape, in place of §3 — and §1 continues to apply to them.

| Listener | Default | Carries |
|---|---|---|
| control | `unix:/tmp/jetty/jetty.sock` | This specification |
| module-declared | per module | Foreign protocols |

An implementation **MUST** support unix domain sockets and **SHOULD** support
TCP.

### 2.2 Content type

Request and response bodies are `application/json; charset=utf-8` unless a
module specifies otherwise. A request carrying a body **MUST** set
`Content-Type: application/json`; an implementation **MUST** respond `415` if it
is absent or different.

---

## 3. Common semantics

### 3.1 Error envelope

Every non-2xx response **MUST** carry this body:

```json
{
  "error": {
    "code": "upstream_unavailable",
    "message": "directory did not respond within 2000ms",
    "retryable": true
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `code` | string | From the closed set below. Clients **MUST** switch on this and **MUST NOT** parse `message`. |
| `message` | string | Human-readable. **MUST NOT** contain credential material (§1.4). Not stable across versions. |
| `retryable` | bool | Whether an identical retry could plausibly succeed. |

| `code` | Status | `retryable` | Meaning |
|---|---|---|---|
| `unauthenticated` | 401 | false | Missing or invalid credential for Jetty itself, or unusable forwarded credentials. |
| `invalid_request` | 400 | false | Malformed body or unknown field. |
| `not_found` | 404 | false | Named user, group, or resource does not exist upstream. |
| `module_disabled` | 404 | false | Route belongs to a module that is not enabled. |
| `unsupported_media_type` | 415 | false | Body was not JSON. |
| `rate_limited` | 429 | true | Upstream or local throttle. `Retry-After` **SHOULD** be set. |
| `internal_error` | 500 | true | Fault within the sidecar. |
| `upstream_unavailable` | 503 | true | Upstream unreachable or refused. |
| `upstream_error` | 502 | true | Upstream responded unintelligibly. |

An implementation **MUST NOT** use codes outside this set for endpoints defined
in this document. A module **MAY** define additional codes for its own
endpoints, which **MUST** be listed in that module's specification.

### 3.2 Header forwarding

Modules that validate an assertion made by an upstream gateway require the
headers the client received. This section defines how those cross the boundary
and applies to every such module.

#### 3.2.1 Forwarding rule

The client **MUST** forward every header it received, verbatim and unfiltered.
The sidecar determines which of them are significant.

An implementation **MUST NOT** require the client to configure which headers to
send, and **MUST NOT** define a client-side allowlist.

Header selection is the responsibility of the sidecar's upstream driver.

§1.4 applies to every forwarded header, not only to those a driver selects.

#### 3.2.2 Wire format

`headers` is an array of `[name, value]` pairs:

```json
"headers": [
  ["x-gateway-user", "avarma"],
  ["x-gateway-assertion", "…"],
  ["accept", "application/json"]
]
```

Header names above are illustrative. No header name is significant to this
specification.

- Names **MUST** be lowercased by the client.
- Order **MUST** be preserved as received.
- Values **MUST** be transmitted verbatim, without trimming, decoding, or
  joining.
- Repeated headers **MUST** be preserved as separate entries. An implementation
  **MUST NOT** represent forwarded headers as a JSON object.
- The array **MAY** be empty, denoting that the client received no headers. Such
  a request is well-formed, and authentication fails.

#### 3.2.3 Duplicate credential headers

If a driver selects a header for authentication and that header appears more
than once, the request **MUST** fail `401 unauthenticated`. The implementation
**MUST NOT** select the first occurrence, the last occurrence, or a merge of
them.

---

## 4. Core API

### 4.1 `GET /healthz` — liveness

**MUST NOT** contact any upstream.

Returns `200` whenever the process is running and able to serve requests. This
endpoint reports process liveness only; it **MUST NOT** reflect upstream
availability.

```json
{ "ok": true, "spec_version": "1.0.0-draft", "uptime_s": 4211 }
```

### 4.2 `GET /v1/meta` — capability discovery

Reports the specification version and the enabled modules, so that a client can
verify at startup that the sidecar provides what it requires.

```json
{
  "spec_version": "1.0.0-draft",
  "implementation": { "name": "jetty-oss", "version": "0.1.0" },
  "modules": [
    { "name": "auth",     "api_version": "v1", "mount": "/auth" },
    { "name": "llmproxy", "api_version": "v1", "mount": "/llmproxy",
      "listener": "http://127.0.0.1:7242" }
  ]
}
```

`implementation.name` and `implementation.version` are free-form; clients
**MUST NOT** branch on them. Only `spec_version` and `modules[].api_version` are
contractual.

`modules[].listener` **MUST** be present when the module declares its own
listener (§2.1) and absent otherwise.

### 4.3 Disabled modules

A request to a disabled module's mount **MUST** return `404` with code
`module_disabled`. An implementation **MUST NOT** return `501`.

`GET /v1/meta` is the defined means of discovering which modules are available.

---

## 5. Modules

Every capability is a module that can be independently enabled or disabled.

A module specification **MUST** state: its name, its mount prefix, its API
version, its configuration keys, its endpoints, and any additional error codes.

Module routes are served at `/{module}/{api_version}/…` on the control listener.
API versions are per module: `auth` may reach `v2` while `llmproxy` remains at
`v1`.

Modules defined alongside this document:

| Module | Specification | Purpose |
|---|---|---|
| `auth` | `spec/auth-v1.md` | Identity assertion and group membership |
| `llmproxy` | `spec/llmproxy-v1.md` | LLM API surfaces over a pluggable driver |
| `chat` | `spec/chat-v1.md` | Google Chat API subset over a pluggable driver |
| `hg` | `spec/hg-v1.md` | Read-only view of the Mercurial repositories under a configured root |
| `issuetracker` | `spec/issuetracker-v1.md` | Google Issue Tracker API subset over a pluggable driver |
| `mail` | `spec/mail-v1.md` | Outbound mail relay over a pluggable driver |
| `sql` | `spec/sql-v1.md` | Relational storage in the GoogleSQL ∩ SQLite dialect over a pluggable driver (experimental: breaking changes without warning) |
| `filesystem` | `spec/filesystem-v1.md` | Whole-file read/write with unix semantics under a configured root, over a pluggable driver (experimental: breaking changes without warning) |

`xmanager` is a reserved name with no specification yet.

---

## 6. Versioning

- **Spec version** is semver. Additive changes bump the minor version. Breaking
  changes bump the major version and **MUST** introduce a new per-module
  `api_version` path segment rather than altering an existing one.
- A module's `api_version` is frozen once published. Adding an optional request
  field or a new response field is additive. Changing a field's type, removing a
  field, tightening validation, or changing the error code for an existing
  condition is breaking.
- Clients **MUST** ignore unknown fields in responses.
- Implementations **MUST** reject unknown fields in requests with
  `400 invalid_request`.

---

## 7. Non-goals

- **Not a policy engine.** Jetty reports who a caller is and which named groups
  they belong to. It does not decide what they may do. Authorization decisions
  remain with the client.

  Jetty has no notion of a privileged group: no superadmin group, no admin role,
  no group that is significant to Jetty itself. Every group is an opaque
  identifier to be resolved and reported. An implementation **MUST NOT** accept
  configuration naming a distinguished group, and a driver **MUST NOT** treat
  any group differently from any other.

- **Not a cache or a database** (§1.1).

- **Not a service mesh.** One co-located process, one trust domain.

- **Not a credential broker for end users.** Jetty validates assertions issued
  by a gateway; it does not perform interactive login.
