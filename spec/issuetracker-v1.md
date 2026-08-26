# Jetty module: `issuetracker` — v1

Mount: `/issuetracker` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4.
Status: **experimental** — this module is subject to breaking changes without
warning: the surface, driver interface, and configuration may change
incompatibly under the same `api_version`, and SPEC.md §6's freeze does not
apply until this notice is removed.

Serves a subset of the Google Issue Tracker REST API (v1) so that a client or
library written against `issuetracker.googleapis.com` can reach an internal
issue tracker by changing only its base URL.

---

## 1. Provenance of the emulated interface

Google publishes no reference documentation for this API. The emulated
interface is `google.devtools.issuetracker.v1.IssueTracker`, whose only public
definition is the generated protobuf code vendored in the Chromium LUCI
repository:

- `third_party/google.golang.org/genproto/googleapis/devtools/issuetracker/v1/issuetracker.pb.go`
  (messages) and `issuetracker_service.pb.go` (service and HTTP bindings) in
  [`infra/luci/luci-go`](https://chromium.googlesource.com/infra/luci/luci-go/+/refs/heads/main/third_party/google.golang.org/genproto/googleapis/devtools/issuetracker/v1/).
- `go.chromium.org/luci/analysis/internal/bugs/buganizer` — a production
  consumer of the interface, useful as a worked example of client behaviour.

The REST method table in §4 is the `google.api.http` bindings carried in those
descriptors, verbatim. Where this document states field names, enum values, or
request semantics, they come from the same descriptors; the vendored copy is
itself a subset of Google's internal definition, and this module emulates the
vendored copy, not the internal superset.

This section is informative. The normative surface is what §3–§7 state.

---

## 2. Mount

The issue-tracker surface is a foreign protocol served under the module's
mount prefix on the control listener (SPEC.md §2.1). The emulated API's URL
layout appears verbatim below the prefix: `/issuetracker/v1/…`. This is what a
stock client of the emulated API produces when its base URL is set to the
sidecar's address plus `/issuetracker`.

The module declares no listener of its own; `modules[].listener` is absent in
`GET /v1/meta` (SPEC.md §4.2).

The surface conforms to the API it emulates, not to SPEC.md §3. Error
responses **MUST** use the emulated API's error shape and **MUST NOT** use the
envelope in SPEC.md §3.1:

```json
{ "error": { "code": 404, "message": "issue not found", "status": "NOT_FOUND" } }
```

SPEC.md §1 applies to the surface in full.

---

## 3. No authentication

This module performs **no authentication**. Identity, authorization, and
auditing for issue-tracker operations happen elsewhere in the deployment; by
the time a request reaches this surface it is trusted. The transport is the
access control (SPEC.md §1.5): a unix socket's file mode, or a loopback bind.

Consequently:

- The surface **MUST NOT** require OAuth, an API key, or any credential.
- Stock clients of the emulated API attach an `Authorization` header, an
  `x-goog-api-key` header, or a `key` query parameter. The surface **MUST**
  accept all three and, by default, ignore them: **MUST NOT** forward them to
  the driver or any upstream (SPEC.md §1.4 applies to them regardless).
- `forward_headers` (configuration; default empty) names incoming request
  headers the surface passes to the driver **verbatim**, bound to the request
  they arrived on. This is for deployments whose upstream authenticates or
  attributes each caller: the caller supplies its own credential and the
  driver presents it upstream. Headers not named are never forwarded, and the
  forwarded values **MUST NOT** be logged or echoed in any response
  (SPEC.md §1.4). With `forward_headers` empty the previous rule applies in
  full.
- The module defines no per-caller identity. Where the emulated API attributes
  an action to the calling user (`reporter` of a created issue, `author` of a
  comment or issue update), the attribution is the driver's concern (e.g. a
  configured service identity). Reads report these fields exactly as the
  upstream records them, never a value the implementation synthesises.

---

## 4. Emulated surface

A subset of the Google Issue Tracker REST API v1 (§1). An enabled module
**MUST** implement every method below. A request matching the emulated API's
URL layout but not this subset **MUST** receive the emulated API's error with
status `UNIMPLEMENTED` (code `501`); an unrecognisable path is `NOT_FOUND`.

| Method | HTTP |
|---|---|
| `components.get` | `GET /issuetracker/v1/components/{componentId}` |
| `issues.list` | `GET /issuetracker/v1/issues` |
| `issues.batchGet` | `GET /issuetracker/v1/issues:batchGet` |
| `issues.get` | `GET /issuetracker/v1/issues/{issueId}` |
| `issues.create` | `POST /issuetracker/v1/issues` |
| `issues.modify` | `POST /issuetracker/v1/issues/{issueId}:modify` |
| `issues.relationships.create` | `POST /issuetracker/v1/issues/{issueId}/relationships` |
| `issues.relationships.list` | `GET /issuetracker/v1/issues/{issueId}/relationships` |
| `issues.issueUpdates.list` | `GET /issuetracker/v1/issues/{issueId}/issueUpdates` |
| `issues.comments.create` | `POST /issuetracker/v1/issues/{issueId}/comments` |
| `issues.comments.list` | `GET /issuetracker/v1/issues/{issueId}/comments` |
| `issues.comments.update` | `PUT /issuetracker/v1/issues/{issueId}/comments/{commentNumber}` |
| `issues.attachments.list` | `GET /issuetracker/v1/issues/{issueId}/attachments` |
| `hotlists.entries.create` | `POST /issuetracker/v1/hotlists/{hotlistId}/entries` |
| `hotlists.entries.delete` | `DELETE /issuetracker/v1/hotlists/{hotlistId}/entries/{issueId}` |

Identifiers (`issueId`, `componentId`, `hotlistId`) are decimal integers, and
`commentNumber` is the comment's 1-based position within its issue, exactly as
the emulated API defines them. There are no resource-name strings in this API;
the mount prefix exists in URLs only.

### 4.1 Issue shape

An issue's mutable fields live in `issueState`; the enclosing `Issue` carries
identity, audit, and derived data. The field vocabulary is the emulated
API's (§1):

- `issueState.componentId`, `issueState.title` — required at creation.
- `issueState.type` — `BUG`, `FEATURE_REQUEST`, `CUSTOMER_ISSUE`,
  `INTERNAL_CLEANUP`, `PROCESS`, `VULNERABILITY`, `PRIVACY_ISSUE`,
  `PORTFOLIO`, `PROGRAM`, `PROJECT`, `FEATURE`, `MILESTONE`, `EPIC`, `STORY`,
  `TASK`.
- `issueState.status` — `NEW`, `ASSIGNED`, `ACCEPTED`, `FIXED`, `VERIFIED`,
  `NOT_REPRODUCIBLE`, `INTENDED_BEHAVIOR`, `OBSOLETE`, `INFEASIBLE`,
  `DUPLICATE`, `INACTIVE`.
- `issueState.priority` — `P0`–`P4`. `issueState.severity` — `S0`–`S4`.
- `issueState.reporter`, `assignee`, `verifier` — User objects
  (`emailAddress`); `ccs`, `collaborators` — lists of the same.
- `issueState.blockedByIssueIds`, `blockingIssueIds`, `duplicateIssueIds`,
  `canonicalIssueId`, `hotlistIds` — cross-references by ID.
- `Issue.issueId`, `createdTime`, `modifiedTime`, `resolvedTime`,
  `verifiedTime` (RFC-3339), `lastModifier`, `description` (comment 1),
  `parentIssueIds`, `customFields`, `etag`.

### 4.2 Listing and searching

`issues.list` supports the emulated API's parameters, and an implementation
**MUST** honour all of them:

- `query` — required; the same query language as the emulated API's search
  box (e.g. `componentid:1396945 status:open p:p1`). The subset of the
  language a driver supports is driver-defined, but a query term the driver
  cannot evaluate follows §4.6 — it is never silently dropped.
- `orderBy` — a sort field with optional direction, ascending by default,
  e.g. `priority asc, created desc`. Sortable fields follow the emulated API
  (priority, severity, created, modified_time, assignee, …) — note the
  tracker's sort field is `modified_time` although the response key is
  `modifiedTime`; an implementation **MUST NOT** accept spellings the
  emulated API rejects.
- `pageSize` (default 25, maximum 500) and `pageToken`; responses carry
  `nextPageToken` when more results exist and `totalSize` as an
  approximation.
- `view` — `BASIC` (default) or `FULL`, controlling issue payload size.

`issues.batchGet` takes repeated `issueIds` parameters plus `view`, and
returns the issues that exist; per the emulated API, an unknown ID in the
batch is omitted from the response, not an error.

### 4.3 Mutation

`issues.create` takes an `issue` whose `issueState` names at minimum
`componentId`, `title`, `status`, `type`, `priority`, and `severity`;
`description` seeds comment 1. Rejection of an incomplete issue is the
emulated `INVALID_ARGUMENT` error.

`issues.modify` is the emulated API's masked read-modify-write, not a PATCH:
the body carries `add` + `addMask` (an `IssueState` holding new values and
the field mask naming which of them apply), `remove` + `removeMask` (values
to remove from collection fields), and an optional `issueComment` recorded
atomically with the change. Fields outside the masks **MUST** be left
untouched. A modification that names no masked field and carries no comment
is the emulated `INVALID_ARGUMENT` error.

`issues.comments.create` appends a comment; `issues.comments.update` replaces
the body of comment `{commentNumber}`. A comment carries `author` — the
upstream's recorded commenter, in the same user shape as `reporter` and
`assignee` — absent when the upstream records none; per §3 it is reported
exactly as recorded, never synthesised. The upstream's comment history
semantics (whether prior versions remain visible) are the upstream's; the
surface does not restate them.

`issues.relationships.create` and `issues.relationships.list` take a
`relationshipType` (`CHILD`, `DEPENDENCY`, `LINKED`); list responses carry
`issueRelationships` with `targetIssueId` and, at the emulated API's option,
an embedded `targetIssue`.

`hotlists.entries.create` bodies carry a `hotlistEntry` with `issueId` and
optional `position`; delete removes the issue from the hotlist. Hotlists
themselves are not creatable, listable, or deletable through this surface.

### 4.4 History

`issues.issueUpdates.list` returns the issue's change history: `issueUpdates`
entries with `author`, `timestamp`, and `fieldUpdates` (each naming a `field`
with old/new values for scalars, added/removed values for collections),
paginated like §4.2 with a `sortBy` parameter.

### 4.5 Attachments

`issues.attachments.list` returns attachment metadata: `attachmentId`,
`filename`, `contentType`, `length`, `attachmentDataRef.resourceName`.
Attachment bytes are not served: the emulated interface (§1) defines no
download or upload binding, and v1 of this module adds none.

### 4.6 Translation fidelity

If a request specifies a field, parameter, or query term the driver cannot
honour, the implementation **MUST** reject the request with the emulated
API's `INVALID_ARGUMENT` error, naming the field. It **MUST NOT** drop,
substitute, or approximate it, and **MUST NOT** offer a mode that does so.

### 4.7 Read fidelity

Every issue a response carries — from list, batchGet, get, create, or
modify — **MUST** include the following whenever the upstream records them:
`issueId`, `issueState.componentId`, `issueState.type`, `issueState.status`,
`issueState.priority`, `issueState.severity`, `issueState.title`,
`issueState.reporter`, `issueState.assignee`, `createdTime`, `modifiedTime`.
Comments are held to the same rule for `issueId`, `commentNumber`, and
`comment`. An implementation **MUST NOT** omit, blank, or approximate one of
these fields when the upstream provides it; a field the upstream genuinely
lacks is absent, per the emulated API.

---

## 5. Driver interface

The surface translates requests into one internal representation and
dispatches to a **driver**.

```python
class IssueTrackerDriver(Protocol):
    async def get_component(self, component_id: int) -> Component | None: ...
    async def list_issues(self, query: IssueQuery) -> IssuePage: ...
    async def batch_get_issues(self, issue_ids: list[int], view: View) -> list[Issue]: ...
    async def get_issue(self, issue_id: int, view: View) -> Issue | None: ...
    async def create_issue(self, req: IssueCreate) -> Issue: ...
    async def modify_issue(self, issue_id: int, req: IssueModify) -> Issue | None: ...
    async def create_relationship(self, issue_id: int, rel: RelationshipCreate) -> IssueRelationship: ...
    async def list_relationships(self, issue_id: int, type: RelationshipType) -> list[IssueRelationship]: ...
    async def list_issue_updates(self, issue_id: int, page: Page) -> IssueUpdatePage: ...
    async def create_comment(self, issue_id: int, comment: str) -> IssueComment: ...
    async def list_comments(self, issue_id: int, page: Page) -> IssueCommentPage: ...
    async def update_comment(self, issue_id: int, number: int, comment: str) -> IssueComment | None: ...
    async def list_attachments(self, issue_id: int) -> list[Attachment]: ...
    async def create_hotlist_entry(self, hotlist_id: int, entry: HotlistEntryCreate) -> HotlistEntry: ...
    async def delete_hotlist_entry(self, hotlist_id: int, issue_id: int) -> bool: ...
    async def ping(self) -> None: ...
```

`IssueQuery` carries §4.2's constraints beside pagination: the query string,
the ordering, and the view. `IssueModify` carries the two masked states and
the optional comment of §4.3.

A driver returns `None`/`False` for a resource that does not exist and raises
for an unreachable upstream. The surface maps the former to the emulated
`NOT_FOUND` and the latter to the emulated `UNAVAILABLE`; an implementation
**MUST NOT** conflate them (SPEC.md §1.2).

Drivers defined alongside this document:

| Driver | Behaviour |
|---|---|
| `mock` | In-memory components, issues, comments, and hotlists seeded from configuration, for development, CI, and conformance runs. Performs no network I/O. Supports the query terms `componentid:`, `status:`, `assignee:`, `p:`, and bare-word title match. |
| `passthrough` | Forwards to a real Issue Tracker endpoint (`issuetracker.googleapis.com` or a deployment-specific equivalent) using a configured service credential. Access to that endpoint is allowlisted by Google per automation identity and component; obtaining it is a deployment concern, not this module's. |

Additional drivers implement the same Protocol without modification to this
module or its surface.

---

## 6. Configuration

| Key | Default | Meaning |
|---|---|---|
| `issuetracker.enabled` | `false` | Mount the module. |
| `issuetracker.driver` | `mock` | Upstream driver to use. |
| `issuetracker.identity` | `jetty@example.com` | The service identity mutations are attributed to (§3). |
| `issuetracker.seed` | empty | Mock-driver seed data: `seed.components` (component_id, name) and `seed.issues` (component_id, title, status, type, priority, severity, reporter, assignee, description). Other drivers ignore it. |
