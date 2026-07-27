# Jetty Sidecar Protocol & API Specification

**Spec version: `1.0.0-draft`** · Status: draft · Licence: Apache-2.0

Jetty is a **sidecar** that lets open-source binaries talk to internal corporate
infrastructure without knowing anything about it. The OSS client speaks this
specification; a private implementation of the same specification does whatever
the corp environment requires. Neither side leaks into the other.

This document is the contract. It is deliberately implementable by anyone: plain
HTTP, JSON, standard status codes, no vendor identifiers, no proprietary
transports. A conformance suite (`conformance/`) executes this document against
any implementation.

---

## 0. Conformance language

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** are
to be interpreted as described in RFC 2119.

An implementation is **conformant** if it passes the conformance suite for every
module it claims to support in `GET /v1/meta`.

---

## 1. Design invariants

These hold for every module. A module that cannot honour them does not belong in
Jetty.

### 1.1 Stateless integration

The sidecar **MUST NOT** retain user session state or credentials across
requests. Every identity assertion is validated against the upstream authority
live, on every call.

*Why:* the sidecar is not a second source of truth. If it cached, revoking access
upstream would leave a window in which the sidecar still says yes — and the
client has no way to know how stale the answer is. Callers may cache the
*answer* under their own policy; the sidecar never decides that for them.

An implementation **MAY** cache negative upstream *reachability* (circuit
breaking) as long as it fails **closed** while the breaker is open.

### 1.2 Fail-closed

Any upstream failure, timeout, or internal error **MUST** result in a `5xx`
response, never a `200` carrying a degraded or guessed answer.

Specifically, an implementation **MUST NOT**:

- return "no groups" when the directory is unreachable,
- return a partial group map when only some lookups succeeded,
- silently truncate a request that exceeds a documented limit.

The third is the one that bites: a truncated authorization answer is
indistinguishable from a negative one, so it reads as "access denied" until the
day it reads as "access granted". Exceeding a limit is a `400`, never a trim.
See §3.4.

Clients **MUST** treat any `5xx` as *deny* for authorization decisions and as
*abort* for data operations.

### 1.3 Canonical identifiers

Usernames and group identifiers are compared after canonicalization:
**NFKC normalize, then casefold** (Unicode-aware lowercase).

Two rules that together resolve an ambiguity in most directory APIs:

- The sidecar **MUST** compare canonically.
- The sidecar **MUST** echo the client's **exact original string** as the key in
  any response map.

So a client that asks about `"Group-A"` gets back `{"Group-A": true}` — it can
look up its own key without re-deriving the canonical form. Clients **SHOULD**
still canonicalize before comparing identifiers they got from two different
sources.

`username` in a **response body** is always the canonical form, because it is
the sidecar's own assertion rather than an echo of client input.

### 1.4 Credential isolation

Tokens, signatures, cookies and forwarded headers **MUST NOT** appear in logs,
traces, metrics labels, error messages, or health output — at any log level,
including debug. Implementations **MUST NOT** provide a configuration flag that
disables this.

Error bodies returned to clients **MUST NOT** echo credential material back,
even when the credential is what was malformed.

### 1.5 Least authority at the transport

Jetty exposes authorization answers, so reaching Jetty is itself a privilege.

- Over **UDS** (default and recommended), the socket file's permissions are the
  access control. The socket **MUST** be created with mode `0660` or tighter.
- Over **TCP**, the implementation **MUST** require a bearer token
  (§2.3) and **MUST** refuse to bind a non-loopback address unless explicitly
  configured to do so.

*Why this is in the spec at all:* the draft of this document omitted it, and an
unauthenticated TCP listener that answers "is alice an admin?" is a privilege
escalation primitive for anything else on the box.

---

## 2. Transport

### 2.1 Listeners

Jetty binds one **control listener** carrying the Jetty protocol itself
(`/healthz`, `/readyz`, `/v1/meta`, and each module's `/{module}/v1/...`
surface).

A module **MAY** additionally request its own listener when it must speak a
*foreign* protocol that would collide with Jetty's own routes — the LLM proxy
(§5) is the motivating case, because third-party SDKs insist on owning the URL
root. Foreign-protocol listeners are not covered by this specification beyond
§1: they implement whatever upstream API they emulate.

| Listener | Default | Purpose |
|---|---|---|
| control | `unix:/run/jetty/jetty.sock` | This spec |
| module-specific | per module | Foreign protocols |

Implementations **MUST** support UDS and **SHOULD** support TCP.

### 2.2 Content type

Request and response bodies are `application/json; charset=utf-8` unless a
module documents otherwise (SSE in §5). Requests with a body **MUST** set
`Content-Type: application/json`; an implementation **MUST** respond `415` if it
is absent or different.

Bodies are capped at **1 MiB** on the control listener; exceeding it is `413`.

### 2.3 Authenticating to Jetty

On a TCP control listener, clients **MUST** send:

```
Authorization: Bearer <token>
```

The token is shared configuration between the sidecar and its co-located client
(a file, an env var, a k8s secret — out of scope here). A missing or wrong token
is `401` with code `unauthenticated`. Comparison **MUST** be constant-time.

On a UDS control listener the token is **OPTIONAL** and disabled by default;
filesystem permissions already bound access to the same trust domain.

`/healthz` is exempt (§4.1) so that a supervisor can probe liveness without
holding a credential.

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
| `code` | string | From the closed set below. Clients switch on this, never on `message`. |
| `message` | string | Human-readable, for logs. **MUST NOT** contain credentials (§1.4). Not stable across versions. |
| `retryable` | bool | Whether an identical retry could plausibly succeed. |

*Why a closed set:* the draft specified status codes only. A `503` from "the
directory is down" and a `503` from "this module is disabled" demand completely
different client behaviour — retry versus reconfigure — and status codes alone
cannot express that.

| `code` | Status | `retryable` | Meaning |
|---|---|---|---|
| `unauthenticated` | 401 | false | Missing/invalid credential for Jetty itself, or unusable forwarded credentials. |
| `invalid_request` | 400 | false | Malformed body, unknown field, limit exceeded. |
| `not_found` | 404 | false | Named user/group/resource does not exist upstream. |
| `module_disabled` | 404 | false | Route belongs to a module that is not enabled. |
| `unsupported_media_type` | 415 | false | Body was not JSON. |
| `payload_too_large` | 413 | false | Body over the cap. |
| `rate_limited` | 429 | true | Upstream or local throttle. `Retry-After` **SHOULD** be set. |
| `internal_error` | 500 | true | Bug in the sidecar. |
| `upstream_unavailable` | 503 | true | Upstream unreachable, refused, or timed out. |
| `upstream_error` | 502 | true | Upstream answered, unintelligibly. |

An implementation **MUST NOT** invent codes outside this set for the endpoints
this document defines. Modules **MAY** define additional codes for their own
endpoints, documented in their section.

### 3.2 Request identity and tracing

Clients **MAY** send `X-Request-Id`. The sidecar **MUST** echo it on the
response, and **MUST** generate one if absent. It appears in sidecar logs and is
the join key when debugging across the client/sidecar boundary.

### 3.3 Timeouts

The sidecar **MUST** apply its own deadline to every upstream call and **MUST**
convert a breach into `503 upstream_unavailable` rather than hanging. The
default deadline **SHOULD** be 5s and **MUST** be configurable.

Clients **SHOULD** set a timeout strictly greater than the sidecar's.

### 3.4 Limits

| Limit | Default | Counted over | On exceed |
|---|---|---|---|
| `groups[]` per request | 512 | **distinct** entries after canonicalization | `400 invalid_request` |
| Identifier length | 256 **bytes**, UTF-8 encoded | each raw entry as sent | `400 invalid_request` |
| Forwarded headers per request | 128 | header entries, duplicates counted separately | `400 invalid_request` |
| Forwarded header bytes | 64 KiB | sum of all name+value bytes | `400 invalid_request` |
| Request body | 1 MiB | encoded bytes | `413 payload_too_large` |

The two header limits are sized for **whole-request forwarding** (§3.5): clients
send every header they received rather than a configured subset, so the caps
have to accommodate a browser's full header set plus a gateway's additions.
A single `Cookie` header can be several KiB on its own, which is why there is a
byte cap as well as a count.

Both "counted over" columns are load-bearing and are stated because leaving them
implicit has already caused divergence in a prior implementation of a similar
contract: "512 entries" and "512 *distinct* entries" differ for any client that
does not deduplicate, and "256 characters" and "256 bytes" differ for any
non-ASCII identifier. Implementations **MUST** count as specified here.

Limits **MUST** be reported in `GET /v1/meta` so clients can batch correctly
instead of discovering a cap by tripping it.

Exceeding a limit is **always** an error, never a silent truncation (§1.2).
Where a response is *inherently* partial — enumerating a large group — that is
signalled explicitly by a `truncated` flag (§4.3.3).

---

### 3.5 Header forwarding

Modules that validate an assertion made by an upstream gateway need the headers
the client received. This section defines how they cross the boundary, because
getting it wrong is a security bug and every such module gets it identically.

#### The rule

**The client forwards every header it received, verbatim and unfiltered. The
sidecar decides which ones mean anything.**

Clients **MUST NOT** be required to configure which headers to send, and
implementations **MUST NOT** define a client-side allowlist.

*Why:* the header names a corp gateway uses are exactly the kind of internal
detail Jetty exists to hide. An OSS binary configured with
`forward_headers = x-corp-user,x-corp-token` has corp topology baked into its
deployment, and the day that gateway renames a header, every OSS consumer needs
reconfiguring — and until they do, authentication fails in a way that looks like
an outage rather than a config drift. Selection belongs in the driver, next to
the knowledge of what the names mean.

The cost is that headers the sidecar does not need cross the process boundary,
including `Cookie`. This is accepted deliberately: client and sidecar are
co-located in one trust domain (§1.5), and the alternative — a client-side
filter — trades a real, recurring operational failure for a marginal reduction
in exposure within a boundary that is already shared. §1.4's prohibition on
logging credential material applies to every forwarded header, not just the
ones a driver selects.

#### Wire format

`headers` is an **array of `[name, value]` pairs**, not an object:

```json
"headers": [
  ["x-corp-user", "avarma"],
  ["x-corp-token", "…"],
  ["accept", "application/json"]
]
```

An object would collapse repeated headers, and HTTP permits repeats. That
collapse is security-relevant: if a gateway sets `x-corp-user` and an attacker
also sends one, an object silently keeps one of them — most JSON parsers keep
the last — and the sidecar can no longer tell that anything was duplicated. The
array preserves both order and duplicates so the driver can refuse.

- Names **MUST** be lowercased by the client (HTTP header names are
  case-insensitive; lowercasing makes driver matching exact).
- Order **MUST** be preserved as received.
- Values **MUST** be sent verbatim — no trimming, decoding, or joining.
- The array **MAY** be empty, meaning "I received no headers". It is still a
  well-formed request, and authentication simply fails.

#### Duplicate credential headers

If a driver selects a header for authentication and that header appears **more
than once**, the request **MUST** fail `401 unauthenticated`. It **MUST NOT**
pick the first, the last, or attempt to merge.

A duplicated identity header means either a misconfigured gateway or an attempt
to smuggle one past it. Neither has a safe interpretation, and "pick one" turns
an ambiguity into a silent, attacker-influenced choice. Failing closed here is
the only defensible behaviour.

---

## 4. Core API

### 4.1 `GET /healthz` — liveness

Unauthenticated. **MUST NOT** contact any upstream.

Returns `200` with `{"ok": true}` whenever the process is running and able to
serve. It answers exactly one question: *should my supervisor restart me?*

*Why this differs from the draft:* the draft's `/healthz` performed upstream
reachability checks and returned `503` when they failed. Wired to a container
liveness probe — which is what a path named `healthz` gets wired to — that
restarts the sidecar every time the corp directory has a bad minute. Restarting
a stateless sidecar cannot fix a remote outage; it just removes the component
that was correctly reporting the problem, and does it fleet-wide and
simultaneously. Liveness and readiness are split below.

```json
{ "ok": true, "spec_version": "1.0.0-draft", "uptime_s": 4211 }
```

### 4.2 `GET /readyz` — readiness

Unauthenticated by default (it exposes no identity data). Performs each enabled
module's upstream reachability check, subject to a short internal cache
(**SHOULD** be ~5s) so that probes cannot amplify into upstream load.

- `200` — every module reporting `required: true` is ready.
- `503` — at least one required module is not ready. Body still lists all
  modules; readiness of an *optional* module never fails the probe.

```json
{
  "ok": false,
  "modules": {
    "auth":     { "ready": false, "required": true,  "detail": "upstream_unavailable" },
    "llmproxy": { "ready": true,  "required": false, "detail": null }
  }
}
```

`detail` **MUST** be one of the `code` values in §3.1, or `null`. It **MUST
NOT** be free text (that would leak internal topology, and clients would parse
it anyway).

### 4.3 `GET /v1/meta` — capability discovery

The feature-detection endpoint. Lets a client verify at startup that the sidecar
it found actually speaks what it needs, instead of discovering a missing module
via a 404 in the middle of a user request.

```json
{
  "spec_version": "1.0.0-draft",
  "implementation": { "name": "jetty-oss", "version": "0.1.0" },
  "modules": [
    { "name": "auth",     "api_version": "v1", "mount": "/auth",     "required": true },
    { "name": "llmproxy", "api_version": "v1", "mount": "/llmproxy", "required": false,
      "listener": "http://127.0.0.1:7242" }
  ],
  "limits": { "groups_per_request": 512, "identifier_length": 256,
              "headers_per_request": 128, "header_bytes": 65536,
              "body_bytes": 1048576 }
}
```

`implementation.name` is free-form and **MAY** identify a private build; clients
**MUST NOT** branch on it. Only `spec_version` and `modules[].api_version` are
contractual.

### 4.4 Disabled modules

A request to a disabled module's mount returns `404` with code
`module_disabled` — not `501`. From the client's perspective the route does not
exist; `/v1/meta` is the supported way to learn what is available.

---

## 5. Modules

Jetty is a shell; every capability is a module that can be independently enabled
or disabled. This first release specifies two. `filesystem` and `xmanager` are
reserved names, not yet specified.

A module definition **MUST** state: its name, its mount prefix, its API version,
whether it is `required` for readiness, its configuration keys, its endpoints,
and any additional error codes.

Module routes live at **`/{module}/{api_version}/...`** on the control listener —
so `/auth/v1/identify`. Versioning is per module: `auth` can reach `v2` while
`llmproxy` stays at `v1`.

- **§5.1 `auth`** — identity assertion and group membership. *Specified in
  `spec/auth-v1.md`.*
- **§5.2 `llmproxy`** — LLM API surfaces (Gemini / OpenAI / Anthropic wire
  formats) over a pluggable upstream driver, on its own listener. *Specified in
  `spec/llmproxy-v1.md`.*

Both are stubs in this repository pending review of this core document; the
module contract they implement is `jetty.modules.base.Module`.

---

## 6. Versioning

- **Spec version** is semver. Additive changes bump minor; breaking changes bump
  major and **MUST** introduce a new per-module `api_version` path segment
  rather than mutating an existing one.
- A module's `v1` is frozen once published. Adding an **optional** request field
  or a **new** response field is additive and allowed. Changing a field's type,
  removing a field, tightening validation, or changing an error code for an
  existing condition is breaking.
- Clients **MUST** ignore unknown fields in responses.
- Implementations **MUST** reject unknown fields in *requests* with
  `400 invalid_request`, so that a client relying on a field a given
  implementation ignores fails loudly rather than silently losing meaning. This
  is the one place where strictness beats tolerance: an ignored `groups` field
  in an authorization request is a security bug.

---

## 7. Non-goals

- **Not a policy engine.** Jetty answers "who is this, what are they in". It
  never answers "may they". Authorization decisions stay in the client, where
  the resource model lives.
- **Not a cache or a database.** §1.1.
- **Not a service mesh.** One process, co-located, one trust domain.
- **Not a credential broker for end users.** It validates assertions that a
  gateway already made; it does not perform interactive login.
