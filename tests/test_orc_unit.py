"""Unit tests for the orchestrator's pure parts: config, templates, ports,
registry."""

from __future__ import annotations

import os
import socket
import sys
import tomllib
from pathlib import Path

from absl.testing import absltest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from jetty.orchestrator import procfs  # noqa: E402
from jetty.orchestrator.config import OrchestratorConfig, start_order  # noqa: E402
from jetty.orchestrator.ports import PortError, allocate_ports  # noqa: E402
from jetty.orchestrator.registry import Registry, supervisor_alive  # noqa: E402
from jetty.orchestrator.render import (  # noqa: E402
    RenderError,
    build_context,
    render_str,
    validate_templates,
)


def config_from(text: str) -> OrchestratorConfig:
    return OrchestratorConfig.model_validate(tomllib.loads(text))


MINIMAL = """
[instance]
name = "dev"

[services.api]
cmd = ["true"]
"""


class ConfigTest(absltest.TestCase):
    def test_minimal_loads(self):
        cfg = config_from(MINIMAL)
        self.assertEqual(cfg.instance.name, "dev")
        self.assertEqual(cfg.services["api"].restart.no_restart_exit, [2])

    def test_unknown_key_rejected(self):
        with self.assertRaisesRegex(Exception, "max_restart"):
            config_from(
                MINIMAL + "\n[services.api.restart]\nmax_restart = 5\n"
            )

    def test_after_cycle_rejected(self):
        with self.assertRaisesRegex(Exception, "cycle"):
            config_from(
                """
[instance]
name = "dev"
[services.a]
cmd = ["true"]
after = ["b"]
[services.b]
cmd = ["true"]
after = ["a"]
"""
            )

    def test_unknown_gate_rejected(self):
        with self.assertRaisesRegex(Exception, "unknown gate"):
            config_from(MINIMAL + 'requires = ["creds"]\n')

    def test_duplicate_fixed_port_rejected(self):
        with self.assertRaisesRegex(Exception, "both fixed"):
            config_from(
                '[ports]\na = 8000\nb = 8000\n' + MINIMAL
            )

    def test_port_spec_forms(self):
        cfg = config_from(
            '[ports]\na = "auto"\nb = 8000\nc = "8000+"\nd = "9000-9020"\n'
            + MINIMAL
        )
        self.assertEqual(len(cfg.ports), 4)
        for bad in ('"8000plus"', '"9020-9000"', '"0+"', '"-5"', '"70000"', "70000"):
            with self.assertRaisesRegex(Exception, "port", msg=bad):
                config_from(f"[ports]\nx = {bad}\n" + MINIMAL)

    def test_bad_name_rejected(self):
        with self.assertRaisesRegex(Exception, "must match"):
            config_from(MINIMAL.replace('"dev"', '"Dev Instance"'))

    def test_start_order_respects_after(self):
        cfg = config_from(
            """
[instance]
name = "dev"
[services.web]
cmd = ["true"]
after = ["api"]
[services.api]
cmd = ["true"]
after = ["db"]
[services.db]
cmd = ["true"]
"""
        )
        self.assertEqual(start_order(cfg.services), ["db", "api", "web"])


class RenderTest(absltest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = build_context("dev", {"api": 1234}, "/state", "/logs")

    def test_substitution(self):
        self.assertEqual(
            render_str("http://127.0.0.1:{ports.api}/x", self.ctx),
            "http://127.0.0.1:1234/x",
        )
        self.assertEqual(render_str("{instance.name}-{state_dir}", self.ctx), "dev-/state")

    def test_unknown_placeholder(self):
        with self.assertRaisesRegex(RenderError, "ports.apii"):
            render_str("{ports.apii}", self.ctx)

    def test_brace_escape(self):
        self.assertEqual(render_str('{{"a": {ports.api}}}', self.ctx), '{"a": 1234}')

    def test_validate_templates_catches_typo(self):
        cfg = config_from(
            MINIMAL.replace('["true"]', '["true", "{ports.nope}"]')
        )
        with self.assertRaisesRegex(ValueError, "ports.nope"):
            validate_templates(cfg)


class PortsTest(absltest.TestCase):
    def test_auto_ports_distinct(self):
        got = allocate_ports({"a": "auto", "b": "auto", "c": "auto"})
        self.assertLen(set(got.values()), 3)

    def test_fixed_port_returned(self):
        # Find a free port first, then ask for it explicitly.
        free = allocate_ports({"x": "auto"})["x"]
        self.assertEqual(allocate_ports({"x": free})["x"], free)

    def test_occupied_fixed_port_refused(self):
        port = self.occupy()
        with self.assertRaisesRegex(PortError, "refusing to reclaim"):
            allocate_ports({"api": port})

    def test_prefer_scans_past_occupied(self):
        port = self.occupy()
        got = allocate_ports({"api": f"{port}+"})["api"]
        self.assertGreater(got, port)

    def test_prefer_takes_default_when_free(self):
        free = allocate_ports({"x": "auto"})["x"]
        self.assertEqual(allocate_ports({"x": f"{free}+"})["x"], free)

    def test_single_port_range_reads_as_fixed(self):
        port = self.occupy()
        with self.assertRaisesRegex(PortError, "refusing to reclaim"):
            allocate_ports({"api": f"{port}-{port}"})

    def test_range_exhausted_refused(self):
        for _ in range(20):  # find two adjacent free ports we can occupy
            low = self.occupy()
            try:
                allocate_ports({"next": low + 1})
            except PortError:
                continue
            self.occupy(low + 1)
            with self.assertRaisesRegex(PortError, "no free port in"):
                allocate_ports({"api": f"{low}-{low + 1}"})
            return
        self.fail("could not find two adjacent free ports to occupy")

    def test_batch_never_hands_out_duplicates(self):
        free = allocate_ports({"x": "auto"})["x"]
        got = allocate_ports({"a": f"{free}+", "b": f"{free}+"})
        self.assertNotEqual(got["a"], got["b"])

    def occupy(self, port: int = 0) -> int:
        holder = socket.socket()
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        return holder.getsockname()[1]


class RegistryTest(absltest.TestCase):
    def test_roundtrip_and_alive(self):
        root = self.create_tempdir()
        reg = Registry(Path(root.full_path))
        record = {
            "name": "dev",
            "supervisor_pid": os.getpid(),
            "supervisor_start_ticks": procfs.start_ticks(os.getpid()),
        }
        reg.write(record)
        loaded = reg.load("dev")
        self.assertEqual(loaded["name"], "dev")
        self.assertTrue(supervisor_alive(loaded))
        # Same pid, wrong start ticks: a reused pid must not count as alive.
        loaded["supervisor_start_ticks"] = 1
        self.assertFalse(supervisor_alive(loaded))
        reg.remove("dev")
        self.assertIsNone(reg.load("dev"))


if __name__ == "__main__":
    absltest.main()
