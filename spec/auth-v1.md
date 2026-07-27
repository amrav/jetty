# Jetty module: `auth` — v1

Mount: `/auth/v1` on the control listener · `required: true` by default
Depends on: [SPEC.md](../SPEC.md) §1–§4, which this document does not repeat.

The `auth` module answers two questions and nothing else:

- **Who is this?** — validate an assertion a gateway already made.
- **Are they in these groups?** — a closed-question membership check.

It never answers *may they do X*. Authorization stays in the client, which is
where the resource model lives (SPEC.md §7).

---

## 1. The closed-question rule

Every membership endpoint takes an explicit list of group identifiers and
returns one boolean per identifier. There is deliberately **no endpoint that
enumerates the groups a user belongs to**.

*Why:* a user's full group list is a map of the whole organization — projects,
reorgs, security teams, incident channels. A client that only needs "is this
person a hiring admin" does not need that, and once such an endpoint exists it
gets used, logged, and leaked. A closed question can be answered without
disclosing anything the caller did not already name.

Group **member** enumeration (§4) is the one carve-out, because assigning work
to a team genuinely requires knowing who is in it. It is deliberately asymmetric:
you may ask "who is in group G", never "what groups is user U in".

---

## 2. `POST /auth/v1/identify`

Validate forwarded gateway headers into an identity, and answer group questions
about the resulting user in the same round trip.

### Request

```json
{
  "headers": { "x-corp-user": "avarma", "x-corp-token": "…" },
  "groups": ["eng-hiring", "eng-all"]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `headers` | object(string→string) | yes | Verbatim headers the client received. Client **MUST** send an allowlist, never everything. Max 32 (SPEC.md §3.4). |
| `groups` | string[] | no, default `[]` | Group ids to check. Empty is valid and means *pure authentication*. |

`headers` **MUST** be present, even if empty — an absent `headers` key is
`400 invalid_request`, not an anonymous identify. The distinction matters:
`{"headers": {}}` is "I received no credentials" (→ `401`) whereas a missing key
is a malformed client.

### Response `200`

```json
{
  "username": "avarma",
  "name": "Anika Varma",
  "email": "avarma@corp.example",
  "groups": { "eng-hiring": true, "eng-all": false }
}
```

| Field | Type | Notes |
|---|---|---|
| `username` | string | **Canonical** form (SPEC.md §1.3). The sidecar's assertion, not an echo. |
| `name` | string | Display name. **MUST** be non-empty; implementations without a real one **MUST** fall back to `username`. |
| `email` | string \| **null** | Nullable — not every directory has one. Clients **MUST** handle `null`. |
| `groups` | object(string→bool) | One key per **distinct** requested id, echoed **byte-identically** to the request (SPEC.md §1.3). |

Four rules govern `groups`, all of which have bitten a previous implementation of
a similar contract and are therefore normative and conformance-tested:

1. **Empty in, empty out.** `"groups": []` → `"groups": {}` with a `200`. This is
   the pure-authentication call.
2. **Duplicates collapse.** `["g", "g"]` → exactly one key. The response
   **MUST NOT** contain more keys than there were distinct inputs.
3. **Unknown group ⇒ `false`, never an error.** A client's configuration may
   reference a group that has been deleted upstream. That grant must degrade to
   "grants nobody access", not break every request that mentions it. Returning
   `404` here would let one stale config entry take down an entire application.
4. **Transitive membership counts.** If the directory nests groups, a user in a
   child group **MUST** answer `true` for the parent. Clients cannot see the
   hierarchy and must not have to.

### Errors

| Status | `code` | When |
|---|---|---|
| 401 | `unauthenticated` | Headers absent, malformed, expired, or failing validation. |
| 400 | `invalid_request` | Missing `headers`, wrong types, limits exceeded. |
| 503 | `upstream_unavailable` | Directory unreachable or timed out. |

**A `401` MUST NOT evaluate groups**, and its body **MUST NOT** hint at group
membership. Authentication fails first and completely.

### The bare-header rule

An implementation **MUST NOT** treat an unverified identity claim as
authentication. A request whose `headers` contain only `{"x-corp-user": "alice"}`
— a name with no accompanying token, signature, or assertion — **MUST** return
`401`.

This is the single most important line in this document. Everything else here is
a convenience; this is the security boundary. Anything on the host can set a
header. Conformance test `identify_bare_header_rejected` enforces it by
stripping every header matching `/token|secret|signature|assertion/i` from an
otherwise-valid request and requiring a `401`.

---

## 3. `POST /auth/v1/users/{username}/membership`

The same closed question, for a user named directly rather than authenticated.
For administrative and auditing paths — "can the person I am about to assign
this to actually do it?"

`{username}` is percent-encoded in the path and matched canonically.

### Request / Response

```json
{ "groups": ["eng-hiring"] }
```

```json
{ "username": "bob", "groups": { "eng-hiring": true } }
```

`groups` follows every rule in §2. `username` in the response is canonical.

**Consistency requirement:** for the same user and group list, this endpoint
**MUST** return a `groups` map identical to `identify`'s. Two code paths that
disagree about membership is a privilege-escalation bug; the conformance suite
asserts them equal.

### Errors

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | No such user upstream. |
| 400 | `invalid_request` | Bad body or limits. |
| 503 | `upstream_unavailable` | Directory unreachable. |

A suspended or deactivated account **MUST** still resolve here rather than
`404`. This endpoint reports directory facts; blocking a login is the gateway's
job at identify time, and an application's own kill switch is its own business.
Conflating "does not exist" with "may not log in" makes audit tooling wrong.

Clients **SHOULD** treat `404` as "all requested groups are false" rather than
as a hard failure, so that a stale username in a config degrades the same way a
stale group id does (§2 rule 3).

---

## 4. `GET /auth/v1/groups/{group_id}/members`

Enumerate the members of one named group. The deliberate asymmetry from §1.

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
| `members[].name` | string | Non-empty; falls back to `username`. |
| `truncated` | bool | `true` iff the group has more members than the cap. |

- Members **MUST** be sorted by `username` (byte order on the canonical form),
  so clients can diff two responses without sorting.
- Transitive members **MUST** be included, consistently with §2 rule 4.
- The cap is **1000** members. Above it, return the first 1000 sorted and set
  `truncated: true`.

`truncated` is a real signal and **MUST** be honest — an implementation that
caps the list but hardcodes `truncated: false` is non-conformant. This is a
partial *answer*, not a truncated *question*, which is why it is allowed at all
(SPEC.md §3.4); the flag is what keeps it from being a silent lie.

Note that pagination is deliberately absent. A group with more than a thousand
members is not a paging problem to engineer around; it is a signal that the
client is using the wrong group, and the flag is there to say so.

### Errors

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | No such group. |
| 503 | `upstream_unavailable` | Directory unreachable. |

Unlike a membership *check* (§2 rule 3), an unknown group **is** a `404` here:
the group id is the entire subject of the request, so there is no useful
degraded answer.

---

## 5. Configuration

| Key | Default | Meaning |
|---|---|---|
| `auth.enabled` | `false` | Mount the module. |
| `auth.driver` | `mock` | Which upstream implementation to use. |
| `auth.required` | `true` | Counts toward `/readyz`. |
| `auth.timeout_ms` | `2000` | Upstream deadline. Directories are fast; slow means broken (SPEC.md §3.3). |
| `auth.max_groups` | `512` | SPEC.md §3.4. |
| `auth.members_cap` | `1000` | §4. |

### Drivers

The module is a protocol shell; a **driver** talks to a real directory.

```python
class AuthDriver(Protocol):
    async def identify(self, headers: Mapping[str, str]) -> Subject | None: ...
    async def member_of(self, username: str, groups: Sequence[str]) -> dict[str, bool]: ...
    async def group_members(self, group_id: str, cap: int) -> GroupMembers | None: ...
    async def ping(self) -> None: ...
```

`identify` returns `None` for "credentials did not validate" and **raises** for
"could not reach the directory" — the module maps the former to `401` and the
latter to `503`. Collapsing them would turn an outage into a mass logout, which
is precisely the fail-open this specification exists to prevent.

This repository ships:

- **`mock`** — deterministic personas from config, including nested groups. For
  development, CI, and running the conformance suite. Its credential convention
  is transparent by design and it **MUST NOT** be enabled in production; the
  implementation refuses to start if `mock` is combined with a non-loopback
  bind.
- **`static`** — reads users/groups from a signed file. Useful for small
  deployments with no directory at all.

A corp deployment supplies its own driver privately and the OSS protocol,
module, tests and conformance suite are unchanged.
