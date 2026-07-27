# Design notes

Why the protocol is shaped the way it is. **Non-normative** — `SPEC.md` and the
module specifications under `spec/` are the contract, and they deliberately
contain no rationale. This file exists so the reasoning is not lost, and so that
a future change can tell whether it is overturning a considered decision or an
accident.

If this file and the spec disagree, the spec wins and this file is stale.

---

## Liveness and readiness are separate endpoints

`/healthz` performs no upstream checks; `/readyz` does.

A path named `healthz` gets wired to a container liveness probe. If it reported
upstream reachability, a directory outage would restart every sidecar in the
fleet at once. Restarting a stateless sidecar cannot repair a remote
dependency — it only destroys the component that was correctly reporting the
fault, and converts a partial outage into a crash-loop.

`/readyz` caches results briefly so that a fleet of frequent probes cannot
amplify into load on the directory it is checking.

## The error envelope has a closed code set

HTTP status codes alone cannot distinguish "the directory is down" from "that
module is not enabled", yet the two demand opposite responses from a client:
retry with backoff versus stop and reconfigure. `retryable` is carried
explicitly rather than inferred from the status, so a client never has to encode
a status-to-behaviour table that drifts from this one.

`message` is excluded from the contract deliberately. Anything clients are
permitted to parse becomes frozen; keeping the human-readable field explicitly
unstable preserves the freedom to improve it.

## Limits state what they count

"512 groups" and "512 *distinct* groups" differ for any client that does not
deduplicate. "256 characters" and "256 bytes" differ for any non-ASCII
identifier. Both ambiguities are easy to read past and produce implementations
that disagree only on inputs nobody tests.

Exceeding a limit is an error rather than a truncation because a truncated
authorization answer is indistinguishable from a negative one. It reads as
"access denied" — which looks like correct behaviour — until the day the dropped
entry was the one that would have granted access.

Group *member* enumeration is allowed to be partial because there the response
is inherently large, not the question. The `truncated` flag is what keeps that
from being a silent lie, which is why it is required to be honest rather than
merely present.

## Clients forward every header; the sidecar selects

An earlier design had the client name the headers to forward. That places the
gateway's header names in every client's configuration — precisely the internal
detail this project exists to keep out of them. Renaming a gateway header would
then break every client until each was reconfigured, and the failure presents as
an authentication outage rather than as configuration drift.

Selection belongs in the driver, which is the only component that knows what the
names mean and the only one that must be redeployed when they change.

**Accepted cost.** Headers the sidecar does not need cross the process boundary,
including `Cookie`. Client and sidecar are co-located in one trust domain, so
the alternative — a client-side filter — trades a recurring operational failure
for a marginal reduction in exposure inside a boundary that is already shared.
If that trade ever looks wrong, the change is small: a deny-list in the spec and
a filter in the client.

The credential-isolation rule therefore applies to *every* forwarded header, not
only to those a driver selects.

## Forwarded headers are pairs, not an object

HTTP permits repeated headers; a JSON object cannot represent them. Most parsers
silently keep the last occurrence.

That loss is security-relevant. If a gateway sets an identity header and a
caller also supplies one, an object representation discards the evidence that
anything was duplicated, and the sidecar cannot detect it. An array of pairs
preserves order and duplicates so that a driver can refuse.

A duplicated credential header is a hard `401` rather than a documented
precedence rule, because both plausible causes — a misconfigured gateway, or an
attempt to smuggle a claim past one — have no safe interpretation. "Pick one"
would turn an ambiguity into a silent, externally-influenced choice.

Whole-request forwarding raises the stakes on unverified claims rather than
lowering them: the forwarded set now includes headers an end user fully
controls, so the presence of a header conveys nothing at all.

## Membership is a closed question

There is no endpoint returning the groups a user belongs to. A full group list
describes an organization — its projects, reorganizations, security teams,
incident channels. A client that needs to know whether someone is in one
specific group does not need that, and an endpoint that exists will be used,
logged, and eventually exported.

Group *member* enumeration is the deliberate exception: assigning work to a team
genuinely requires knowing who is on it. The asymmetry is the point.

## Unknown group resolves to `false`, unknown user is `404`

A client's configuration will eventually reference a group that has been deleted
upstream. If that returned an error, one stale entry would break every request
that mentioned it. Resolving to `false` degrades a dead reference to "grants
nobody access", which is both safe and visible.

For group member enumeration the group id is the entire subject of the request,
so there is no degraded answer to give and `404` is correct.

Deactivated accounts still resolve on the membership endpoint because that
endpoint reports directory membership. Conflating "does not exist" with "may not
log in" would make auditing tools quietly wrong.

## No privileged groups

"Which group is special" is a fact about one application's resource model. Two
clients of the same sidecar will disagree about it, and a privileged group
configured centrally would widen every other client's access the moment someone
edited it. That fact belongs in each client's configuration, beside the
resources it protects.

## Unknown request fields are rejected

Everywhere else, tolerance of unknown fields is good practice. Here it is not:
if one implementation ignores a `groups` field that another honours, a client
written against the second silently loses its membership checks against the
first. Rejecting loudly turns a security bug into a startup failure.

The same reasoning applies to configuration keys and to unknown module names in
config — a typo in a module name would otherwise leave a security module quietly
disabled.

## Transport authority

Reaching the sidecar is itself a privilege, because it answers questions about
who holds which group. An unauthenticated TCP listener that will say whether a
named user is an administrator is a useful primitive for anything else running
on the host.

Unix sockets are the default so that filesystem permissions serve as the access
control and no token has to be distributed. TCP requires a token, and a
non-loopback bind requires an explicit opt-in so that it cannot happen by
copying a config.

## Implementation notes

Two behaviours in the reference implementation exist because of specific
failure modes found while running it:

- **The unix socket is bound directly rather than by the ASGI server.** uvicorn
  chmods a `uds=` socket to `0666` after binding, which would violate the
  0660-or-tighter rule while every unit test still passed. The socket is created
  and chmodded explicitly, then handed over as a file descriptor.
- **Framework 404s are translated into the spec envelope.** Unmatched routes
  otherwise return the framework's own `{"detail": ...}` shape — on exactly the
  paths a misconfigured client hits most.
