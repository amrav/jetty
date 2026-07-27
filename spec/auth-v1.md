# Jetty module: `auth` — v1

Mount: `/auth/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4, which this document does not restate.

The `auth` module resolves two things:

- **Identity** — validating an assertion issued by an upstream gateway.
- **Group membership** — answering, for a named set of groups, whether the
  subject belongs to each.

It does not make authorization decisions (SPEC.md §7).

---

## 1. Closed-question membership

Every membership endpoint takes an explicit list of group identifiers and
returns one boolean per identifier.

This module **MUST NOT** expose an endpoint that enumerates the groups a user
belongs to.

Enumerating the **members of a named group** is defined in §4. A caller may
therefore ask which subjects belong to a group it names, and may not ask which
groups a named subject belongs to.

---

## 2. `POST /auth/v1/identify`

Validates forwarded gateway headers into an identity and answers group questions
about the resulting subject in one round trip.

### Request

```json
{
  "headers": [
    ["x-gateway-user", "avarma"],
    ["x-gateway-assertion", "…"],
    ["accept", "application/json"],
    ["user-agent", "example-client/0.1.0"]
  ],
  "groups": ["eng-hiring", "eng-all"]
}
```

Header names above are illustrative; no header name is significant to this
specification.

| Field | Type | Required | Notes |
|---|---|---|---|
| `headers` | array of `[name, value]` | yes | Every header the client received, verbatim and unfiltered (SPEC.md §3.2). Lowercased names, order preserved, duplicates retained. |
| `groups` | string[] | no, default `[]` | Group identifiers to check. |

An implementation **MUST NOT** offer configuration that selects which headers
the client sends (SPEC.md §3.2.1). Selecting the credential from the forwarded
set is the driver's responsibility.

`headers` **MUST** be present. An absent `headers` key is `400 invalid_request`.
An empty array is valid and denotes that the client received no headers.

If a header the driver selects for authentication appears more than once, the
request **MUST** fail `401` (SPEC.md §3.2.3).

### Response `200`

```json
{
  "username": "avarma",
  "name": "Anika Varma",
  "email": "avarma@example.internal",
  "groups": { "eng-hiring": true, "eng-all": false }
}
```

| Field | Type | Notes |
|---|---|---|
| `username` | string | Canonical form (SPEC.md §1.3). |
| `name` | string | Display name. **MUST** be non-empty; an implementation without one **MUST** substitute `username`. |
| `email` | string \| null | Nullable. Clients **MUST** handle `null`. |
| `groups` | object(string→bool) | One key per distinct requested identifier, echoed byte-identically (SPEC.md §1.3). |

The `groups` map obeys four rules:

1. **Empty request, empty map.** `"groups": []` **MUST** yield `"groups": {}`
   with status `200`, authenticating without any membership check.
2. **Duplicates collapse.** A repeated identifier **MUST** produce exactly one
   key. The response **MUST NOT** contain more keys than there were distinct
   requested identifiers.
3. **Unknown group resolves to `false`.** A group that does not exist upstream
   **MUST** be reported as `false`. It **MUST NOT** produce an error.
4. **Transitive membership counts.** Where the directory nests groups, a subject
   in a child group **MUST** be reported `true` for the parent.

### Errors

| Status | `code` | When |
|---|---|---|
| 401 | `unauthenticated` | Headers absent, malformed, expired, or failing validation. |
| 400 | `invalid_request` | Missing `headers` or wrong types. |
| 503 | `upstream_unavailable` | Directory unreachable or timed out. |

A `401` **MUST NOT** evaluate group membership, and its body **MUST NOT**
disclose any membership information.

### 2.1 Unverified identity claims

An implementation **MUST NOT** derive an identity from any header it has not
verified. A request whose headers assert a username without an accompanying
token, signature, or assertion **MUST** return `401`.

Because the client forwards every header it received (SPEC.md §3.2.1), the
forwarded set includes headers under end-user control. The presence of a header
therefore confers no authority. A driver **MUST** treat the forwarded set as
untrusted input from which a credential is verified.

Conformance test `identify_bare_header_rejected` removes every header whose name
matches `/token|secret|signature|assertion/i` from an otherwise valid request
and requires `401`.

---

## 3. `POST /auth/v1/users/{username}/membership`

Answers the same closed question for a directly named user rather than an
authenticated one.

`{username}` is percent-encoded in the path and matched canonically (SPEC.md
§1.3).

### Request / Response

```json
{ "groups": ["eng-hiring"] }
```

```json
{ "username": "bob", "groups": { "eng-hiring": true } }
```

`groups` obeys the four rules in §2. `username` is canonical.

For the same subject and group list, this endpoint **MUST** return a `groups`
map identical to the one `identify` returns.

### Errors

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | No such user upstream. |
| 400 | `invalid_request` | Malformed body. |
| 503 | `upstream_unavailable` | Directory unreachable. |

A suspended or deactivated account **MUST** resolve normally rather than `404`.
This endpoint reports directory membership; account status is not in its scope.

Clients **SHOULD** treat `404` as equivalent to all requested groups being
`false`.

---

## 4. `GET /auth/v1/groups/{group_id}/members`

Enumerates the members of one named group (§1).

`{group_id}` is percent-encoded in the path and matched canonically.

### Response `200`

```json
{
  "members": [
    { "username": "avarma", "name": "Anika Varma" },
    { "username": "bwu",    "name": "Bo Wu" }
  ],
  "truncated": false
}
```

| Field | Type | Notes |
|---|---|---|
| `members[].username` | string | Canonical. |
| `members[].name` | string | Non-empty; substitutes `username` when unavailable. |
| `truncated` | bool | `true` when the group has more members than the cap. |

- Members **MUST** be sorted by `username`, byte order on the canonical form.
- Transitive members **MUST** be included, consistently with §2 rule 4.
- The cap is **1000** members. Above it, an implementation **MUST** return the
  first 1000 in sort order and **MUST** set `truncated: true`.
- `truncated` **MUST** reflect the actual result. An implementation that applies
  the cap while reporting `truncated: false` is non-conformant.

This endpoint defines no pagination.

### Errors

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | No such group. |
| 503 | `upstream_unavailable` | Directory unreachable. |

An unknown group **MUST** be `404` here, in contrast to a membership check,
where it resolves to `false` (§2 rule 3).

---

## 5. Configuration

| Key | Default | Meaning |
|---|---|---|
| `auth.enabled` | `false` | Mount the module. |
| `auth.driver` | `mock` | Upstream driver to use. |
| `auth.members_cap` | `1000` | §4. |

This module defines no key naming a distinguished group, and an implementation
**MUST NOT** add one (SPEC.md §7). Every group identifier it handles is opaque.

### Drivers

The module is a protocol shell; a **driver** resolves identity and membership
against a real directory.

```python
class AuthDriver(Protocol):
    async def identify(self, headers: Headers) -> Subject | None: ...
    async def member_of(self, username: str, groups: Sequence[str]) -> dict[str, bool]: ...
    async def group_members(self, group_id: str, cap: int) -> GroupMembers | None: ...
    async def ping(self) -> None: ...
```

`Headers` is an ordered, duplicate-preserving sequence of `(name, value)`, not a
mapping, so that a driver can detect the condition in SPEC.md §3.2.3. It exposes
`get_all(name) -> list[str]` and `sole(name) -> str | None`, where `sole` raises
if the header appears more than once. It exposes no single-value indexing
operator.

Header selection is internal to the driver. Changing which header names are
recognised **MUST NOT** require reconfiguring any client.

`identify` **MUST** return `None` when credentials fail validation and **MUST**
raise when the directory is unreachable. The module maps the former to `401` and
the latter to `503`; an implementation **MUST NOT** conflate them.

Drivers defined alongside this document:

| Driver | Behaviour |
|---|---|
| `mock` | Deterministic subjects and nested groups from configuration, for development, CI, and conformance runs. Its credential scheme is transparent by construction; an implementation **MUST** refuse to start when `mock` is combined with a non-loopback bind. |
| `static` | Subjects and groups read from a signed file, for deployments with no directory service. |

Additional drivers implement the same Protocol without modification to this
module, its endpoints, or the conformance suite.
