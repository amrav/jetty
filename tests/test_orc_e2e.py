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
        path = os.path.join(self.env["JETTY_ORC_ROOT"], "registry", f"{name}.json")
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
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

    def test_duplicate_instance_name_refused(self):
        config = self.write_config(
            f"""
[instance]
name = "dev"
containment = "pgroup"

[services.sleeper]
cmd = ["{sys.executable}", "-c", "import time; time.sleep(300)"]
"""
        )
        proc = self.orc("up", "-c", config)
        self.wait_until(
            lambda: self.service_state("sleeper") == "running", proc, what="running"
        )
        rc, output = self.orc_run("up", "-c", config)
        self.assertEqual(rc, 1)
        self.assertIn("already running", output)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=SHUTDOWN_TIMEOUT_S)

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
