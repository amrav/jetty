"""The functions module: name syntax, raw payloads, exec driver, error shape.

Assertions here are against spec/functions-v1.md, exercised black-box
through the HTTP surface. Functions under test are small Python programs
written into the test's temp directory and run with the current interpreter.
"""

from __future__ import annotations

import os
import sys

from absl.testing import absltest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.modules.functions.driver import UnknownFunction
from jetty.modules.functions.module import register_driver
from jetty.server import create_app

# A byte string that is not valid UTF-8 and not valid JSON: the payload must
# cross untouched, whatever it looks like.
BINARY = bytes(range(256)) * 3

ECHO = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
UPPER = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper())"
FAIL = (
    "import sys; sys.stdout.write('partial output'); "
    "sys.stderr.write('boom'); sys.exit(3)"
)
SLOW = "import time; time.sleep(30)"
NAME = "import os, sys; sys.stdout.write(os.environ['JETTY_FUNCTION'])"


class FunctionsTestCase(absltest.TestCase):

    def setUp(self):
        super().setUp()
        self.base = self.create_tempdir().full_path
        self.socket_path = os.path.join(self.base, "jetty.sock")

    def script(self, source: str) -> list[str]:
        path = self.create_tempfile(content=source).full_path
        return [sys.executable, path]

    def build(self, functions=None, **settings) -> TestClient:
        cfg = Config.model_validate(
            {
                "listener": {"uds": self.socket_path},
                "modules": {
                    "functions": {
                        "enabled": True,
                        "exec": functions or {},
                        **settings,
                    }
                },
            }
        )
        return TestClient(create_app(cfg))

    def assert_envelope(self, response, status, code, retryable):
        self.assertEqual(response.status_code, status, response.text)
        err = response.json()["error"]
        self.assertEqual(err["code"], code)
        self.assertEqual(err["retryable"], retryable)
        self.assertIsInstance(err["message"], str)


class FakeDriver:
    """A library-backed driver as a private build would write one: no
    subprocess, its own settings key, lifecycle hooks."""

    def __init__(self, settings):
        self.settings = settings
        self.started = False
        self.stopped = False

    async def startup(self):
        if self.settings.get("fake", {}).get("fail_startup"):
            raise RuntimeError("backend unreachable at boot")
        self.started = True

    async def shutdown(self):
        self.stopped = True

    def functions(self):
        return ["planner.next_slot"]

    async def call(self, name, payload):
        if name != "planner.next_slot":
            raise UnknownFunction(name)
        return b"slot:" + payload


LAST_FAKE: list[FakeDriver] = []


def build_fake(settings):
    driver = FakeDriver(settings)
    LAST_FAKE.append(driver)
    return driver


not_a_factory = "a string, not a callable"


class ModuleLifecycleTest(FunctionsTestCase):

    def test_disabled_by_default_is_module_disabled(self):
        cfg = Config.model_validate({"listener": {"uds": self.socket_path}})
        with TestClient(create_app(cfg)) as c:
            r = c.post("/functions/v1/call/x", content=b"")
        self.assert_envelope(r, 404, "module_disabled", False)

    def test_meta_advertises_functions_without_listener(self):
        with self.build() as c:
            modules = c.get("/v1/meta").json()["modules"]
        self.assertEqual([m["name"] for m in modules], ["functions"])
        self.assertEqual(modules[0]["mount"], "/functions")
        self.assertEqual(modules[0]["api_version"], "v1")
        self.assertNotIn("listener", modules[0])

    def test_unavailable_driver_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "grpc"):
            self.build(driver="grpc")

    def test_unknown_config_key_fails_boot(self):
        with self.assertRaises(Exception):
            self.build(drivr="exec")

    def test_unknown_function_config_key_fails_boot(self):
        with self.assertRaises(Exception):
            self.build({"a.b": {"command": self.script(ECHO), "timeout": 1}})

    def test_invalid_function_name_in_config_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "valid function name"):
            self.build({"Planner.Book": {"command": self.script(ECHO)}})

    def test_missing_executable_fails_boot(self):
        missing = os.path.join(self.base, "no-such-program")
        with self.assertRaisesRegex(ValueError, "executable"):
            self.build({"a": {"command": [missing]}})
        with self.assertRaisesRegex(ValueError, "PATH"):
            self.build({"a": {"command": ["jetty-no-such-program-4f9a"]}})

    def test_empty_command_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            self.build({"a": {"command": []}})

    def test_nonpositive_timeout_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "timeout_s"):
            self.build({"a": {"command": self.script(ECHO), "timeout_s": 0}})


class LibraryDriverTest(FunctionsTestCase):
    """functions-v1 §6: a driver that is a library, not a command."""

    def build_lib(self, driver, **extra) -> TestClient:
        cfg = Config.model_validate(
            {
                "listener": {"uds": self.socket_path},
                "modules": {"functions": {"enabled": True, "driver": driver, **extra}},
            }
        )
        return TestClient(create_app(cfg))

    def test_driver_by_import_path(self):
        LAST_FAKE.clear()
        with self.build_lib(f"{__name__}:build_fake", fake={"tag": 1}) as c:
            self.assertEqual(c.get("/functions/v1/list").json(), {"functions": ["planner.next_slot"]})
            r = c.post("/functions/v1/call/planner.next_slot", content=b"\x00\xff")
            self.assertEqual(r.content, b"slot:\x00\xff")
            r = c.post("/functions/v1/call/planner.book", content=b"")
            self.assert_envelope(r, 404, "unknown_function", False)
        driver = LAST_FAKE[-1]
        # The factory sees the whole table, including its own keys.
        self.assertEqual(driver.settings["fake"], {"tag": 1})
        self.assertTrue(driver.started)
        self.assertTrue(driver.stopped)

    def test_driver_by_registered_name(self):
        register_driver("fake-registered", build_fake)
        with self.build_lib("fake-registered") as c:
            r = c.post("/functions/v1/call/planner.next_slot", content=b"x")
        self.assertEqual(r.content, b"slot:x")

    def test_registering_a_name_twice_is_an_error(self):
        register_driver("fake-twice", build_fake)
        with self.assertRaisesRegex(ValueError, "already registered"):
            register_driver("fake-twice", build_fake)

    def test_startup_failure_aborts_boot(self):
        client = self.build_lib(f"{__name__}:build_fake", fake={"fail_startup": True})
        with self.assertRaisesRegex(RuntimeError, "unreachable"):
            client.__enter__()

    def test_unimportable_path_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "cannot import"):
            self.build_lib("jetty_no_such_package_4f9a.driver:build")

    def test_missing_attribute_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "not a callable"):
            self.build_lib(f"{__name__}:no_such_factory")
        with self.assertRaisesRegex(ValueError, "not a callable"):
            self.build_lib(f"{__name__}:not_a_factory")

    def test_unregistered_bare_name_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "not available"):
            self.build_lib("grpc")


class ListTest(FunctionsTestCase):

    def test_list_is_sorted_names(self):
        fns = {
            "planner.next_slot": {"command": self.script(ECHO)},
            "planner.book": {"command": self.script(ECHO)},
        }
        with self.build(fns) as c:
            r = c.get("/functions/v1/list")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"functions": ["planner.book", "planner.next_slot"]})

    def test_no_functions_configured_is_an_empty_list(self):
        with self.build() as c:
            self.assertEqual(c.get("/functions/v1/list").json(), {"functions": []})


class CallTest(FunctionsTestCase):

    def test_binary_payload_round_trips_untouched(self):
        with self.build({"echo": {"command": self.script(ECHO)}}) as c:
            r = c.post("/functions/v1/call/echo", content=BINARY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, BINARY)
        self.assertEqual(r.headers["content-type"], "application/octet-stream")

    def test_function_output_is_the_response(self):
        with self.build({"up": {"command": self.script(UPPER)}}) as c:
            r = c.post("/functions/v1/call/up", content=b"hello")
        self.assertEqual(r.content, b"HELLO")

    def test_any_content_type_is_accepted_and_ignored(self):
        with self.build({"echo": {"command": self.script(ECHO)}}) as c:
            for headers in (
                {"content-type": "application/json"},
                {"content-type": "text/plain"},
                {"content-type": "application/x-planner"},
            ):
                r = c.post("/functions/v1/call/echo", content=b"{not json", headers=headers)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.content, b"{not json")

    def test_empty_payload_both_ways(self):
        with self.build({"echo": {"command": self.script(ECHO)}}) as c:
            r = c.post("/functions/v1/call/echo")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"")

    def test_function_sees_its_name_in_the_environment(self):
        fns = {
            "planner.book": {"command": self.script(NAME)},
            "planner.cancel": {"command": self.script(NAME)},
        }
        with self.build(fns) as c:
            self.assertEqual(c.post("/functions/v1/call/planner.book").content, b"planner.book")
            self.assertEqual(c.post("/functions/v1/call/planner.cancel").content, b"planner.cancel")

    def test_unknown_function_is_404_unknown_function(self):
        with self.build({"echo": {"command": self.script(ECHO)}}) as c:
            r = c.post("/functions/v1/call/planner.book", content=b"x")
        self.assert_envelope(r, 404, "unknown_function", False)

    def test_invalid_name_is_400_before_the_driver(self):
        with self.build({"echo": {"command": self.script(ECHO)}}) as c:
            for bad in ("Echo", "a b", "a!b", "x" * 129):
                r = c.post(f"/functions/v1/call/{bad}", content=b"x")
                self.assert_envelope(r, 400, "invalid_request", False)

    def test_nonzero_exit_is_502_and_discards_stdout(self):
        with self.build({"fail": {"command": self.script(FAIL)}}) as c:
            with self.assertLogs("jetty.functions.exec", level="WARNING") as logs:
                r = c.post("/functions/v1/call/fail", content=b"x")
        self.assert_envelope(r, 502, "upstream_error", True)
        self.assertNotIn(b"partial output", r.content)
        # Stderr is relayed to the log (functions-v1 §6.1).
        self.assertTrue(any("boom" in line for line in logs.output), logs.output)

    def test_timeout_is_503(self):
        with self.build({"slow": {"command": self.script(SLOW), "timeout_s": 0.5}}) as c:
            r = c.post("/functions/v1/call/slow", content=b"x")
        self.assert_envelope(r, 503, "upstream_unavailable", True)

    def test_payloads_are_never_logged(self):
        marker_in = b"SECRET-REQUEST-7f3a"
        marker_out = b"SECRET-RESPONSE-9c1d"
        script = self.script(
            "import sys; sys.stdin.buffer.read(); "
            f"sys.stdout.buffer.write({marker_out!r})"
        )
        with self.build({"f": {"command": script}}) as c:
            with self.assertLogs("jetty", level="DEBUG") as logs:
                r = c.post("/functions/v1/call/f", content=marker_in)
        self.assertEqual(r.content, marker_out)
        joined = "\n".join(logs.output)
        self.assertNotIn(marker_in.decode(), joined)
        self.assertNotIn(marker_out.decode(), joined)
        self.assertIn("name=f", joined)

    def test_get_on_call_is_not_a_route(self):
        with self.build({"echo": {"command": self.script(ECHO)}}) as c:
            r = c.get("/functions/v1/call/echo")
        self.assert_envelope(r, 404, "not_found", False)


if __name__ == "__main__":
    absltest.main()
