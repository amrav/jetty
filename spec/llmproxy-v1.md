# Jetty module: `llmproxy` — v1

Mount: a module-declared listener, default `http://127.0.0.1:7242`
Control-plane mount: `/llmproxy/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4.

Serves widely-implemented LLM HTTP APIs so that a client configured with a
`base_url` can reach an internal model service without further modification.

---

## 1. Listeners

The LLM surfaces are served on a module-declared listener (SPEC.md §2.1),
separate from the control listener, because the URL layouts they emulate would
otherwise collide with this specification's own routes.

The listener address **MUST** be reported as `modules[].listener` in
`GET /v1/meta` (SPEC.md §4.2).

The surfaces in §2 conform to the APIs they emulate, not to SPEC.md §3. Error
responses on those surfaces **MUST** use the emulated API's error shape and
**MUST NOT** use the envelope in SPEC.md §3.1. SPEC.md §1 applies to them in
full.

The control-plane endpoints in §5 are served on the control listener and
conform to SPEC.md §3 in full.

---

## 2. Surfaces

Each surface is independently enableable. An implementation **MUST** reject a
configuration that enables the module with no surfaces.

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
- `GET  /genai/v1beta/models`

### 2.2 `openai`

- `POST /openai/v1/chat/completions` — SSE when `stream: true`, terminated by `data: [DONE]`
- `POST /openai/v1/embeddings`
- `GET  /openai/v1/models`

### 2.3 `anthropic`

- `POST /anthropic/v1/messages` — SSE when `stream: true`, using the emulated
  API's typed events (`message_start`, `content_block_delta`, and so on)
- `GET  /anthropic/v1/models`

An enabled surface **MUST** implement every endpoint listed for it, or return
the emulated API's own not-found response for those it does not.

---

## 3. Driver interface

Every surface translates requests into one internal representation and
dispatches to a **driver**.

```python
class LLMDriver(Protocol):
    async def generate(self, req: Generation) -> Completion: ...
    def stream(self, req: Generation) -> AsyncIterator[Chunk]: ...
    async def embed(self, req: Embedding) -> Embeddings: ...
    async def models(self) -> list[ModelInfo]: ...
    async def ping(self) -> None: ...
```

Adding a surface **MUST NOT** require changes to any driver, and adding a driver
**MUST NOT** require changes to any surface.

Drivers defined alongside this document:

| Driver | Behaviour |
|---|---|
| `mock` | Deterministic responses derived from a hash of the request. Performs no network I/O. |
| `passthrough` | Forwards to the emulated vendor's public API using a configured key. |

### 3.1 Translation fidelity

If a request specifies a parameter the driver cannot honour, the implementation
**MUST** reject the request with the emulated API's `400`-equivalent error,
naming the parameter. It **MUST NOT** drop, substitute, or approximate the
parameter, and **MUST NOT** offer a mode that does so.

Parameters with no equivalent on a given surface **MUST** be reported in
`GET /llmproxy/v1/capabilities` (§5).

### 3.2 Streaming

- Streaming responses use SSE with `Content-Type: text/event-stream` and
  `Cache-Control: no-store`.
- Each surface **MUST** use the event framing of the API it emulates.
- A driver error occurring mid-stream **MUST** be emitted as that API's in-band
  error event before the stream closes. An implementation **MUST NOT** close the
  stream silently, which would present a truncated result as a complete one.
- Client disconnection **MUST** cancel the corresponding upstream call.

---

## 4. Identity and credentials

- When the `auth` module is enabled, this module **MAY** be configured to
  require an identity, resolved through the `auth` driver, and attribute usage
  to it.
- Otherwise requests are attributed to the configured service identity.

An implementation **MUST NOT** forward client-supplied vendor API keys upstream.
An `Authorization` or `x-api-key` header on an incoming request to a surface
listener is used only for local admission control and **MUST** be removed before
the driver is invoked.

---

## 5. Control plane

Served on the control listener; conforms to SPEC.md §3.

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

Reports the available models, their aliases, and the parameters each does not
support, so that a client can determine compatibility before issuing a request.

### `GET /llmproxy/v1/usage`

Aggregate counters since process start: requests, tokens, and errors, broken
down by model and, where known, by identity.

An implementation **MUST NOT** retain prompt or completion content. This
endpoint reports counters only.

---

## 6. Configuration

| Key | Default | Meaning |
|---|---|---|
| `llmproxy.enabled` | `false` | Mount the module. |
| `llmproxy.driver` | `mock` | Upstream driver to use. |
| `llmproxy.listener` | `127.0.0.1:7242` | A non-loopback address requires `allow_remote = true`. |
| `llmproxy.surfaces` | `["openai"]` | At least one; see §2. |
| `llmproxy.require_identity` | `false` | Requires the `auth` module to be enabled. |
