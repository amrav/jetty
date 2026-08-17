"""Unit tests for the orchestrator's pure parts: config, templates, ports,
registry."""

from __future__ import annotations

import os
import socket
import sys
import time
import tomllib
from pathlib import Path
from unittest import mock

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
    render_service,
    render_str,
    validate_templates,
)  # resolve_config_path/resolve_command imported inside their tests


def config_from(text: str) -> OrchestratorConfig:
    return OrchestratorConfig.parse(tomllib.loads(text))


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

    def test_ready_uds_parses(self):
        cfg = config_from(
            MINIMAL + '\n[services.api.ready]\nuds = "{state_dir}/api.sock"\n'
        )
        self.assertEqual(cfg.services["api"].ready.uds, "{state_dir}/api.sock")

    def test_ready_probes_are_exclusive(self):
        with self.assertRaisesRegex(Exception, "at most one of http/tcp/uds/path"):
            config_from(
                MINIMAL
                + '\n[services.api.ready]\nuds = "/x.sock"\ntcp = "127.0.0.1:80"\n'
            )

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


class InheritanceTest(absltest.TestCase):
    def setUp(self):
        super().setUp()
        self.dir = self.create_tempdir()
        self.dir.create_file(
            "prod.toml",
            content="""
[instance]
name = "prod"

[ports]
api = 8000

[services.api]
cmd = ["./serve", "--port", "{ports.api}"]
[services.api.restart]
max_restarts = 3

[services.metrics]
cmd = ["./metrics"]
""",
        )

    def load(self, name: str) -> OrchestratorConfig:
        return OrchestratorConfig.load(os.path.join(self.dir.full_path, name))

    def test_child_overrides_and_inherits(self):
        self.dir.create_file(
            "dev.toml",
            content="""
extends = "prod.toml"

[instance]
name = "dev"

[ports]
api = "8000+"

[services.api.restart]
max_restarts = 10
""",
        )
        cfg = self.load("dev.toml")
        self.assertEqual(cfg.instance.name, "dev")
        self.assertEqual(cfg.ports["api"], "8000+")
        # Deep merge: the override touched restart.max_restarts only; the
        # inherited cmd and the sibling service survive.
        self.assertEqual(cfg.services["api"].restart.max_restarts, 10)
        self.assertEqual(cfg.services["api"].cmd, ["./serve", "--port", "{ports.api}"])
        self.assertEqual(cfg.services["api"].restart.window_seconds, 60.0)
        self.assertIn("metrics", cfg.services)

    def test_false_deletes_an_inherited_table(self):
        self.dir.create_file(
            "dev.toml",
            content='extends = "prod.toml"\n[services]\nmetrics = false\n',
        )
        cfg = self.load("dev.toml")
        self.assertNotIn("metrics", cfg.services)
        self.assertIn("api", cfg.services)

    def test_chain_inherits_transitively(self):
        self.dir.create_file(
            "staging.toml",
            content='extends = "prod.toml"\n[instance]\nname = "staging"\n',
        )
        self.dir.create_file(
            "dev.toml",
            content='extends = "staging.toml"\n[ports]\napi = "auto"\n',
        )
        cfg = self.load("dev.toml")
        self.assertEqual(cfg.instance.name, "staging")  # nearest ancestor wins
        self.assertEqual(cfg.ports["api"], "auto")
        self.assertIn("metrics", cfg.services)

    def test_cycle_and_missing_parent_are_clear_errors(self):
        self.dir.create_file("a.toml", content='extends = "b.toml"\n')
        self.dir.create_file("b.toml", content='extends = "a.toml"\n')
        with self.assertRaisesRegex(Exception, "cycle"):
            self.load("a.toml")
        self.dir.create_file("c.toml", content='extends = "nope.toml"\n')
        with self.assertRaisesRegex(Exception, "not found"):
            self.load("c.toml")

    def test_extends_cannot_escape_the_subtree_relatively(self):
        self.dir.create_file("dev.toml", content='extends = "../outside.toml"\n')
        with self.assertRaisesRegex(Exception, "outside"):
            self.load("dev.toml")


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
            validate_templates(cfg, self.create_tempdir().full_path)

    def test_ready_uds_renders_against_state_dir(self):
        cfg = config_from(
            MINIMAL + '\n[services.api.ready]\nuds = "{state_dir}/api.sock"\n'
        )
        ctx = build_context("dev", {}, "/state", "/logs")
        rendered = render_service(
            cfg.services["api"], ctx, self.create_tempdir().full_path
        )
        self.assertEqual(rendered.ready_uds, "/state/api.sock")

    def test_ready_uds_over_sun_path_cap_rejected(self):
        cfg = config_from(
            MINIMAL + '\n[services.api.ready]\nuds = "{state_dir}/api.sock"\n'
        )
        ctx = build_context("dev", {}, "/s" * 60, "/logs")
        with self.assertRaisesRegex(RenderError, "sun_path"):
            render_service(cfg.services["api"], ctx, self.create_tempdir().full_path)


class EnvSubstitutionTest(absltest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = build_context("dev", {"api": 1234}, "/s", "/l")
        self.base = self.create_tempdir().full_path

    def test_set_variable_substitutes(self):
        with mock.patch.dict(os.environ, {"ORC_TEST_LVL": "debug"}):
            self.assertEqual(
                render_str("--log-level={env.ORC_TEST_LVL}", self.ctx),
                "--log-level=debug",
            )

    def test_unset_without_default_is_a_clear_error(self):
        with mock.patch.dict(os.environ, clear=True):
            with self.assertRaisesRegex(RenderError, "ORC_TEST_LVL is not set"):
                render_str("{env.ORC_TEST_LVL}", self.ctx)

    def test_default_applies_when_unset_or_empty(self):
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(render_str("{env.ORC_TEST_LVL:-info}", self.ctx), "info")
        with mock.patch.dict(os.environ, {"ORC_TEST_LVL": ""}):
            self.assertEqual(render_str("{env.ORC_TEST_LVL:-info}", self.ctx), "info")
        with mock.patch.dict(os.environ, {"ORC_TEST_LVL": "warn"}):
            self.assertEqual(render_str("{env.ORC_TEST_LVL:-info}", self.ctx), "warn")

    def test_standalone_env_element_splices_argv(self):
        from jetty.orchestrator.render import render_argv

        with mock.patch.dict(os.environ, {"ORC_TEST_FLAGS": '--a "b c"'}):
            self.assertEqual(
                render_argv(["./run", "{env.ORC_TEST_FLAGS:-}"], self.ctx, self.base, "cmd"),
                [os.path.join(self.base, "run"), "--a", "b c"],
            )
        # Unset with an empty default: the element vanishes instead of
        # becoming an empty argument.
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(
                render_argv(["./run", "{env.ORC_TEST_FLAGS:-}"], self.ctx, self.base, "cmd"),
                [os.path.join(self.base, "run")],
            )
        # Embedded in a larger element: plain substitution, one argument.
        with mock.patch.dict(os.environ, {"ORC_TEST_FLAGS": "a b"}):
            self.assertEqual(
                render_argv(["./run", "--x={env.ORC_TEST_FLAGS:-}"], self.ctx, self.base, "cmd"),
                [os.path.join(self.base, "run"), "--x=a b"],
            )

    def test_env_reaches_every_rendered_field(self):
        cfg = config_from(
            MINIMAL
            + 'env = { LEVEL = "{env.ORC_TEST_LVL:-info}" }\n'
            + '[gates.g]\ncheck = ["test", "-f", "{env.ORC_TEST_FLAG_FILE:-flag}"]\n'
        )
        with mock.patch.dict(os.environ, clear=True):
            validate_templates(cfg, self.base)  # defaults keep it valid

    def test_port_specs_render_env(self):
        from jetty.orchestrator.config import parse_port_spec
        from jetty.orchestrator.render import render_port_specs

        # A digit string is a fixed port — what env substitution produces.
        self.assertEqual(parse_port_spec("8080"), (8080, 8080))

        cfg = config_from(
            '[ports]\nhttp = "{env.ORC_TEST_PORT:-8080+}"\n' + MINIMAL
        )
        ctx = build_context("dev", {}, "/s", "/l")
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(render_port_specs(cfg, ctx)["http"], "8080+")
        with mock.patch.dict(os.environ, {"ORC_TEST_PORT": "9000"}):
            self.assertEqual(render_port_specs(cfg, ctx)["http"], "9000")
        with mock.patch.dict(os.environ, {"ORC_TEST_PORT": "nonsense"}):
            with self.assertRaisesRegex(RenderError, "not a valid port spec"):
                render_port_specs(cfg, ctx)

    def test_port_spec_cannot_reference_other_ports(self):
        cfg = config_from('[ports]\na = "auto"\nb = "{ports.a}"\n' + MINIMAL)
        with self.assertRaisesRegex(ValueError, "unknown placeholder"):
            validate_templates(cfg, self.base)

    def test_cwd_from_env_with_tilde_default(self):
        from jetty.orchestrator.render import render_service

        svc = config_from(
            MINIMAL + 'cwd = "{env.ORC_TEST_PROJECT_DIR:-~/projects}"\n'
        ).services["api"]
        with mock.patch.dict(os.environ, {"ORC_TEST_PROJECT_DIR": "/opt/proj"}):
            self.assertEqual(render_service(svc, self.ctx, self.base).cwd, "/opt/proj")
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(
                render_service(svc, self.ctx, self.base).cwd,
                os.path.expanduser("~/projects"),
            )
        # A tilde inside the variable's value expands too.
        with mock.patch.dict(os.environ, {"ORC_TEST_PROJECT_DIR": "~/work"}):
            self.assertEqual(
                render_service(svc, self.ctx, self.base).cwd,
                os.path.expanduser("~/work"),
            )

    def test_string_cmd_form_is_shell_split_not_a_shell(self):
        cfg = config_from(MINIMAL.replace('["true"]', '"./run --flag \'two words\' && echo hi"'))
        self.assertEqual(
            cfg.services["api"].cmd,
            ["./run", "--flag", "two words", "&&", "echo", "hi"],
        )


class ConfigPathTest(absltest.TestCase):
    def setUp(self):
        super().setUp()
        self.base = self.create_tempdir().full_path

    def test_siblings_and_subtrees_resolve_absolute_paths_pass(self):
        from jetty.orchestrator.render import resolve_config_path

        self.assertEqual(
            resolve_config_path("run.sh", self.base, "cmd"),
            os.path.join(self.base, "run.sh"),
        )
        self.assertEqual(
            resolve_config_path("scripts/run.sh", self.base, "cmd"),
            os.path.join(self.base, "scripts/run.sh"),
        )
        self.assertEqual(resolve_config_path("/usr/bin/env", self.base, "cmd"), "/usr/bin/env")

    def test_escaping_the_config_subtree_is_an_error(self):
        from jetty.orchestrator.render import resolve_config_path

        with self.assertRaisesRegex(RenderError, "outside the config"):
            resolve_config_path("../sibling/run.sh", self.base, "cmd")

    def test_tilde_expands_and_counts_as_absolute(self):
        from jetty.orchestrator.render import resolve_config_path

        home = os.path.expanduser("~")
        self.assertEqual(resolve_config_path("~", self.base, "cwd"), home)
        self.assertEqual(
            resolve_config_path("~/work", self.base, "cwd"),
            os.path.join(home, "work"),
        )

    def test_home_placeholder(self):
        ctx = build_context("dev", {}, "/s", "/l")
        self.assertEqual(
            render_str("{home}/x", ctx), os.path.expanduser("~") + "/x"
        )

    def test_bare_command_names_are_path_lookups(self):
        from jetty.orchestrator.render import resolve_command

        self.assertEqual(resolve_command(["python", "-V"], self.base, "cmd"), ["python", "-V"])
        resolved = resolve_command(["./run.sh"], self.base, "cmd")
        self.assertEqual(resolved, [os.path.join(self.base, "run.sh")])

    def test_validate_templates_catches_escaping_cwd(self):
        cfg = config_from(MINIMAL + 'cwd = "../elsewhere"\n')
        with self.assertRaisesRegex(ValueError, "outside the config"):
            validate_templates(cfg, self.base)

    def test_instance_workdir_is_the_default_cwd(self):
        from jetty.orchestrator.render import render_service

        svc = config_from(MINIMAL).services["api"]
        ctx = build_context("dev", {}, "/s", "/l")
        self.assertEqual(
            render_service(svc, ctx, self.base, default_cwd="/somewhere").cwd,
            "/somewhere",
        )
        # No workdir configured: the config file's directory.
        self.assertEqual(render_service(svc, ctx, self.base).cwd, self.base)
        # An explicit per-service cwd still wins over the instance default.
        svc = config_from(MINIMAL + 'cwd = "~"\n').services["api"]
        self.assertEqual(
            render_service(svc, ctx, self.base, default_cwd="/somewhere").cwd,
            os.path.expanduser("~"),
        )

    def test_escaping_instance_workdir_rejected_at_check(self):
        cfg = config_from(
            '[instance]\nname = "dev"\nworkdir = "../out"\n'
            '[services.api]\ncmd = ["true"]\n'
        )
        with self.assertRaisesRegex(ValueError, "outside the config"):
            validate_templates(cfg, self.base)


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


class ResolverConfigTest(absltest.TestCase):
    def test_provides_defaults_to_resolver_name(self):
        cfg = config_from(
            '[resolvers.mybin]\ncmd = ["true"]\n'
            + MINIMAL.replace('["true"]', '["{bin.mybin}"]')
        )
        self.assertEqual(cfg.resolvers["mybin"].provides, ["mybin"])

    def test_unresolved_bin_reference_rejected(self):
        with self.assertRaisesRegex(Exception, "no resolver provides"):
            config_from(MINIMAL.replace('["true"]', '["{bin.ghost}"]'))

    def test_duplicate_provides_rejected(self):
        with self.assertRaisesRegex(Exception, "provided by both"):
            config_from(
                '[resolvers.a]\ncmd = ["true"]\nprovides = ["x"]\n'
                '[resolvers.b]\ncmd = ["true"]\nprovides = ["x"]\n' + MINIMAL
            )

    def test_gate_cannot_use_bin_placeholders(self):
        with self.assertRaisesRegex(Exception, "gate commands cannot"):
            config_from(
                '[resolvers.x]\ncmd = ["true"]\n'
                '[gates.g]\ncheck = ["{bin.x}"]\n' + MINIMAL
            )


class ResolverOutputTest(absltest.TestCase):
    def parse(self, provides, stdout):
        from jetty.orchestrator.resolvers import parse_output

        return parse_output("rel", provides, stdout)

    def test_single_name_bare_path(self):
        self.assertEqual(self.parse(["app"], "/opt/app-v2\n"), {"app": "/opt/app-v2"})

    def test_key_value_lines_any_order_with_comments(self):
        got = self.parse(
            ["cp", "harness"],
            "# release 2026-08-12\nharness=/opt/h\n\ncp=/opt/cp\n",
        )
        self.assertEqual(got, {"cp": "/opt/cp", "harness": "/opt/h"})

    def test_unknown_repeated_and_missing_names_rejected(self):
        from jetty.orchestrator.resolvers import ResolveError

        with self.assertRaisesRegex(ResolveError, "unknown name"):
            self.parse(["a"], "b=/x\n")
        with self.assertRaisesRegex(ResolveError, "twice"):
            self.parse(["a"], "a=/x\na=/y\n")
        with self.assertRaisesRegex(ResolveError, "did not return"):
            self.parse(["a", "b"], "a=/x\n")
        with self.assertRaisesRegex(ResolveError, "unparseable"):
            self.parse(["a", "b"], "/just/a/path\n")


class MaterializeTest(absltest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.create_tempdir("binroot").full_path)
        self.srcdir = Path(self.create_tempdir("src").full_path)

    def _source(self, name="app", content="v1") -> str:
        path = self.srcdir / name
        path.write_text(content)
        os.chmod(path, 0o755)
        return str(path)

    def materialize(self, source, keep_days=7.0) -> str:
        from jetty.orchestrator.resolvers import materialize

        return materialize(source, self.root, keep_days)[0]

    def fingerprint(self, source) -> str:
        from jetty.orchestrator.resolvers import materialize

        return materialize(source, self.root, 7.0)[1]

    def test_copies_once_and_caches_by_path(self):
        source = self._source()
        dest = self.materialize(source)
        self.assertTrue(dest.startswith(str(self.root)))
        self.assertEqual(Path(dest).read_text(), "v1")
        # Prove the second call is a cache hit: corrupt the copy; an
        # unchanged source must not overwrite it.
        Path(dest).write_text("sentinel")
        self.assertEqual(self.materialize(source), dest)
        self.assertEqual(Path(dest).read_text(), "sentinel")

    def test_changed_source_is_recopied_to_the_same_name(self):
        source = self._source(content="v1")
        dest = self.materialize(source)
        src = Path(source)
        src.write_text("v2-longer")  # size and mtime both move
        self.assertEqual(self.materialize(source), dest)
        self.assertEqual(Path(dest).read_text(), "v2-longer")

    def test_vanished_source_falls_back_to_the_copy(self):
        from jetty.orchestrator.resolvers import ResolveError

        source = self._source()
        dest = self.materialize(source)
        os.unlink(source)
        self.assertEqual(self.materialize(source), dest)
        self.assertEqual(Path(dest).read_text(), "v1")
        # ...but no copy and no source is a hard failure.
        gone = str(self.srcdir / "never-existed")
        with self.assertRaisesRegex(ResolveError, "no cached copy"):
            self.materialize(gone)

    def test_distinct_sources_never_collide(self):
        a = self._source("app")
        b = str(self.srcdir / "sub")
        os.mkdir(b)
        b_app = Path(b) / "app"  # same basename, different path
        b_app.write_text("other")
        self.assertNotEqual(self.materialize(a), self.materialize(str(b_app)))

    def test_unused_copies_expire_and_used_ones_do_not(self):
        old = self.materialize(self._source("old"))
        ancient = time.time() - 8 * 86400
        for suffix in ("", ".src"):
            os.utime(old + suffix, (ancient, ancient))
        kept = self.materialize(self._source("kept"))
        # Any later materialize prunes; `kept` was just touched and survives.
        self.materialize(self._source("kept"))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(kept))

    def test_directory_source_rejected(self):
        from jetty.orchestrator.resolvers import ResolveError

        with self.assertRaisesRegex(ResolveError, "regular file"):
            self.materialize(str(self.srcdir))

    def test_fingerprint_tracks_content_and_survives_vanishing(self):
        source = self._source(content="v1")
        fp1 = self.fingerprint(source)
        Path(source).write_text("v2-longer")  # in-place release
        fp2 = self.fingerprint(source)
        self.assertNotEqual(fp1, fp2)
        os.unlink(source)  # gone: the sidecar remembers what we run
        self.assertEqual(self.fingerprint(source), fp2)


class ResolverGateConfigTest(absltest.TestCase):
    def test_unknown_resolver_gate_rejected(self):
        with self.assertRaisesRegex(Exception, "unknown gate"):
            config_from(
                '[resolvers.app]\ncmd = ["true"]\nrequires = ["creds"]\n'
                + MINIMAL.replace('["true"]', '["{bin.app}"]')
            )


class ConsoleTest(absltest.TestCase):
    def test_line_buffer_reassembles_chunks(self):
        from jetty.orchestrator.console import LineBuffer

        buf = LineBuffer()
        self.assertEqual(buf.feed(b"hel"), [])
        self.assertEqual(buf.feed(b"lo\nwor"), ["hello"])
        self.assertEqual(buf.feed(b"ld\npartial"), ["world"])
        self.assertEqual(buf.flush(), "partial")
        self.assertIsNone(buf.flush())

    def test_prefixer_pads_and_colors_only_when_asked(self):
        from jetty.orchestrator.console import Prefixer

        plain = Prefixer(["api", "webserver"], color=False)
        self.assertEqual(plain.format("api", "x"), "[api      ] x")
        self.assertEqual(plain.format("webserver", "x"), "[webserver] x")
        colored = Prefixer(["api", "web"], color=True)
        self.assertIn("\x1b[", colored.format("api", "x"))
        # Distinct services get distinct colours.
        self.assertNotEqual(
            colored.format("api", "x").split("[api")[0],
            colored.format("web", "x").split("[web")[0],
        )
        # Unknown names (a stray log file) degrade to a plain label.
        self.assertEqual(plain.format("ghost", "x"), "[ghost] x")


class ServiceEnvTest(absltest.TestCase):
    def test_cgroup_root_exported_only_under_cgroup_containment(self):
        from jetty.orchestrator.containment import CgroupBackend, PgroupBackend
        from jetty.orchestrator.supervisor import service_extra_env

        cg = CgroupBackend(Path("/sys/fs/cgroup/fake.scope"))
        env = service_extra_env("dev", cg)
        self.assertEqual(env["JETTY_ORC_INSTANCE"], "dev")
        self.assertEqual(env["JETTY_ORC_CGROUP_ROOT"], "/sys/fs/cgroup/fake.scope")

        env = service_extra_env("dev", PgroupBackend())
        self.assertNotIn("JETTY_ORC_CGROUP_ROOT", env)


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
