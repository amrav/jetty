"""The llmproxy module: transparency, credentials, mock mode, control plane.

Assertions are against spec/llmproxy-v1.md, exercised black-box. The
passthrough tests stand up a real HTTP upstream and compare bytes on both
legs: transparency is a claim about the wire, so it is observed on the wire.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from absl.testing import absltest
from fastapi.testclient import TestClient

import jetty
from jetty.config import Config
from jetty.server import create_app

IMPORT_ROOT = str(Path(jetty.__file__).resolve().parent.parent)


def _uds_get(sock: str, path: str) -> tuple[int, str]:
    """Minimal HTTP/1.1 GET over a unix socket (as in test_listener.py)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(sock)
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    raw = b"".join(chunks).decode()
    head, _, body = raw.partition("\r\n\r\n")
    return int(head.split()[1]), body


class LlmProxyTestCase(absltest.TestCase):

    def setUp(self):
        super().setUp()
        self.socket_path = os.path.join(self.create_tempdir().full_path, "jetty.sock")

    def build(self, surfaces=None, **settings) -> TestClient:
        merged = {
            "enabled": True,
            "surfaces": surfaces if surfaces is not None else {"gemini": {"mode": "mock"}},
            **settings,
        }
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": {"llmproxy": merged}}
        )
        return TestClient(create_app(cfg))

    def surface(self, control: TestClient) -> TestClient:
        module = next(
            m for m in control.app.state.jetty.modules if m.name == "llmproxy"
        )
        client = TestClient(module.listener_app())
        # The listener app's own lifespan starts the forwarders' httpx
        # clients — on the loop that serves them, as in production.
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    @staticmethod
    def prompt(text: str = "ping", **over) -> dict:
        return {"contents": [{"role": "user", "parts": [{"text": text}]}], **over}


class LifecycleTest(LlmProxyTestCase):

    def test_disabled_by_default_is_module_disabled(self):
        cfg = Config.model_validate({"listener": {"uds": self.socket_path}})
        with TestClient(create_app(cfg)) as c:
            r = c.get("/llmproxy/v1/capabilities")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "module_disabled")

    def test_meta_advertises_the_listener(self):
        with self.build(listener="127.0.0.1:7311") as c:
            modules = c.get("/v1/meta").json()["modules"]
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]["name"], "llmproxy")
        self.assertEqual(modules[0]["listener"], "http://127.0.0.1:7311")

    def test_no_surfaces_is_rejected(self):
        with self.assertRaisesRegex(Exception, "at least one"):
            self.build(surfaces={})

    def test_unknown_surface_is_rejected(self):
        with self.assertRaisesRegex(Exception, "not a surface"):
            self.build(surfaces={"gemeni": {"mode": "mock"}})

    def test_unshipped_surface_fails_loudly(self):
        """llmproxy-v1 §2: never serve a silent subset of the config."""
        with self.assertRaisesRegex(Exception, "this build ships: gemini"):
            self.build(surfaces={"openai": {"mode": "mock"}})

    def test_passthrough_requires_api_key(self):
        with self.assertRaisesRegex(Exception, "api_key"):
            self.build(surfaces={"gemini": {"mode": "passthrough"}})

    def test_non_loopback_listener_needs_allow_remote(self):
        with self.assertRaisesRegex(Exception, "allow_remote"):
            self.build(listener="0.0.0.0:7242")

    def test_unknown_config_key_fails_boot(self):
        with self.assertRaises(Exception):
            self.build(surfaces={"gemini": {"mode": "mock", "api_keys": "x"}})

    def test_capabilities_reports_surfaces_and_listener(self):
        with self.build() as c:
            body = c.get("/llmproxy/v1/capabilities").json()
        self.assertEqual(body["listener"], "http://127.0.0.1:7242")
        self.assertEqual(body["surfaces"], {"gemini": {"mode": "mock"}})


class MockSurfaceTest(LlmProxyTestCase):
    """The gemini mock emulator (llmproxy-v1 §5)."""

    def setUp(self):
        super().setUp()
        self.control = self.build()
        self.control.__enter__()
        self.addCleanup(self.control.__exit__, None, None, None)
        self.client = self.surface(self.control)

    def test_generate_content_is_deterministic(self):
        first = self.client.post(
            "/genai/v1beta/models/gemini-3.1-pro-preview:generateContent",
            json=self.prompt(),
        )
        second = self.client.post(
            "/genai/v1beta/models/gemini-3.1-pro-preview:generateContent",
            json=self.prompt(),
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        body = first.json()
        candidate = body["candidates"][0]
        self.assertEqual(candidate["finishReason"], "STOP")
        self.assertIn("mock(gemini-3.1-pro-preview)", candidate["content"]["parts"][0]["text"])
        self.assertEqual(
            body["usageMetadata"]["totalTokenCount"],
            body["usageMetadata"]["promptTokenCount"]
            + body["usageMetadata"]["candidatesTokenCount"],
        )

    def test_unmodelled_fields_are_accepted_not_rejected(self):
        """llmproxy-v1 §5: the mock must not fail a request the provider
        would accept — CI traffic uses tools and safety settings."""
        r = self.client.post(
            "/genai/v1beta/models/m:generateContent",
            json=self.prompt(
                tools=[{"functionDeclarations": [{"name": "f"}]}],
                safetySettings=[{"category": "HARM_CATEGORY_HARASSMENT"}],
                generationConfig={"responseMimeType": "application/json"},
            ),
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_ignored_fields_still_shape_the_hash(self):
        plain = self.client.post(
            "/genai/v1beta/models/m:generateContent", json=self.prompt()
        ).json()
        with_tools = self.client.post(
            "/genai/v1beta/models/m:generateContent", json=self.prompt(tools=[])
        ).json()
        self.assertNotEqual(plain["candidates"], with_tools["candidates"])

    def test_max_output_tokens_truncates(self):
        body = self.client.post(
            "/genai/v1beta/models/m:generateContent",
            json=self.prompt(generationConfig={"maxOutputTokens": 2}),
        ).json()
        self.assertEqual(body["candidates"][0]["finishReason"], "MAX_TOKENS")
        self.assertEqual(body["usageMetadata"]["candidatesTokenCount"], 2)

    def test_sse_stream_matches_the_full_completion(self):
        full = self.client.post(
            "/genai/v1beta/models/m:generateContent", json=self.prompt()
        ).json()["candidates"][0]["content"]["parts"][0]["text"]
        r = self.client.post(
            "/genai/v1beta/models/m:streamGenerateContent?alt=sse", json=self.prompt()
        )
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        events = [
            json.loads(line[len("data:") :])
            for line in r.text.split("\r\n")
            if line.startswith("data:")
        ]
        self.assertGreater(len(events), 1)
        streamed = "".join(
            e["candidates"][0]["content"]["parts"][0]["text"] for e in events
        )
        self.assertEqual(streamed, full)
        self.assertEqual(events[-1]["candidates"][0]["finishReason"], "STOP")

    def test_stream_without_alt_sse_is_a_json_array(self):
        """The provider frames a non-SSE stream as a JSON array; so does the
        mock — the emulated API's own convention, not a jetty invention."""
        r = self.client.post(
            "/genai/v1beta/models/m:streamGenerateContent", json=self.prompt()
        )
        self.assertEqual(r.status_code, 200, r.text)
        chunks = r.json()
        self.assertIsInstance(chunks, list)
        self.assertEqual(chunks[-1]["candidates"][0]["finishReason"], "STOP")

    def test_models_are_listed(self):
        names = [m["name"] for m in self.client.get("/genai/v1beta/models").json()["models"]]
        self.assertIn("models/jetty-mock-large", names)

    def test_unimplemented_paths_are_the_emulated_not_found(self):
        for path, method in [
            ("/genai/v1beta/models/m:embedContent", "post"),
            ("/genai/v1beta/cachedContents", "get"),
            ("/outside-any-surface", "get"),
        ]:
            r = getattr(self.client, method)(path)
            self.assertEqual(r.status_code, 404, path)
            self.assertEqual(r.json()["error"]["status"], "NOT_FOUND")

    def test_invalid_json_and_missing_contents_are_gemini_400s(self):
        r = self.client.post(
            "/genai/v1beta/models/m:generateContent",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["status"], "INVALID_ARGUMENT")
        r = self.client.post("/genai/v1beta/models/m:generateContent", json={})
        self.assertEqual(r.status_code, 400)

    def test_usage_counts_mock_traffic(self):
        self.client.post("/genai/v1beta/models/m1:generateContent", json=self.prompt("a b c"))
        row = self.control.get("/llmproxy/v1/usage").json()["models"]["m1"]
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["errors"], 0)
        self.assertEqual(row["input_tokens"], 3)
        self.assertGreater(row["output_tokens"], 0)


# --- passthrough against a live upstream -----------------------------------

class _FakeUpstream(BaseHTTPRequestHandler):
    """Records exactly what arrives; replies with configured canned bytes."""

    server_version = "FakeUpstream/1.0"

    def log_message(self, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("content-length", "0"))
        self.raw_body = self.rfile.read(length) if length else b""
        self.server.seen.append(self)  # type: ignore[attr-defined]
        status, content_type, payload, *extra = self.server.responder(self)  # type: ignore[attr-defined]
        # A fourth element over-declares content-length; the connection then
        # closes short of it, simulating an upstream lost mid-body.
        declared = extra[0] if extra else len(payload)
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(declared))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


class PassthroughTest(LlmProxyTestCase):

    API_KEY = "configured-upstream-key"

    def default_responder(self, req) -> tuple[int, str, bytes]:
        return (
            200,
            "application/json",
            json.dumps(
                {
                    "echo": req.path,
                    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
                }
            ).encode(),
        )

    def setUp(self):
        super().setUp()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
        self.upstream.seen = []
        self.upstream.responder = self.default_responder
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.shutdown)
        self.control = self.build(
            surfaces={
                "gemini": {
                    "mode": "passthrough",
                    "api_key": self.API_KEY,
                    "upstream": f"http://127.0.0.1:{self.upstream.server_address[1]}",
                }
            }
        )
        self.control.__enter__()  # control plane only; forwarders start with the surface app
        self.addCleanup(self.control.__exit__, None, None, None)
        self.client = self.surface(self.control)

    def test_request_bytes_path_and_query_are_forwarded_verbatim(self):
        """llmproxy-v1 §3: unknown fields are the provider's to judge."""
        body = json.dumps(
            self.prompt(
                tools=[{"functionDeclarations": [{"name": "f"}]}],
                thinkingConfig={"thinkingBudget": 42},
                someFieldInventedNextYear=True,
            )
        ).encode()
        r = self.client.post(
            "/genai/v1beta/models/gemini-3.7-flash:generateContent?pageSize=2&x=1",
            content=body,
            headers={"content-type": "application/json"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        seen = self.upstream.seen[-1]
        self.assertEqual(seen.raw_body, body)  # byte-for-byte
        self.assertEqual(
            seen.path, "/v1beta/models/gemini-3.7-flash:generateContent?pageSize=2&x=1"
        )

    def test_provider_responses_relay_verbatim_including_errors(self):
        """llmproxy-v1 §3: the provider's errors are the client's to see."""
        vendor_error = json.dumps(
            {"error": {"code": 429, "message": "quota exceeded",
                       "status": "RESOURCE_EXHAUSTED",
                       "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure"}]}}
        ).encode()
        self.upstream.responder = lambda req: (429, "application/json", vendor_error)
        r = self.client.post(
            "/genai/v1beta/models/m:generateContent", json=self.prompt()
        )
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.content, vendor_error)  # byte-for-byte, details intact
        self.assertNotIn("x-jetty-error", r.headers)

    def test_client_credentials_never_reach_the_upstream(self):
        """llmproxy-v1 §4: headers and the ?key= parameter are stripped;
        the configured key alone authenticates."""
        r = self.client.post(
            "/genai/v1beta/models/m:generateContent?key=client-url-key&alt=json",
            json=self.prompt(),
            headers={
                "authorization": "Bearer client-secret",
                "x-api-key": "client-key",
                "x-goog-api-key": "client-goog-key",
                "x-goog-user-project": "some-project",  # non-credential: forwarded
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        seen = self.upstream.seen[-1]
        self.assertEqual(seen.headers.get("x-goog-api-key"), self.API_KEY)
        self.assertIsNone(seen.headers.get("authorization"))
        self.assertIsNone(seen.headers.get("x-api-key"))
        self.assertNotIn("key=client-url-key", seen.path)
        self.assertIn("alt=json", seen.path)
        self.assertEqual(seen.headers.get("x-goog-user-project"), "some-project")
        for value in seen.headers.values():
            self.assertNotIn("client-secret", value)

    def test_every_provider_endpoint_is_in_scope(self):
        """Transparency means countTokens, embedContent, files — all of it —
        without jetty knowing them by name."""
        for method, path in [
            ("post", "/genai/v1beta/models/m:countTokens"),
            ("post", "/genai/v1beta/models/m:embedContent"),
            ("get", "/genai/v1beta/cachedContents?pageSize=5"),
            ("delete", "/genai/v1beta/files/abc123"),
        ]:
            r = getattr(self.client, method)(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertTrue(
                self.upstream.seen[-1].path.startswith(path.removeprefix("/genai")),
                path,
            )

    def test_sse_streams_relay_verbatim(self):
        payload = (
            b'data: {"candidates":[{"content":{"parts":[{"text":"pon"}]}}]}\r\n\r\n'
            b'data: {"candidates":[{"content":{"parts":[{"text":"g"}]},'
            b'"finishReason":"STOP"}]}\r\n\r\n'
        )
        self.upstream.responder = lambda req: (200, "text/event-stream", payload)
        r = self.client.post(
            "/genai/v1beta/models/m:streamGenerateContent?alt=sse", json=self.prompt()
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(r.content, payload)  # no added events or terminators

    def test_unreachable_upstream_is_synthesized_and_marked(self):
        """llmproxy-v1 §3.1: jetty's own voice is provider-shaped and marked
        with x-jetty-error, so proxy failures never masquerade as verdicts."""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        control = self.build(
            surfaces={
                "gemini": {
                    "mode": "passthrough",
                    "api_key": "k",
                    "upstream": f"http://127.0.0.1:{dead_port}",
                }
            },
            listener="127.0.0.1:7243",
        )
        with control:
            r = self.surface(control).post(
                "/genai/v1beta/models/m:generateContent", json=self.prompt()
            )
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.headers.get("x-jetty-error"), "upstream_unreachable")
        self.assertEqual(r.json()["error"]["status"], "UNAVAILABLE")

    def test_usage_counts_relayed_traffic(self):
        self.client.post("/genai/v1beta/models/m1:generateContent", json=self.prompt())
        self.upstream.responder = lambda req: (
            500, "application/json", b'{"error":{"code":500}}'
        )
        self.client.post("/genai/v1beta/models/m1:generateContent", json=self.prompt())
        row = self.control.get("/llmproxy/v1/usage").json()["models"]["m1"]
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["errors"], 1)
        self.assertEqual(row["input_tokens"], 7)   # from relayed usageMetadata
        self.assertEqual(row["output_tokens"], 3)

    def test_body_lost_mid_response_is_synthesized_and_marked(self):
        """llmproxy-v1 §3.1: a buffered body that dies mid-read has relayed
        nothing — jetty speaks, provider-shaped and marked, instead of a
        bare 500 or a silent truncation."""
        self.upstream.responder = lambda req: (200, "application/json", b'{"partial', 400)
        r = self.client.post(
            "/genai/v1beta/models/m1:generateContent", json=self.prompt()
        )
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.headers.get("x-jetty-error"), "upstream_lost")
        self.assertEqual(r.json()["error"]["status"], "UNAVAILABLE")
        row = self.control.get("/llmproxy/v1/usage").json()["models"]["m1"]
        self.assertEqual(row["errors"], 1)

    def test_stream_lost_mid_relay_aborts_not_clean_eof(self):
        """llmproxy-v1 §3: truncation is propagated. A swallowed stream
        error would end the chunked response with a valid terminal chunk — a
        fabricated 'complete'. The relay must abort the connection."""
        self.upstream.responder = lambda req: (
            200, "text/event-stream", b'data: {"candidates":[]}\r\n\r\n', 4096
        )
        with self.assertRaises(Exception):
            self.client.post(
                "/genai/v1beta/models/m:streamGenerateContent?alt=sse",
                json=self.prompt(),
            )

    def test_capabilities_names_the_upstream(self):
        body = self.control.get("/llmproxy/v1/capabilities").json()
        self.assertEqual(body["surfaces"]["gemini"]["mode"], "passthrough")
        self.assertIn("127.0.0.1", body["surfaces"]["gemini"]["upstream"])


# --- the whole thing over real sockets -------------------------------------

class ProcessTest(absltest.TestCase):
    """jetty as a real process: control plane over UDS, surface over TCP.

    Socket paths are kept relative to the managed temp dir (AF_UNIX's 108-byte
    limit; see test_listener.py) and the TCP port is picked by binding port 0
    and releasing it — a small race, accepted for a test.
    """

    def test_meta_discovery_to_surface_call(self):
        workdir = self.create_tempdir()
        os.chmod(workdir.full_path, 0o700)
        origin = os.getcwd()
        self.addCleanup(os.chdir, origin)
        os.chdir(workdir.full_path)

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        config = workdir.create_file(
            "jetty.toml",
            content=(
                '[listener]\nuds = "jetty.sock"\n\n'
                "[modules.llmproxy]\nenabled = true\n"
                f'listener = "127.0.0.1:{port}"\n\n'
                '[modules.llmproxy.surfaces.gemini]\nmode = "mock"\n'
            ),
        ).full_path
        env = {**os.environ, "PYTHONPATH": IMPORT_ROOT}
        proc = subprocess.Popen(
            [sys.executable, "-m", "jetty.cli", "--config", config],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.addCleanup(proc.wait, timeout=10)
        self.addCleanup(proc.terminate)

        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f"{base}/genai/v1beta/models", timeout=2) as r:
                    listed = json.load(r)
                break
            except OSError:
                if proc.poll() is not None:
                    self.fail(f"jetty exited: {proc.stdout.read().decode()}")
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        self.assertIn("models/jetty-mock-large", [m["name"] for m in listed["models"]])

        status, body = _uds_get("jetty.sock", "/v1/meta")
        self.assertEqual(status, 200)
        module = json.loads(body)["modules"][0]
        self.assertEqual(module["name"], "llmproxy")
        self.assertEqual(module["listener"], base)

        req = urllib.request.Request(
            f"{base}/genai/v1beta/models/gemini-3.7-flash:generateContent",
            data=json.dumps(
                {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}
            ).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            completion = json.load(r)
        self.assertIn(
            "mock(gemini-3.7-flash)",
            completion["candidates"][0]["content"]["parts"][0]["text"],
        )


class ProcessPassthroughTest(absltest.TestCase):
    """Passthrough through a real process: the forwarder's httpx client must
    live on the module listener's own event loop (a daemon thread), and must
    exist before the first request — the cross-loop/boot-race regression."""

    def test_passthrough_over_real_sockets(self):
        workdir = self.create_tempdir()
        os.chmod(workdir.full_path, 0o700)
        origin = os.getcwd()
        self.addCleanup(os.chdir, origin)
        os.chdir(workdir.full_path)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
        upstream.seen = []
        upstream.responder = lambda req: (200, "application/json", b'{"pong": true}')
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.shutdown)

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        config = workdir.create_file(
            "jetty.toml",
            content=(
                '[listener]\nuds = "jetty.sock"\n\n'
                "[modules.llmproxy]\nenabled = true\n"
                f'listener = "127.0.0.1:{port}"\n\n'
                "[modules.llmproxy.surfaces.gemini]\n"
                'mode = "passthrough"\napi_key = "process-test-key"\n'
                f'upstream = "http://127.0.0.1:{upstream.server_address[1]}"\n'
            ),
        ).full_path
        env = {**os.environ, "PYTHONPATH": IMPORT_ROOT}
        proc = subprocess.Popen(
            [sys.executable, "-m", "jetty.cli", "--config", config],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.addCleanup(proc.wait, timeout=10)
        self.addCleanup(proc.terminate)

        deadline = time.monotonic() + 20
        while True:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/genai/v1beta/models/m:generateContent",
                data=b'{"contents":[]}',
                headers={"content-type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(json.load(r), {"pong": True})
                break
            except OSError:
                if proc.poll() is not None:
                    self.fail(f"jetty exited: {proc.stdout.read().decode()}")
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        self.assertEqual(
            upstream.seen[-1].headers.get("x-goog-api-key"), "process-test-key"
        )


if __name__ == "__main__":
    absltest.main()
