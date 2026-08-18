# jetty

[![CI](https://github.com/amrav/jetty/actions/workflows/ci.yml/badge.svg)](https://github.com/amrav/jetty/actions/workflows/ci.yml)

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
| Protocol spec (`SPEC.md`) — normative, standalone | Written, for review |
| Rationale ([`DESIGN-NOTES.md`](DESIGN-NOTES.md)) — non-normative | Written |
| Core: config, registry, listeners, `/healthz` `/v1/meta`, error envelope | **Implemented, tested** |
| `reference` module (worked example + test fixture) | **Implemented, tested** |
| `hg` module ([`spec/hg-v1.md`](spec/hg-v1.md)) — read-only Mercurial API | **Implemented, tested** |
| `auth` module ([`spec/auth-v1.md`](spec/auth-v1.md)) | Specified, not implemented |
| `llmproxy` module ([`spec/llmproxy-v1.md`](spec/llmproxy-v1.md)) | Specified, not implemented |
| `chat` module ([`spec/chat-v1.md`](spec/chat-v1.md)) | **Implemented, tested** (`mock` driver; `passthrough` specified only) |
| `issuetracker` module ([`spec/issuetracker-v1.md`](spec/issuetracker-v1.md)) — emulated Google Issue Tracker API (**experimental**) | **Implemented, tested** (`mock` driver; `passthrough` specified only) |
| `mail` module ([`spec/mail-v1.md`](spec/mail-v1.md)) — outbound mail relay | **Implemented, tested** (`spool` driver; delivery drivers are private) |
| `filesystem`, `xmanager` | Names reserved only |
| Orchestrator `jetty-orc` ([`docs/orchestrator.md`](docs/orchestrator.md)) — companion tool, not a module | **Implemented, tested** |

Enabling an unimplemented module fails at boot with `unknown module` rather than
serving stub answers — the correct fail-closed behaviour for a security
component.

## Quick start

```sh
uv sync --extra dev        # .venv from the committed uv.lock

.venv/bin/jetty --config jetty.example.toml --check   # validate, don't bind
.venv/bin/jetty --config jetty.example.toml          # serve

curl --unix-socket /tmp/jetty/jetty.sock http://localhost/v1/meta
```

Dependencies are locked: `uv.lock` is committed, CI installs with
`uv sync --locked`, and changing `pyproject.toml` means re-running `uv lock`
and committing the result.

Tests are [absltest](https://abseil.io/docs/python/guides/testing); each file is
also a standalone binary. Temp files, sockets and config all live under the
directory absltest hands out, so nothing assumes a writable `/tmp`.

```sh
.venv/bin/python tests/run_all.py         # whole suite
.venv/bin/python tests/test_listener.py   # one file, absl flags apply
```

`run_all.py` rather than `unittest discover`: unittest never parses absl's
flags, so every test that asks for a temp path would fail on `--test_tmpdir`.

## Orchestrator (`jetty-orc`)

`jetty.orchestrator` is a separate, standalone package in this repo —
**stdlib-only** (test-enforced), no jetty-core imports, all-relative
intra-package imports — so it vendors into another build system at any module
path, and a bare copy of the directory runs directly on any Linux box with
Python 3.11+ (`scp -r`, then `python3 <dir> up -c orc.toml`), nothing to
install. It launches and supervises a named *instance* of a multi-process
stack, Linux only:

- kernel-backed containment — an owned cgroup v2 subtree (one subgroup per
  service, `cgroup.kill` teardown, memory/CPU accounting), acquired directly
  when running under a delegated unit or via a `systemd-run --user --scope
  -p Delegate=yes` re-exec; plain process groups as the fallback. Ctrl-C, a
  crash of the supervisor (children get `PR_SET_PDEATHSIG`), or `jetty-orc
  kill` take down the whole tree, double-forked grandchildren included;
- a port broker: `{ports.<name>}` placeholders resolve to free ports at
  launch and are injected into commands, environments and readiness probes;
- binary resolvers: a script names the current binary path(s) — several may
  be pinned together atomically — and `{bin.<name>}` re-resolves before each
  spawn, so a restart picks up a new release without config edits;
- per-service restart policy (bounded restarts + backoff; exhaustion fails
  the instance loudly with the log tail) and *gates* — external condition
  probes (credentials, VPN) that park a crashing service in `blocked` without
  burning restart budget, reviving it when the condition clears;
- a registry: `jetty-orc ls` shows every instance with health, ports, process
  count and memory/CPU; `status` breaks a stack down per service.

```sh
.venv/bin/jetty-orc doctor                              # what this host offers
.venv/bin/jetty-orc check -c orchestrator.example.toml  # validate, spawn nothing
.venv/bin/jetty-orc up -c orchestrator.example.toml     # run in the foreground
.venv/bin/jetty-orc ls                                  # from anywhere
```

See [`docs/orchestrator.md`](docs/orchestrator.md) for full docs (config reference,
lifecycle and exit classification, containment levels, registry,
troubleshooting) and `orchestrator.example.toml` for a worked config.

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
  supervisor restart me?". A liveness probe that reported upstream health would
  restart the whole fleet during a remote outage, which cannot help and removes
  the component that was correctly reporting the problem.
- **Fail closed, everywhere.** Any upstream failure is a `5xx`, never a `200`
  carrying a degraded or partial answer. A partial authorization result is
  indistinguishable from a negative one until the day it reads as positive.

## Notable changes from the original API draft

`SPEC.md` and the module specifications are normative and carry no rationale, so
that they stand alone as a contract. The reasoning behind each decision —
including every row below — is in [`DESIGN-NOTES.md`](DESIGN-NOTES.md), which is
explicitly non-normative.

The substantive changes:

| Change | Why |
|---|---|
| `/healthz` reports liveness only, never upstream health | The draft's upstream-checking `/healthz`, wired to a liveness probe, restarts the fleet during a directory blip. |
| Structured error envelope, closed `code` set | The draft specified status codes only. "Directory is down" and "module is disabled" demand opposite client behaviour; a status code alone cannot say which. |
| `GET /v1/meta` added | Feature-detection at startup, instead of discovering a missing module via a 404 mid-request. |
| Transport auth specified | The draft never said who may call the sidecar. An unauthenticated TCP listener that answers "is alice an admin?" is a privilege-escalation primitive. |
| Clients forward **every** header; the sidecar selects | The draft had the client name `x-proxy-auth-token`, baking corp topology into every OSS deployment — the exact coupling jetty exists to remove. A gateway rename would then break every consumer until each was reconfigured. |
| `headers` is an array of `[name, value]` pairs, not an object | An object collapses repeated headers, and a repeated identity header is either a broken gateway or a smuggling attempt. Duplicates of a selected credential are now a hard `401` rather than a silent pick. |
| `gemini_api` renamed `llmproxy` | It serves Gemini, OpenAI *and* Anthropic shapes; naming it after one vendor misleads. |
| Canonicalization contradiction resolved | The draft said identifiers are lowercased *and* echoed exactly. Now: compare canonically, echo the caller's bytes. |
| Unknown group ⇒ `false`, not an error | A deleted group in a client's config must degrade to "grants nobody access", not break every request that mentions it. |
| No endpoint enumerating a *user's* groups | A closed question can be answered without disclosing an org chart. Group **member** listing stays, as a deliberate asymmetry. |

## Licence

Apache-2.0.
