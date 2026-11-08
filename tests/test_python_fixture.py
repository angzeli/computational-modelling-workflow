"""Synthetic executables must not inherit the developer's Python selection."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from cmw.jobs.runtime import runtime_environment
from tests.molecular.orca.jobs_fixture import make_spec
from tests.python_fixture import write_python_script


ROOT = Path(__file__).parent


class PythonFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw fixture interpreter ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.foreign = self.root / "foreign Python first"
        self.foreign.mkdir()
        self.marker = self.root / "foreign-python-used"
        for name in ("python", "python3"):
            executable = self.foreign / name
            executable.write_text("#!/bin/sh\nprintf foreign > " + shlex.quote(str(self.marker)) + "\nexit 97\n")
            executable.chmod(0o700)
        self.environment = {**os.environ, "PATH": str(self.foreign) + os.pathsep + os.environ.get("PATH", "")}

    def run_script(self, executable, *arguments, environment=None):
        result = subprocess.run([str(executable), *map(str, arguments)],
            env=environment or self.environment, input="", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.marker.exists(), "fixture selected Python through PATH")
        return result.stdout

    def test_quoted_pin_preserves_adjacent_resources_and_future_imports(self):
        interpreter = self.root / "explicit interpreter with spaces"
        interpreter.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        interpreter.chmod(0o700)
        executable = self.root / "fixture with spaces"
        (self.root / "adjacent.txt").write_text("adjacent evidence")
        write_python_script(executable, '''"""Fixture documentation."""
from __future__ import annotations
import json, sys
from pathlib import Path
print(json.dumps([sys.executable, Path(__file__).name, Path(__file__).with_name("adjacent.txt").read_text()]))
''', executable=str(interpreter))
        self.assertEqual(json.loads(self.run_script(executable)),
                         [sys.executable, executable.name, "adjacent evidence"])

    def test_orca_jobs_fixture_ignores_foreign_path_with_runtime_pythonpath(self):
        spec = make_spec(self.root / "ORCA")
        executable = Path(spec["runtime"]["executable"])
        # Use the same runtime environment as the PR #33 producing-run fixture.
        environment = runtime_environment({"PATH": self.environment["PATH"]})
        executable.write_text(executable.read_text() + "\nprint('FIXTURE_PYTHON=' + sys.executable)\n")
        output = self.run_script(executable, spec["scientific"]["input"], environment=environment)
        self.assertIn("FIXTURE_PYTHON=" + sys.executable, output)
        self.assertIn("ORCA TERMINATED NORMALLY", output)

    def test_vasp_fixture_selects_test_python_without_a_profile_pin(self):
        from tests.jobs.successor_fixtures import _runner
        from tests.python_fixture import vasp_environment
        for unsafe in ('/tmp/$other/python', '/tmp/`command`/python', '/tmp/"quote/python'):
            with self.assertRaisesRegex(ValueError, 'static literal path contract'):
                vasp_environment(executable=unsafe)
        runner = _runner(self.root)
        runner.with_name('run_vasp.py').write_text(
            'import json, sys; print(json.dumps([sys.executable, sys.prefix]))\n')
        environment = {**self.environment,
                       'CMW_MANAGED_PYTHON': str(self.foreign / 'python3')}
        self.assertEqual(json.loads(self.run_script(runner, environment=environment)),
                         [sys.executable, sys.prefix])

    def test_static_engine_sources_generate_pinned_executables(self):
        for filename in ("fake_orca.py", "fake_multiwfn.py"):
            with self.subTest(filename=filename):
                source = (ROOT / "fixtures" / filename).read_text()
                source = source.replace("import sys\n", "import sys\nprint('FIXTURE_PYTHON=' + sys.executable)\n", 1)
                executable = write_python_script(self.root / filename, source)
                output = self.run_script(executable, "--version")
                self.assertIn("FIXTURE_PYTHON=" + sys.executable, output)
