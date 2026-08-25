# Jetty module: `llmproxy` — v1

Mount: a module-declared listener, default `http://127.0.0.1:7242`
Control-plane mount: `/llmproxy/v1` on the control listener
Depends on: [SPEC.md](../SPEC.md) §1–§4.

A transparent proxy for LLM provider HTTP APIs: a client configured with a
`base_url` reaches the provider through the sidecar, which owns the vendor
credential. The proxy offers **exactly the API the provider offers** — it
forwards requests and responses verbatim and adds nothing of its own beyond
credential custody, admission, and usage counters.

---

## 1. Listeners

The proxied surfaces are served on a module-declared listener (SPEC.md §2.1),
separate from the control listener, because the URL layouts they mirror would
otherwise collide with this specification's own routes.

The listener address **MUST** be reported as `modules[].listener` in
`GET /v1/meta` (SPEC.md §4.2).

The surfaces in §2 conform to the APIs they front, not to SPEC.md §3. Error
responses on the surface listener are the provider's own (§3), never the
envelope in SPEC.md §3.1. SPEC.md §1 applies in full.

The control-plane endpoints in §6 are served on the control listener and
conform to SPEC.md §3 in full.

---

## 2. Surfaces

Each surface is one provider API behind one path prefix. A surface is enabled
by configuring it (§7); an implementation **MUST** reject a configuration that
enables the module with no surfaces, and **MUST** reject a configured surface
it does not ship rather than serve a subset silently.

| Surface | Prefix | Fronts |
|---|---|---|
| `gemini` | `/genai` | Google Generative Language API (`generativelanguage.googleapis.com`) |
| `openai` | `/openai` | OpenAI REST API (`api.openai.com`) |
| `anthropic` | `/anthropic` | Anthropic API (`api.anthropic.com`) |

The path under the prefix is the provider's own path, unchanged: a client
built for `https://generativelanguage.googleapis.com/v1beta/...` points its
base URL at `{listener}/genai` and everything after the prefix is forwarded
as-is. Every endpoint the provider serves under that base is in scope —
including ones that did not exist when this build shipped.

---

## 3. Transparency

In `passthrough` mode (§5), for every request under a surface prefix an
implementation:

- **MUST** forward the method, the remaining path, the query string, and the
  body to the configured upstream **verbatim**. It **MUST NOT** parse,
  validate, translate, reject, or rewrite request fields or parameters it
  does not recognize — unknown fields are the provider's to judge.
- **MUST** relay the upstream response — status, body, and content type —
  verbatim, including provider error responses of every shape and status.
- **MUST** relay a streaming response unbuffered, preserving the provider's
  own framing. The proxy adds no events, comments, or terminators of its
  own. If the upstream connection is lost mid-stream, the client connection
  is closed; truncation is propagated, never papered over (SPEC.md §1.2).
- **MUST** cancel the upstream call when the client disconnects.
- **MAY** drop or replace hop-by-hop headers (`Connection`,
  `Transfer-Encoding`, `Host`, and the like) and **MAY** pin
  `Accept-Encoding`; all other request headers are forwarded except those
  named in §4.

### 3.1 Synthesized responses

The proxy speaks with its own voice only when there is no upstream response
to relay: the upstream is unreachable, times out, or the connection fails
before a status line arrives. Such a response:

- **MUST** use the provider's own error shape for the surface, with a `5xx`
  status;
- **MUST** carry the header `x-jetty-error` (for example
  `x-jetty-error: upstream_unreachable`) so a client can distinguish the
  proxy's failures from the provider's without parsing prose;
- **MUST NOT** be emitted once any part of an upstream response has been
  relayed.

---

## 4. Credentials

The sidecar owns the vendor credential. On every forwarded request an
implementation:

- **MUST** remove client-supplied credentials before forwarding: the
  `Authorization`, `x-api-key`, and `x-goog-api-key` headers, and the `key`
  query parameter. A client credential is admission-control input at most;
  it **MUST NOT** reach the upstream.
- **MUST** attach the configured credential in the form the provider expects
  (`x-goog-api-key` for `gemini`, `Authorization: Bearer` for `openai`,
  `x-api-key` for `anthropic`).
- **MUST NOT** write the configured credential to logs, error messages, or
  any response.

Admission control is the listener's transport (SPEC.md §1.5): loopback or
socket permissions. Binding a non-loopback address requires
`allow_remote = true`.

---

## 5. Modes

Each surface is independently `passthrough` or `mock`.

| Mode | Behaviour |
|---|---|
| `passthrough` | §3: forward to the configured upstream with the configured key. |
| `mock` | A deterministic emulator of the surface's API. No network I/O. |

The `mock` mode exists for development, CI, and conformance. Responses are
derived from a hash of the request, so identical requests get identical
responses. A mock **MUST** implement at least the surface's primary
generation endpoint, its streaming variant, and its model-listing endpoint,
answering in the provider's shapes; other endpoints return the provider's
not-found shape. A mock **MAY** ignore request fields it does not model; it
**MUST NOT** reject fields the provider would accept.

---

## 6. Control plane

Served on the control listener; conforms to SPEC.md §3.

### `GET /llmproxy/v1/capabilities`

```json
{
  "listener": "http://127.0.0.1:7242",
  "surfaces": {
    "gemini": { "mode": "passthrough", "upstream": "https://generativelanguage.googleapis.com" }
  }
}
```

### `GET /llmproxy/v1/usage`

Aggregate counters since process start, broken down by model where the
surface's URL layout makes the model evident, else under `"-"`:

```json
{
  "models": {
    "gemini-3.7-flash": { "requests": 4, "errors": 1, "input_tokens": 32, "output_tokens": 4 }
  }
}
```

Token counters are best-effort — populated when a relayed response carries
usage metadata the implementation recognizes, absent otherwise (streamed
responses **MAY** count requests only). An implementation **MUST NOT** retain
prompt or completion content; counters only.

---

## 7. Configuration

| Key | Default | Meaning |
|---|---|---|
| `llmproxy.enabled` | `false` | Mount the module. |
| `llmproxy.listener` | `127.0.0.1:7242` | A non-loopback address requires `allow_remote = true`. |
| `llmproxy.surfaces.<name>` | — | One table per enabled surface; at least one required. |
| `llmproxy.surfaces.<name>.mode` | `passthrough` | §5. |
| `llmproxy.surfaces.<name>.upstream` | the provider's public base URL | Passthrough target. |
| `llmproxy.surfaces.<name>.api_key` | — | Required in `passthrough` mode. |
