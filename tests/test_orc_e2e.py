"""End-to-end orchestrator tests: a real supervisor process, real children.

Containment is pinned to "pgroup" so the suite runs anywhere (CI containers
included) without a systemd user manager; the cgroup/scope backends are
exercised by `jetty-orc doctor` and manual runs, and their kill/enumerate
mechanics reduce to kernel guarantees rather than orchestrator logic.

Everything lives under absltest's temp dir; the registry root is redirected
via JETTY_ORC_ROOT so tests never touch ~/.local/state.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

from absl.testing import absltest

IMPORT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)

STARTUP_TIMEOUT_S = 20
SHUTDOWN_TIMEOUT_S = 15

#: A child that spawns a grandchild, records both pids, then sleeps — the
#: minimal model of "service with a helper process tree".
TREE_SCRIPT = """
import os, pathlib, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
pathlib.Path("pids.txt").write_text(f"{os.getpid()}\\n{grandchild.pid}\\n")
time.sleep(300)
"""

#: Exits immediately unless the `flag` file exists; the gate watches the same
#: file, so this models a credential-dependent service.
GATED_SCRIPT = """
import pathlib, sys, time
if not pathlib.Path("flag").exists():
    print("no credentials, dying", flush=True)
    sys.exit(1)
pathlib.Path("gated-ready.txt").write_text("up")
time.sleep(300)
"""


class OrcE2ETest(absltest.TestCase):
    def setUp(self):
        super().setUp()
        self.workdir = self.create_tempdir()
        origin = os.getcwd()
        self.addCleanup(os.chdir, origin)
        os.chdir(self.workdir.full_path)
        existing = os.environ.get("PYTHONPATH")
        self.env = {
            **os.environ,
            "PYTHONPATH": (
                f"{IMPORT_ROOT}{os.pathsep}{existing}" if existing else IMPORT_ROOT
            ),
            "JETTY_ORC_ROOT": os.path.join(self.workdir.full_path, "orcroot"),
            "JETTY_ORC_LOG_ROOT": os.path.join(self.workdir.full_path, "orclogs"),
            "JETTY_ORC_BIN_ROOT": os.path.join(self.workdir.full_path, "orcbin"),
        }

    # -- harness --

    def write_config(self, body: str, filename: str = "orc.toml") -> str:
        return self.workdir.create_file(filename, content=body).full_path

    def orc(self, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "jetty.orchestrator", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.env,
        )
        self.addCleanup(self.terminate, proc)
        return proc

    def orc_run(self, *args: str) -> tuple[int, str]:
        result = subprocess.run(
            [sys.executable, "-m", "jetty.orchestrator", *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        return result.returncode, result.stdout + result.stderr

    def terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()

    def output_of(self, proc: subprocess.Popen) -> str:
        return proc.stdout.read().decode() if proc.stdout else ""

    def record(self, name: str = "dev") -> dict | None:
        """The registry record whose name is `name` or `name-<suffix>` —
        instance names carry a random suffix unless pinned with --name."""
        registry = os.path.join(self.env["JETTY_ORC_ROOT"], "registry")
        try:
            entries = sorted(os.listdir(registry))
        except OSError:
            return None
        for entry in entries:
            if entry == f"{name}.json" or (
                entry.startswith(f"{name}-") and entry.endswith(".json")
            ):
                try:
                    with open(os.path.join(registry, entry)) as f:
                        return json.load(f)
                except (OSError, json.JSONDecodeError):
                    return None
        return None

    def wait_until(self, predicate, proc: subprocess.Popen | None = None, timeout=STARTUP_TIMEOUT_S, what=""):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if proc is not None and proc.poll() is not None:
                self.fail(
                    f"supervisor exited early (code {proc.returncode}) waiting "
                    f"for {what}:\n{self.output_of(proc)}"
                )
            time.sleep(0.1)
        extra = f":\n{self.output_of(proc)}" if proc and proc.poll() is not None else ""
        self.fail(f"timed out waiting for {what or predicate}{extra}")

    def service_state(self, service: str, name: str = "dev") -> str | None:
        record = self.record(name)
        if not record:
            return None
        return record.get("services", {}).get(service, {}).get("state")

    @staticmethod
    def pid_gone(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    # -- tests --

    def test_up_ready_then_sigint_kills_whole_tree(self):
        self.workdir.create_file("tree.py", content=TREE_SCRIPT)
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[ports]
http = "auto"

[services.web]
cmd = ["{sys.executable}", "-m", "http.server", "{{ports.http}}", "--bind", "127.0.0.1"]
[services.web.ready]
http = "http://127.0.0.1:{{ports.http}}/"

[services.tree]
cmd = ["{sys.executable}", "tree.py"]
after = ["web"]
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: self.service_state("web") == "running"
            and self.service_state("tree") == "running"
            and os.path.exists("pids.txt"),
            proc,
            what="both services running",
        )
        record = self.record()
        self.assertIn("http", record["ports"])
        with open("pids.txt") as f:
            pids = [int(x) for x in f.read().split()]
        self.assertLen(pids, 2)

        # `ps` shows the whole tree: the tree service's child AND grandchild,
        # the grandchild indented under its parent.
        rc, output = self.orc_run("ps", "dev")
        self.assertEqual(rc, 0, output)
        self.assertIn("[tree]", output)
        for pid in pids:
            self.assertIn(str(pid), output)
        parent_line, child_line = (
            line for line in output.splitlines() if "tree.py" in line or "sleep" in line
        )
        self.assertLess(
            len(parent_line) - len(parent_line.lstrip()),
            len(child_line) - len(child_line.lstrip()),
            f"grandchild should be indented under its parent:\n{output}",
        )

        run_dirs = os.listdir(self.env["JETTY_ORC_LOG_ROOT"])
        self.assertLen(run_dirs, 1)
        self.assertStartsWith(run_dirs[0], "dev-")  # instance + timestamp
        run_dir = os.path.join(self.env["JETTY_ORC_LOG_ROOT"], run_dirs[0])
        self.assertEqual(record["logs_dir"], run_dir)
        self.assertContainsSubset(
            {"web.log", "tree.log"}, set(os.listdir(run_dir))
        )

        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        self.assertEqual(rc, 0, self.output_of(proc))
        for pid in pids:
            self.assertTrue(self.pid_gone(pid), f"pid {pid} survived teardown")
        self.assertIsNone(self.record(), "registry entry should be gone")

    def test_crash_loop_exhausts_budget_and_fails_instance(self):
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[services.boom]
cmd = ["{sys.executable}", "-c", "import sys; print('kaboom'); sys.exit(3)"]
[services.boom.restart]
max_restarts = 2
backoff_initial_seconds = 0.05
backoff_max_seconds = 0.1

[services.bystander]
cmd = ["{sys.executable}", "-c", "import time; time.sleep(300)"]
"""
        )
        proc = self.orc("up", "-c", config)
        rc = proc.wait(timeout=30)
        output = self.output_of(proc)
        self.assertEqual(rc, 1, output)
        self.assertIn("exited 3 times", output)
        self.assertIn("kaboom", output)  # the log tail travels with the error
        # The failed instance leaves a post-mortem registry record.
        record = self.record()
        self.assertIsNotNone(record)
        self.assertStartsWith(record["state"], "failed")

    def test_no_restart_exit_fails_immediately(self):
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[services.cfgbug]
cmd = ["{sys.executable}", "-c", "import sys; sys.exit(7)"]
[services.cfgbug.restart]
no_restart_exit = [7]
"""
        )
        proc = self.orc("up", "-c", config)
        rc = proc.wait(timeout=30)
        output = self.output_of(proc)
        self.assertEqual(rc, 1, output)
        self.assertIn("not worth retrying", output)
        self.assertNotIn("restarting", output)

    def test_gate_blocks_without_burning_budget_then_recovers(self):
        self.workdir.create_file("gated.py", content=GATED_SCRIPT)
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[gates.creds]
check = ["{sys.executable}", "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('flag').exists() else 1)"]
recheck_seconds = 0.2

[services.gated]
cmd = ["{sys.executable}", "gated.py"]
requires = ["creds"]
[services.gated.restart]
max_restarts = 1
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: self.service_state("gated") == "blocked",
            proc,
            what="service blocked on gate",
        )
        record = self.record()
        self.assertEqual(record["services"]["gated"]["blocked_on"], ["creds"])
        # Sit blocked well past what the restart budget would tolerate as
        # crashes: blocked time must not count against it.
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), self.output_of(proc) if proc.poll() else "")

        self.workdir.create_file("flag", content="ok")
        self.wait_until(
            lambda: self.service_state("gated") == "running"
            and os.path.exists("gated-ready.txt"),
            proc,
            what="service revived after gate passed",
        )
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0, self.output_of(proc))

    def test_killed_service_is_restarted(self):
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[services.sleeper]
cmd = ["{sys.executable}", "-c", "import time; time.sleep(300)"]
[services.sleeper.restart]
backoff_initial_seconds = 0.05
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: self.service_state("sleeper") == "running",
            proc,
            what="sleeper running",
        )
        first_pid = self.record()["services"]["sleeper"]["pid"]
        os.kill(first_pid, signal.SIGKILL)
        self.wait_until(
            lambda: (
                self.service_state("sleeper") == "running"
                and self.record()["services"]["sleeper"]["pid"] not in (None, first_pid)
            ),
            proc,
            what="sleeper restarted with a new pid",
        )
        self.assertGreaterEqual(self.record()["services"]["sleeper"]["restarts"], 1)
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0, self.output_of(proc))

    def test_same_config_runs_twice_pinned_name_refused(self):
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[ports]
api = "auto"

[services.sleeper]
cmd = ["{sys.executable}", "-c", "import time; time.sleep(300)"]
"""
        )
        first = self.orc("up", "-c", config)
        self.wait_until(
            lambda: self.service_state("sleeper") == "running", first, what="first up"
        )
        first_name = self.record()["name"]
        self.assertRegex(first_name, r"^dev-[0-9a-f]{4}$")

        # Same config again: a fresh suffix, no collision, disjoint ports.
        second = self.orc("up", "-c", config)
        self.wait_until(
            lambda: len(
                [
                    e
                    for e in os.listdir(
                        os.path.join(self.env["JETTY_ORC_ROOT"], "registry")
                    )
                    if e.startswith("dev-")
                ]
            )
            == 2,
            second,
            what="second instance registered",
        )
        rc, output = self.orc_run("ls")
        self.assertEqual(output.count("dev-"), 2, output)

        # A prefix query with two matches must refuse rather than guess.
        rc, output = self.orc_run("status", "dev")
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous", output)

        # Pinning with --name gives back the old exclusive behaviour.
        rc, output = self.orc_run("up", "-c", config, "--name", first_name)
        self.assertEqual(rc, 1)
        self.assertIn("already running", output)

        for proc in (first, second):
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=SHUTDOWN_TIMEOUT_S)

    def test_resolver_switches_binary_on_restart(self):
        """The release-deploy story: the resolver points at v2, the running v1
        process is untouched, and the next respawn comes up on v2."""
        for version in ("v1", "v2"):
            path = os.path.join(self.workdir.full_path, f"app-{version}")
            with open(path, "w") as f:
                f.write(
                    f"#!{sys.executable}\nimport pathlib, time\n"
                    f"pathlib.Path('ran-{version}').touch()\ntime.sleep(300)\n"
                )
            os.chmod(path, 0o755)
        self.workdir.create_file("target.txt", content=f"{self.workdir.full_path}/app-v1\n")
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[resolvers.app]
cmd = ["cat", "target.txt"]
cache_seconds = 0.0

[services.app]
cmd = ["{{bin.app}}"]
[services.app.restart]
backoff_initial_seconds = 0.05
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: os.path.exists("ran-v1"), proc, what="v1 running"
        )
        record = self.record()
        self.assertEqual(
            record["resolvers"]["app"]["binaries"]["app"],
            f"{self.workdir.full_path}/app-v1",
        )
        with open("target.txt", "w") as f:  # the release moves
            f.write(f"{self.workdir.full_path}/app-v2\n")
        v1_pid = record["services"]["app"]["pid"]
        os.kill(v1_pid, signal.SIGKILL)
        self.wait_until(
            lambda: os.path.exists("ran-v2"), proc, what="v2 after restart"
        )
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0, self.output_of(proc))

    def test_pinned_binaries_restart_together_only_on_release_change(self):
        """Two services pinned by one resolver. A crash on an unchanged
        release restarts only the crasher; a crash after the release moved
        drags the sibling along (budget-free) so the pair never runs split
        across versions."""
        for svc in ("a", "b"):
            for version in ("v1", "v2"):
                path = os.path.join(self.workdir.full_path, f"{svc}-{version}")
                with open(path, "w") as f:
                    f.write(
                        f"#!{sys.executable}\nimport pathlib, time\n"
                        f"pathlib.Path('ran-{svc}-{version}').touch()\ntime.sleep(300)\n"
                    )
                os.chmod(path, 0o755)

        def manifest(version: str) -> str:
            return "".join(
                f"{svc}={self.workdir.full_path}/{svc}-{version}\n"
                for svc in ("a", "b")
            )

        self.workdir.create_file("manifest.txt", content=manifest("v1"))
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[resolvers.release]
cmd = ["cat", "manifest.txt"]
provides = ["a", "b"]
cache_seconds = 0.0

[services.svc_a]
cmd = ["{{bin.a}}"]
[services.svc_a.restart]
backoff_initial_seconds = 0.05

[services.svc_b]
cmd = ["{{bin.b}}"]
[services.svc_b.restart]
backoff_initial_seconds = 0.05
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: os.path.exists("ran-a-v1") and os.path.exists("ran-b-v1"),
            proc,
            what="both on v1",
        )
        b_pid_v1 = self.record()["services"]["svc_b"]["pid"]

        # Crash svc_a with the release unchanged: svc_b must not move.
        os.kill(self.record()["services"]["svc_a"]["pid"], signal.SIGKILL)
        self.wait_until(
            lambda: (
                self.service_state("svc_a") == "running"
                and self.record()["services"]["svc_a"]["restarts"] >= 1
            ),
            proc,
            what="svc_a restarted on the same release",
        )
        self.assertEqual(self.record()["services"]["svc_b"]["pid"], b_pid_v1)
        self.assertFalse(os.path.exists("ran-a-v2"))

        # The release moves; crash svc_a: both must come up on v2, and
        # svc_b's bounce must not spend its restart budget.
        with open("manifest.txt", "w") as f:
            f.write(manifest("v2"))
        os.kill(self.record()["services"]["svc_a"]["pid"], signal.SIGKILL)
        self.wait_until(
            lambda: os.path.exists("ran-a-v2") and os.path.exists("ran-b-v2"),
            proc,
            what="both on v2",
        )
        self.wait_until(
            lambda: self.service_state("svc_b") == "running",
            proc,
            what="svc_b running again",
        )
        self.assertEqual(self.record()["services"]["svc_b"]["restarts"], 0)

        # An IN-PLACE release: same paths, new content. The fingerprint in
        # the generation key must catch it and bounce the sibling.
        b_pid_v2 = self.record()["services"]["svc_b"]["pid"]
        with open(f"{self.workdir.full_path}/b-v2", "a") as f:
            f.write("# rebuilt in place\n")
        os.kill(self.record()["services"]["svc_a"]["pid"], signal.SIGKILL)
        self.wait_until(
            lambda: (
                self.service_state("svc_b") == "running"
                and self.record()["services"]["svc_b"]["pid"] not in (None, b_pid_v2)
            ),
            proc,
            what="svc_b bounced after the in-place release",
        )
        self.assertEqual(self.record()["services"]["svc_b"]["restarts"], 0)
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0, self.output_of(proc))

    def test_resolver_gate_blocks_consumers_without_spending_budget(self):
        """A resolver that needs credentials: while its gate fails, services
        using its binaries park as `blocked` — the resolver is never even
        run, so there is no crash and no budget spent."""
        app = os.path.join(self.workdir.full_path, "app")
        with open(app, "w") as f:
            f.write(f"#!{sys.executable}\nimport time\ntime.sleep(300)\n")
        os.chmod(app, 0o755)
        self.workdir.create_file("target.txt", content=app + "\n")
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[gates.creds]
check = ["{sys.executable}", "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('flag').exists() else 1)"]
recheck_seconds = 0.2

[resolvers.app]
cmd = ["cat", "target.txt"]
requires = ["creds"]
cache_seconds = 0.0

[services.app]
cmd = ["{{bin.app}}"]
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: self.service_state("app") == "blocked",
            proc,
            what="blocked on the resolver's gate",
        )
        record = self.record()
        self.assertEqual(record["services"]["app"]["blocked_on"], ["creds"])
        self.assertEqual(record["services"]["app"]["restarts"], 0)
        self.assertEqual(record["resolvers"], {}, "resolver must not have run")

        self.workdir.create_file("flag", content="ok")
        self.wait_until(
            lambda: self.service_state("app") == "running",
            proc,
            what="unblocked once credentials returned",
        )
        self.assertEqual(self.record()["services"]["app"]["restarts"], 0)
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0, self.output_of(proc))

    def test_copied_binary_survives_its_source_vanishing(self):
        """copy = true: the service runs a local copy, and a respawn works
        even after the source (the 'network mount') is gone."""
        source = os.path.join(self.workdir.full_path, "mounted-app")
        with open(source, "w") as f:
            f.write(
                f"#!{sys.executable}\nimport pathlib, time\n"
                "with open('runs.txt', 'a') as fh: fh.write('run\\n')\n"
                "time.sleep(300)\n"
            )
        os.chmod(source, 0o755)
        self.workdir.create_file("target.txt", content=source + "\n")
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[resolvers.app]
cmd = ["cat", "target.txt"]
copy = true
cache_seconds = 0.0

[services.app]
cmd = ["{{bin.app}}"]
[services.app.restart]
backoff_initial_seconds = 0.05
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(lambda: os.path.exists("runs.txt"), proc, what="first run")
        record = self.record()
        local = record["resolvers"]["app"]["binaries"]["app"]
        self.assertTrue(
            local.startswith(self.env["JETTY_ORC_BIN_ROOT"]),
            f"service should run the copy, got {local}",
        )
        self.assertEqual(record["resolvers"]["app"]["copied_from"]["app"], source)

        def runs() -> int:
            with open("runs.txt") as fh:
                return len(fh.read().splitlines())

        os.unlink(source)  # the mount disappears
        os.kill(record["services"]["app"]["pid"], signal.SIGKILL)
        self.wait_until(
            lambda: runs() >= 2 and self.service_state("app") == "running",
            proc,
            what="respawn from the cached copy",
        )
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0, self.output_of(proc))

    def test_resolver_failure_is_a_spawn_failure_with_stderr(self):
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[resolvers.app]
cmd = ["sh", "-c", "echo release feed is down >&2; exit 3"]
cache_seconds = 0.0

[services.app]
cmd = ["{{bin.app}}"]
[services.app.restart]
max_restarts = 1
backoff_initial_seconds = 0.05
"""
        )
        proc = self.orc("up", "-c", config)
        rc = proc.wait(timeout=30)
        output = self.output_of(proc)
        self.assertEqual(rc, 1, output)
        self.assertIn("resolver 'app' exited 3", output)
        self.assertIn("release feed is down", output)

    def test_everything_defaults_to_the_config_files_directory(self):
        """Config in a subdir, supervisor launched from outside it: the
        resolver reads its sibling manifest, and the service's default cwd is
        the config's directory — nothing depends on the launch cwd."""
        confdir = self.workdir.mkdir("confdir")
        app = os.path.join(confdir.full_path, "app.py")
        with open(app, "w") as f:
            f.write(
                f"#!{sys.executable}\nimport pathlib, time\n"
                "pathlib.Path('marker.txt').write_text('here')\ntime.sleep(300)\n"
            )
        os.chmod(app, 0o755)
        confdir.create_file("manifest.txt", content=app + "\n")
        config = confdir.create_file(
            "orc.toml",
            content=f"""
[instance]
name = "dev"
containment = "pgroup"

[resolvers.app]
cmd = ["cat", "manifest.txt"]

[services.app]
cmd = ["{{bin.app}}"]
""",
        ).full_path
        proc = self.orc("up", "-c", config)  # our cwd is workdir, NOT confdir
        self.wait_until(
            lambda: self.service_state("app") == "running",
            proc,
            what="running from a subdir config",
        )
        marker = os.path.join(confdir.full_path, "marker.txt")
        self.wait_until(lambda: os.path.exists(marker), proc, what="marker in confdir")
        self.assertFalse(os.path.exists("marker.txt"), "must not use the launch cwd")
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0, self.output_of(proc))

    def test_up_echoes_prefixed_output_and_logs_command_replays_it(self):
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[services.chatty]
cmd = ["{sys.executable}", "-u", "-c", "import time; print('hello-from-chatty'); time.sleep(300)"]

[services.sleeper]
cmd = ["{sys.executable}", "-c", "import time; time.sleep(300)"]
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: self.service_state("chatty") == "running"
            and self.service_state("sleeper") == "running",
            proc,
            what="both running",
        )
        rc, output = self.orc_run("logs", "dev")
        self.assertEqual(rc, 0, output)
        self.assertIn("[chatty ] hello-from-chatty", output)  # padded to 'sleeper'
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0)
        # The foreground view carried the same prefixed line (uncoloured:
        # the pipe is not a tty).
        self.assertIn("[chatty ] hello-from-chatty", self.output_of(proc))

    def test_ls_status_and_kill(self):
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[ports]
api = "auto"

[services.sleeper]
cmd = ["{sys.executable}", "-c", "import time; time.sleep(300)"]
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: self.service_state("sleeper") == "running", proc, what="running"
        )
        port = self.record()["ports"]["api"]

        rc, output = self.orc_run("ls")
        self.assertEqual(rc, 0, output)
        self.assertIn("dev", output)
        self.assertIn(f"api={port}", output)
        self.assertIn("1/1 running", output)

        rc, output = self.orc_run("status", "dev")
        self.assertEqual(rc, 0, output)
        self.assertIn("sleeper", output)

        rc, output = self.orc_run("kill", "dev")
        self.assertEqual(rc, 0, output)
        self.assertEqual(proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0)
        self.assertIsNone(self.record())

        rc, output = self.orc_run("ls")
        self.assertIn("no instances", output)


if __name__ == "__main__":
    absltest.main()
