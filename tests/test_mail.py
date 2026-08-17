"""The mail module: wire contract, spool driver, and error shape.

Assertions here are against spec/mail-v1.md, exercised black-box through the
HTTP surface: dedup, dryRun, header injection, all-or-nothing 422,
plus-addressed senders, bearer auth.
"""

from __future__ import annotations

import json
import os

from absl.testing import absltest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.server import create_app

FROM = "relay-bot+notify@corp.example"
TO = "harper@corp.example"


class MailTestCase(absltest.TestCase):

    def setUp(self):
        super().setUp()
        base = self.create_tempdir().full_path
        self.socket_path = os.path.join(base, "jetty.sock")
        self.spool_dir = os.path.join(base, "spool")
        self._key_serial = 0

    def build(self, **mail_settings) -> TestClient:
        settings = {
            "enabled": True,
            "spool_dir": self.spool_dir,
            "sender": FROM,
            "domain": "corp.example",
            **mail_settings,
        }
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": {"mail": settings}}
        )
        return TestClient(create_app(cfg))

    def message(self, **over):
        self._key_serial += 1
        return {
            "idempotencyKey": f"test-{self.id()}-{self._key_serial}",
            "from": FROM,
            "to": [TO],
            "subject": "mail conformance probe",
            "text": "produced by jetty's mail tests",
            **over,
        }

    def send(self, client, body, token=""):
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return client.post("/mail/v1/send", json=body, headers=headers)

    def spooled(self):
        rows = []
        if os.path.isdir(self.spool_dir):
            for name in sorted(os.listdir(self.spool_dir)):
                if name.endswith(".json"):
                    with open(os.path.join(self.spool_dir, name), encoding="utf-8") as f:
                        rows.append(json.load(f))
        return rows

    def assert_mail_error(self, response, status, slug):
        """The contract's flat shape (mail-v1 §1), never the SPEC.md §3.1
        envelope — `error` is a string slug, not an object."""
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body["error"], slug)
        self.assertIsInstance(body["error"], str)


class ModuleLifecycleTest(MailTestCase):

    def test_disabled_by_default_is_module_disabled(self):
        cfg = Config.model_validate({"listener": {"uds": self.socket_path}})
        with TestClient(create_app(cfg)) as c:
            r = c.post("/mail/v1/send", json=self.message())
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "module_disabled")

    def test_meta_advertises_mail_without_listener(self):
        with self.build() as c:
            modules = c.get("/v1/meta").json()["modules"]
        self.assertEqual([m["name"] for m in modules], ["mail"])
        self.assertEqual(modules[0]["mount"], "/mail")
        self.assertNotIn("listener", modules[0])

    def test_unavailable_driver_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "smtp"):
            self.build(driver="smtp")

    def test_spool_driver_requires_spool_dir(self):
        with self.assertRaisesRegex(Exception, "spool_dir"):
            self.build(spool_dir="")

    def test_unknown_config_key_fails_boot(self):
        with self.assertRaises(Exception):
            self.build(spool_dri=self.spool_dir)

    def test_bad_fail_mode_fails_boot(self):
        with self.assertRaises(Exception):
            self.build(fail="404")


class SendTest(MailTestCase):

    def test_well_formed_send_is_accepted_and_spooled(self):
        msg = self.message(cc=["jlin@corp.example"], threadKey="t/1", tags=["probe"])
        with self.build() as c:
            r = self.send(c, msg)
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertNotEmpty(body["messageId"])
        self.assertFalse(body["deduped"])
        (row,) = self.spooled()
        self.assertEqual(row["from"], FROM)
        self.assertEqual(row["to"], [TO])
        self.assertEqual(row["cc"], ["jlin@corp.example"])
        self.assertEqual(row["threadKey"], "t/1")
        self.assertEqual(row["tags"], ["probe"])
        self.assertEqual(row["messageId"], body["messageId"])
        self.assertIn("acceptedAt", row)

    def test_html_alternative_is_spooled(self):
        with self.build() as c:
            r = self.send(c, self.message(html="<p>hi</p>"))
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.spooled()[0]["html"], "<p>hi</p>")

    def test_plus_addressed_sender_is_accepted(self):
        # The permitted sender is pinned as the bare account; the request uses
        # a +tag on it. A driver that normalizes the tag away or rejects it
        # breaks every plus-addressed deployment (mail-v1 §3.3).
        with self.build(sender="relay-bot@corp.example") as c:
            r = self.send(c, self.message())
        self.assertEqual(r.status_code, 202, r.text)

    def test_forbidden_sender_is_rejected(self):
        with self.build() as c:
            r = self.send(c, self.message(**{"from": "someone-else@corp.example"}))
        self.assert_mail_error(r, 403, "sender_not_permitted")
        self.assertEmpty(self.spooled())


class IdempotencyTest(MailTestCase):

    def test_same_key_sends_once_and_answers_deduped(self):
        msg = self.message()
        with self.build() as c:
            first = self.send(c, msg).json()
            second = self.send(c, msg)
        self.assertEqual(second.status_code, 202)
        self.assertTrue(second.json()["deduped"])
        self.assertEqual(second.json()["messageId"], first["messageId"])
        self.assertLen(self.spooled(), 1)  # exactly one delivery, not two

    def test_dedup_survives_restart(self):
        # The idempotency store is rebuilt from the spool at boot (mail-v1 §5),
        # so the ≥7-day window holds across a process restart.
        msg = self.message()
        with self.build() as c:
            first = self.send(c, msg).json()
        with self.build() as c:
            second = self.send(c, msg).json()
        self.assertTrue(second["deduped"])
        self.assertEqual(second["messageId"], first["messageId"])
        self.assertLen(self.spooled(), 1)

    def test_dry_run_delivers_nothing_and_consumes_no_key(self):
        msg = self.message()
        with self.build() as c:
            dry = self.send(c, {**msg, "dryRun": True})
            self.assertEqual(dry.status_code, 202)
            self.assertEmpty(self.spooled())
            # the key is still free: a later real send under it must deliver
            real = self.send(c, msg)
        self.assertFalse(real.json()["deduped"])
        self.assertLen(self.spooled(), 1)


class ValidationTest(MailTestCase):

    def assert_bad_request(self, **over):
        with self.build() as c:
            r = self.send(c, self.message(**over))
        self.assert_mail_error(r, 400, "bad_request")
        self.assertEmpty(self.spooled())

    def test_crlf_in_subject_is_header_injection(self):
        self.assert_bad_request(subject="ok\r\nBcc: someone@elsewhere.example")

    def test_over_limit_recipients_are_rejected_not_truncated(self):
        many = [f"probe{i}@corp.example" for i in range(101)]
        self.assert_bad_request(to=many)

    def test_missing_required_fields(self):
        for field_name in ("idempotencyKey", "from", "to", "subject", "text"):
            msg = self.message()
            del msg[field_name]
            with self.build() as c:
                r = self.send(c, msg)
            self.assert_mail_error(r, 400, "bad_request")

    def test_unknown_field_is_rejected(self):
        self.assert_bad_request(threadkey="miscased")

    def test_display_name_addresses_are_rejected(self):
        self.assert_bad_request(to=["Harper Wu <harper@corp.example>"])

    def test_over_limit_field_sizes(self):
        self.assert_bad_request(idempotencyKey="k" * 129)
        self.assert_bad_request(subject="s" * 513)
        self.assert_bad_request(text="t" * (256 * 1024 + 1))
        self.assert_bad_request(html="h" * (512 * 1024 + 1))
        self.assert_bad_request(threadKey="t" * 257)
        self.assert_bad_request(tags=["t"] * 11)
        self.assert_bad_request(tags=["t" * 65])
        self.assert_bad_request(replyTo=[f"r{i}@corp.example" for i in range(6)])

    def test_unparseable_body_is_bad_request(self):
        with self.build() as c:
            r = c.post(
                "/mail/v1/send",
                content=b"not json",
                headers={"content-type": "application/json"},
            )
        self.assert_mail_error(r, 400, "bad_request")


class RoutabilityTest(MailTestCase):

    def test_unroutable_recipient_fails_whole_message_and_names_itself(self):
        with self.build() as c:
            r = self.send(c, self.message(cc=["nobody@elsewhere.invalid"]))
        self.assert_mail_error(r, 422, "unroutable_recipients")
        self.assertEqual(r.json()["recipients"], ["nobody@elsewhere.invalid"])
        self.assertEmpty(self.spooled())  # all-or-nothing: nothing partial went out

    def test_unconstrained_domain_routes_anywhere(self):
        with self.build(domain="") as c:
            r = self.send(c, self.message(to=["anyone@elsewhere.example"]))
        self.assertEqual(r.status_code, 202)


class ForcedFailureTest(MailTestCase):

    def test_forced_503(self):
        with self.build(fail="503") as c:
            r = self.send(c, self.message())
        self.assert_mail_error(r, 503, "upstream_unavailable")
        self.assertEmpty(self.spooled())

    def test_forced_429_carries_retry_after(self):
        with self.build(fail="429") as c:
            r = self.send(c, self.message())
        self.assert_mail_error(r, 429, "rate_limited")
        self.assertEqual(r.json()["retryAfterSeconds"], 60)

    def test_forced_422(self):
        with self.build(fail="422") as c:
            r = self.send(c, self.message())
        self.assert_mail_error(r, 422, "unroutable_recipients")
        self.assertEqual(r.json()["recipients"], [TO])

    def test_forced_503_fails_healthz(self):
        with self.build(fail="503") as c:
            r = c.get("/mail/healthz")
        self.assert_mail_error(r, 503, "upstream_unavailable")


class AuthTest(MailTestCase):

    TOKEN = "test-mail-token"

    def test_missing_bearer_is_rejected(self):
        with self.build(token=self.TOKEN) as c:
            r = self.send(c, self.message())
        self.assert_mail_error(r, 401, "missing_or_bad_bearer_token")

    def test_wrong_bearer_is_rejected(self):
        with self.build(token=self.TOKEN) as c:
            r = self.send(c, self.message(), token="wrong")
        self.assert_mail_error(r, 401, "missing_or_bad_bearer_token")

    def test_right_bearer_is_accepted(self):
        with self.build(token=self.TOKEN) as c:
            r = self.send(c, self.message(), token=self.TOKEN)
        self.assertEqual(r.status_code, 202, r.text)

    def test_healthz_answers_without_a_bearer_token(self):
        with self.build(token=self.TOKEN) as c:
            r = c.get("/mail/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})

    def test_unmatched_route_still_requires_token(self):
        with self.build(token=self.TOKEN) as c:
            r = c.get("/mail/v1/keys")
        self.assert_mail_error(r, 401, "missing_or_bad_bearer_token")


class SurfaceShapeTest(MailTestCase):

    def test_unknown_route_is_flat_not_found(self):
        with self.build() as c:
            r = c.get("/mail/v1/keys")
        self.assert_mail_error(r, 404, "not_found")

    def test_healthz_reports_upstream_health(self):
        with self.build() as c:
            healthy = c.get("/mail/healthz")
            self.assertEqual(healthy.json(), {"ok": True})
            os.chmod(self.spool_dir, 0o500)
            try:
                r = c.get("/mail/healthz")
            finally:
                os.chmod(self.spool_dir, 0o700)
        self.assert_mail_error(r, 503, "upstream_unavailable")


if __name__ == "__main__":
    absltest.main()
