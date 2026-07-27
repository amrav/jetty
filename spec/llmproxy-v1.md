# Jetty module: `llmproxy` — v1

Mount: its **own listener**, default `http://127.0.0.1:7242` · `required: false`
Control-plane mount: `/llmproxy/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4.

Lets an OSS binary that already speaks a public LLM API — `google-genai`,
`openai`, `anthropic` — talk to an internal model gateway by changing one
environment variable (`base_url`) and nothing else.

---

## 1. Why a separate listener

Third-party SDKs hardcode their URL layout. `openai` insists on `/v1/chat/…`,
`google-genai` on `/v1beta/models/…`. Mounting those under Jetty's control
listener would collide with Jetty's own `/v1/meta`, and would mean an
unauthenticated model call and a privileged identity call share a socket and an
access-control decision.

So: the LLM surface gets its own listener, and Jetty's `/llmproxy/v1` on the
control listener carries only the *control plane* (§5). The listener address is
advertised in `GET /v1/meta` (SPEC.md §4.3).

**Foreign surfaces are not this specification.** `/openai/v1/chat/completions`
conforms to OpenAI's API, not to SPEC.md §3's error envelope, because the whole
point is that an unmodified SDK works. Errors on foreign surfaces **MUST** use
the emulated vendor's error shape. SPEC.md §1 (statelessness, fail-closed,
credential isolation) still applies.

---

## 2. Surfaces

Each is independently enableable; enabling none is a configuration error.

| Surface | Prefix | Emulates |
|---|---|---|
| `gemini` | `/genai/v1beta` | Google Generative Language API |
| `openai` | `/openai/v1` | OpenAI REST API |
| `anthropic` | `/anthropic/v1` | Anthropic Messages API |

### 2.1 `gemini`

- `POST /genai/v1beta/models/{model}:generateContent`
- `POST /genai/v1beta/models/{model}:streamGenerateContent` — SSE when `?alt=sse`
- `POST /genai/v1beta/models/{model}:embedContent`
- `POST /genai/v1beta/models/{model}:batchEmbedContents`
- `GET  /genai/v1beta/models` — model list, from the driver

### 2.2 `openai`

- `POST /openai/v1/chat/completions` — `stream: true` ⇒ SSE terminated by `data: [DONE]`
- `POST /openai/v1/embeddings`
- `GET  /openai/v1/models`

### 2.3 `anthropic`

- `POST /anthropic/v1/messages` — `stream: true` ⇒ SSE with Anthropic's typed
  events (`message_start`, `content_block_delta`, …)
- `GET  /anthropic/v1/models`

An enabled surface **MUST** implement every endpoint listed for it, or return
the vendor's own `404`/`not_found` shape for the ones it does not. It **MUST
NOT** return a Jetty error envelope on a foreign surface.

---

## 3. The driver interface

All three surfaces are translated into one internal representation and handed to
a **driver**. Adding a vendor surface must not require touching any driver, and
adding a driver must not require touching any surface.

```python
class LLMDriver(Protocol):
    async def generate(self, req: Generation) -> Completion: ...
    def stream(self, req: Generation) -> AsyncIterator[Chunk]: ...
    async def embed(self, req: Embedding) -> Embeddings: ...
    async def models(self) -> list[ModelInfo]: ...
    async def ping(self) -> None: ...
```

Shipped in this repository:

| Driver | Purpose |
|---|---|
| `mock` | Deterministic canned responses keyed by a hash of the request. No network. The conformance suite and every example run against this. |
| `passthrough` | Forwards to the real public vendor API using a configured key. Makes the OSS build independently useful. |

A corp driver — the actual reason Jetty exists — lives outside this repository
and implements the same Protocol.

### 3.1 Translation fidelity

Translation is **lossy in one direction only**: a request feature the driver
cannot express **MUST** be rejected, never silently dropped.

Dropping `temperature` produces plausible-looking output that is quietly wrong,
and no client can detect it. So an unsupported parameter is an error in the
emulated vendor's shape (`400`), naming the parameter. Implementations **MUST
NOT** offer a "best effort" mode.

Parameters with no cross-vendor equivalent (`logit_bias`, `top_k` on a surface
that lacks it) **MUST** be listed in `GET /llmproxy/v1/capabilities` (§5) so a
client can check before sending rather than discovering by failing.

### 3.2 Streaming

- SSE, `Content-Type: text/event-stream`, `Cache-Control: no-store`.
- Each surface uses its **own** event framing — Jetty does not invent one.
- A driver error **mid-stream** is emitted as that vendor's in-band error event
  and the stream is closed. It **MUST NOT** be a trailing silent close: a
  truncated completion that looks successful is the streaming equivalent of a
  fail-open.
- Client disconnect **MUST** cancel the upstream call. An abandoned generation
  that keeps billing is the most common way an LLM proxy wastes money.

---

## 4. Identity and quota

The proxy listener is local, but "local" is not "one user".

- If `auth` is also enabled, the proxy **MAY** be configured to require an
  identity, resolved through the same `auth` driver. It then attributes usage per
  user rather than per host.
- Otherwise every request is attributed to the configured service identity.

The proxy **MUST NOT** forward client-supplied vendor API keys upstream. An
`Authorization` or `x-api-key` header on an incoming proxy request is used only
for local admission control and **MUST** be stripped before the driver is
called. Forwarding it would let any local process spend an arbitrary key through
the sidecar.

---

## 5. Control plane

On the **control listener**, so it obeys SPEC.md §3 fully.

### `GET /llmproxy/v1/capabilities`

```json
{
  "driver": "mock",
  "surfaces": ["gemini", "openai", "anthropic"],
  "listener": "http://127.0.0.1:7242",
  "models": [
    { "id": "jetty-mock-large", "aliases": ["gpt-4o", "claude-sonnet-4-5", "gemini-2.5-pro"],
      "streaming": true, "embeddings": false,
      "unsupported_params": ["logit_bias"] }
  ]
}
```

Lets a client verify before its first real call that the model it wants exists
and that the parameters it intends to send are supported.

### `GET /llmproxy/v1/usage`

Aggregate counters since start — requests, tokens, errors, per model and per
identity when known. Counters only; **no prompt or completion content is ever
retained**, which is the same rule as SPEC.md §1.4 applied to payloads rather
than credentials.

---

## 6. Configuration

| Key | Default | Meaning |
|---|---|---|
| `llmproxy.enabled` | `false` | |
| `llmproxy.driver` | `mock` | |
| `llmproxy.listener` | `127.0.0.1:7242` | Non-loopback requires `allow_remote = true`. |
| `llmproxy.surfaces` | `["openai"]` | At least one. |
| `llmproxy.require_identity` | `false` | Needs `auth` enabled. |
| `llmproxy.timeout_s` | `120` | Generation deadline; far longer than §3.3's default because generation is legitimately slow. |
| `llmproxy.max_body_bytes` | `10485760` | 10 MiB — prompts are larger than control-plane bodies. |
