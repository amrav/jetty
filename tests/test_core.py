"""Core behaviour: the module registry, the spec's core endpoints, envelope."""

from __future__ import annotations

import os

from absl.testing import absltest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.modules.registry import UnknownModuleError
from jetty.server import SPEC_VERSION, create_app


class JettyTestCase(absltest.TestCase):
    """Base: builds an app around a socket path owned by the test runner.

    Nothing in this file binds the socket — TestClient drives the ASGI app in
    process and never touches the transport — but the path still has to come
    from `create_tempdir()` rather than a literal. A hardcoded `/tmp/...` bakes
    in an assumption that the build environment has a writable /tmp, and would
    also collide between concurrent runs on the same host.
    """

    def setUp(self):
        super().setUp()
        self.socket_path = os.path.join(
            self.create_tempdir().full_path, "jetty.sock"
        )

    def build(self, **modules: dict) -> TestClient:
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": modules}
        )
        return TestClient(create_app(cfg))


class CoreEndpointsTest(JettyTestCase):

    def test_healthz_is_liveness_only(self):
        with self.build() as c:
            r = c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["spec_version"], SPEC_VERSION)

    def test_meta_advertises_enabled_modules(self):
        with self.build(reference={"enabled": True}) as c:
            body = c.get("/v1/meta").json()
        self.assertEqual(body["spec_version"], SPEC_VERSION)
        self.assertEqual([m["name"] for m in body["modules"]], ["reference"])
        self.assertEqual(body["modules"][0]["mount"], "/reference")
        # No limits block and no readiness flag — both removed from the protocol.
        self.assertNotIn("limits", body)
        self.assertNotIn("required", body["modules"][0])


class EnableDisableTest(JettyTestCase):

    def test_nothing_is_enabled_by_default(self):
        with self.build() as c:
            self.assertEmpty(c.get("/v1/meta").json()["modules"])
            # SPEC.md §4.3: a disabled module's route does not exist, and says
            # so with `module_disabled` rather than a bare not_found.
            r = c.post("/reference/v1/echo", json={"message": "hi"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "module_disabled")
        self.assertFalse(r.json()["error"]["retryable"])

    def test_unknown_route_is_not_found_not_module_disabled(self):
        with self.build(reference={"enabled": True}) as c:
            r = c.get("/nope/v1/whatever")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

    def test_every_404_uses_the_spec_envelope(self):
        """Starlette's default {"detail": ...} must never reach a client."""
        with self.build(reference={"enabled": True}) as c:
            for path in ("/nope", "/reference/v1/nope", "/auth/v1/identify"):
                body = c.get(path).json()
                self.assertIn("error", body, f"{path} returned {body}")
                self.assertEqual(
                    set(body["error"]), {"code", "message", "retryable"}
                )

    def test_module_absent_from_config_is_disabled(self):
        with self.build(reference={"enabled": False}) as c:
            self.assertEmpty(c.get("/v1/meta").json()["modules"])

    def test_enabled_module_serves_its_routes(self):
        with self.build(reference={"enabled": True}) as c:
            r = c.post("/reference/v1/echo", json={"message": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"message": "hi"})

    def test_unknown_module_in_config_fails_boot(self):
        """A typo must not silently leave a security module disabled."""
        with self.assertRaises(UnknownModuleError) as ctx:
            self.build(ath={"enabled": True})
        self.assertIn("ath", str(ctx.exception))

    def test_auth_and_llmproxy_are_not_yet_registered(self):
        """Until the real module lands, enabling it must fail closed, not stub."""
        with self.assertRaises(UnknownModuleError):
            self.build(auth={"enabled": True})


class ErrorEnvelopeTest(JettyTestCase):

    def test_error_envelope_shape(self):
        with self.build(reference={"enabled": True}) as c:
            r = c.get("/reference/v1/boom")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(
            r.json(),
            {
                "error": {
                    "code": "upstream_unavailable",
                    "message": "reference module: deliberate failure",
                    "retryable": True,
                }
            },
        )

    def test_unknown_request_field_is_rejected_not_ignored(self):
        """SPEC.md §6 — the one place strictness beats tolerance."""
        with self.build(reference={"enabled": True}) as c:
            r = c.post(
                "/reference/v1/echo", json={"message": "hi", "groups": ["admin"]}
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid_request")


class ListenerConfigTest(JettyTestCase):
    """Config-level listener invariants. No socket is ever bound here."""

    def test_tcp_listener_refuses_non_loopback_without_explicit_optin(self):
        with self.assertRaisesRegex(ValueError, "allow_remote"):
            Config.model_validate({"listener": {"uds": None, "tcp": "0.0.0.0:7241"}})
        ok = Config.model_validate(
            {
                "listener": {
                    "uds": None,
                    "tcp": "0.0.0.0:7241",
                    "allow_remote": True,
                }
            }
        )
        self.assertEqual(ok.listener.tcp, "0.0.0.0:7241")

    def test_uds_mode_may_not_grant_other_users(self):
        with self.assertRaisesRegex(ValueError, "0660 or tighter"):
            Config.model_validate(
                {"listener": {"uds": self.socket_path, "uds_mode": 0o666}}
            )

    def test_exactly_one_listener(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            Config.model_validate(
                {"listener": {"uds": self.socket_path, "tcp": "127.0.0.1:1"}}
            )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            Config.model_validate({"listener": {"uds": None}})

    def test_unknown_config_key_is_rejected(self):
        with self.assertRaises(ValueError):
            Config.model_validate(
                {"listener": {"uds": self.socket_path}, "lisener": {}}
            )


if __name__ == "__main__":
    absltest.main()
