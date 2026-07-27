# jetty

A modular **sidecar** that lets open-source binaries talk to internal corporate
infrastructure without knowing anything about it.

The OSS binary speaks a published, vendor-neutral protocol
([`SPEC.md`](SPEC.md)). A private implementation of that same protocol does
whatever the corp environment actually requires — LDAP, an internal model
gateway, a bespoke auth mesh. Neither side leaks into the other, so the OSS
project stays fully usable and testable outside the company, and the internal
details stay internal.

```
┌──────────────┐   published protocol    ┌────────────┐   whatever it takes   ┌──────────┐
│  OSS binary  │ ──────────────────────► │   jetty    │ ────────────────────► │   corp   │
│  (any lang)  │   HTTP+JSON over UDS    │  sidecar   │   private drivers     │  infra   │
└──────────────┘                         └────────────┘                       └──────────┘
```

## Status

**Draft — core is implemented and runnable; modules are specified, not built.**

| Piece | State |
|---|---|
| Protocol spec (`SPEC.md`) | Written, for review |
| Core: config, registry, listeners, `/healthz` `/readyz` `/v1/meta`, error envelope | **Implemented, tested** |
| `reference` module (worked example + test fixture) | **Implemented, tested** |
| `auth` module ([`spec/auth-v1.md`](spec/auth-v1.md)) | Specified, not implemented |
| `llmproxy` module ([`spec/llmproxy-v1.md`](spec/llmproxy-v1.md)) | Specified, not implemented |
| `filesystem`, `xmanager` | Names reserved only |

Enabling an unimplemented module fails at boot with `unknown module` rather than
serving stub answers — the correct fail-closed behaviour for a security
component.

## Quick start

```sh
uv venv && uv pip install -e '.[dev]'

sed 's|/run/jetty|/tmp/jetty|' jetty.example.toml > /tmp/jetty.toml
.venv/bin/jetty --config /tmp/jetty.toml --check     # validate, don't bind
.venv/bin/jetty --config /tmp/jetty.toml             # serve

curl --unix-socket /tmp/jetty/jetty.sock http://localhost/v1/meta
```

```sh
pytest        # core semantics + a real process on a real socket
```

## Design in one page

Everything is a module. The core is a shell that owns config, listeners, the
error envelope and three endpoints, and knows nothing about auth or LLMs. A
module supplies a name, an API version, a router, and a readiness check — that
is the whole contract ([`modules/base.py`](src/jetty/modules/base.py)).

Four choices worth knowing before reading the code:

- **Nothing is enabled by default, and a disabled module is never imported.** A
  sidecar that answers questions nobody configured it to answer is a liability.
- **An unknown module name in config is a boot failure**, not a warning. A typo
  in `[modules.ath]` would otherwise silently leave auth off.
- **`/healthz` never touches an upstream.** It answers only "should my
  supervisor restart me?". Readiness — which does check upstreams — is
  `/readyz`. Conflating them means a remote outage restarts your whole fleet,
  which cannot help and removes the component that was correctly reporting the
  problem.
- **Fail closed, everywhere.** Any upstream failure is a `5xx`; a limit breach
  is a `400`, never a silent truncation. A truncated authorization answer is
  indistinguishable from a negative one until the day it reads as positive.

## Notable changes from the original API draft

The substantive ones, each argued in place in `SPEC.md`:

| Change | Why |
|---|---|
| `/healthz` split into `/healthz` + `/readyz` | The draft's upstream-checking `/healthz`, wired to a liveness probe, restarts the fleet during a directory blip. |
| Structured error envelope, closed `code` set | The draft specified status codes only. "Directory is down" and "module is disabled" demand opposite client behaviour; a status code alone cannot say which. |
| `GET /v1/meta` added | Feature-detection at startup, instead of discovering a missing module via a 404 mid-request. |
| Transport auth specified | The draft never said who may call the sidecar. An unauthenticated TCP listener that answers "is alice an admin?" is a privilege-escalation primitive. |
| Clients forward **every** header; the sidecar selects | The draft had the client name `x-proxy-auth-token`, baking corp topology into every OSS deployment — the exact coupling jetty exists to remove. A gateway rename would then break every consumer until each was reconfigured. |
| `headers` is an array of `[name, value]` pairs, not an object | An object collapses repeated headers, and a repeated identity header is either a broken gateway or a smuggling attempt. Duplicates of a selected credential are now a hard `401` rather than a silent pick. |
| `gemini_api` renamed `llmproxy` | It serves Gemini, OpenAI *and* Anthropic shapes; naming it after one vendor misleads. |
| Canonicalization contradiction resolved | The draft said identifiers are lowercased *and* echoed exactly. Now: compare canonically, echo the caller's bytes. |
| Limits state what they count | "512 groups" vs "512 *distinct* groups", "256 characters" vs "256 bytes". |
| Unknown group ⇒ `false`, not an error | A deleted group in a client's config must degrade to "grants nobody access", not break every request that mentions it. |
| No endpoint enumerating a *user's* groups | A closed question can be answered without disclosing an org chart. Group **member** listing stays, as a deliberate asymmetry. |

## Licence

Apache-2.0.
