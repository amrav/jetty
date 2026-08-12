"""The orchestrator's distribution invariants.

The whole deployment story rests on one property: `jetty.orchestrator`
imports NOTHING outside the standard library, so a zipapp of it runs on any
Python 3.11+ with no installs. That property is enforced here — a dependency
creeping in should fail a test, not a deploy.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import zipapp

from absl.testing import absltest

IMPORT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
PACKAGE_DIR = os.path.join(IMPORT_ROOT, "jetty", "orchestrator")


class StdlibOnlyTest(absltest.TestCase):
    def test_orchestrator_imports_only_the_stdlib(self):
        offending = []
        for entry in sorted(os.listdir(PACKAGE_DIR)):
            if not entry.endswith(".py"):
                continue
            with open(os.path.join(PACKAGE_DIR, entry)) as f:
                tree = ast.parse(f.read(), filename=entry)
            for node in ast.walk(tree):
                roots = []
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    roots = [(node.module or "").split(".")[0]]
                for root in roots:
                    # __main__ is always present at runtime but is not
                    # listed among the stdlib module names.
                    if root and root != "__main__" and root not in sys.stdlib_module_names:
                        offending.append(f"{entry}: {root}")
        self.assertEmpty(
            offending,
            "the orchestrator must stay stdlib-only (its single-file "
            f"distribution depends on it): {offending}",
        )


class ZipappTest(absltest.TestCase):
    def test_zipapp_builds_and_runs(self):
        workdir = self.create_tempdir()
        stage = workdir.mkdir("stage")
        pkg = stage.mkdir("jetty_orc")
        for entry in os.listdir(PACKAGE_DIR):
            if entry.endswith(".py"):
                with open(os.path.join(PACKAGE_DIR, entry)) as f:
                    pkg.create_file(entry, content=f.read())
        stage.create_file(
            "__main__.py", content="from jetty_orc.cli import main\n\nmain()\n"
        )
        pyz = os.path.join(workdir.full_path, "jetty-orc.pyz")
        zipapp.create_archive(stage.full_path, pyz)

        config = workdir.create_file(
            "orc.toml",
            content='[instance]\nname = "dev"\n[services.api]\ncmd = ["true"]\n',
        ).full_path
        # An empty PYTHONPATH proves the archive is self-contained: nothing
        # from this checkout or its venv is importable.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        outputs = {}
        for args in (["check", "-c", config], ["doctor"]):
            result = subprocess.run(
                [sys.executable, pyz, *args],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0, f"{args}: {result.stdout}{result.stderr}"
            )
            outputs[args[0]] = result.stdout
        self.assertIn("config OK", outputs["check"])
        self.assertIn("containment", outputs["doctor"])


if __name__ == "__main__":
    absltest.main()
