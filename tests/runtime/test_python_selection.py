"""Shell runtime helpers require the caller's interpreter, never its PATH."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[2]


class RuntimePythonSelectionTests(unittest.TestCase):
    def test_runner_environment_cannot_override_or_unset_the_selected_python(self):
        from tests.periodic.vasp.test_runner_compatibility import RunnerCompatibilityTests
        fixture = RunnerCompatibilityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        started = fixture.root / 'selected-python-started'
        foreign_started = fixture.root / 'foreign-python-started'
        fixture.entrypoint.write_text(
            'from pathlib import Path\n'
            f'Path({str(started)!r}).write_text("started")\n')
        foreign = fixture.root / 'foreign-python'
        foreign.write_text('#!/bin/sh\nprintf started > ' + shlex.quote(str(foreign_started)) + '\n')
        foreign.chmod(0o700)
        environment_path = fixture.scripts / 'environment.sh'
        original = environment_path.read_text()
        environment = {**os.environ, 'CMW_MANAGED_PYTHON': sys.executable,
                       'PYTHON_BIN': sys.executable, 'PYTHONHOME': '', 'PYTHONPATH': ''}
        baseline = subprocess.run(['/bin/bash', str(fixture.runner)], env=environment,
                                  text=True, capture_output=True, timeout=10)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assertEqual(started.read_text(), 'started')
        started.unlink()
        for variable in ('CMW_MANAGED_PYTHON', 'PYTHON_BIN', 'PYTHONPATH', 'PYTHONHOME'):
            for mutation in (f'{variable}={shlex.quote(str(foreign))}', f'unset {variable}'):
                with self.subTest(mutation=mutation):
                    environment_path.write_text(original + mutation + '\n')
                    result = subprocess.run(['/bin/bash', str(fixture.runner)], env=environment,
                                            text=True, capture_output=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('readonly variable', result.stderr)
                    self.assertFalse(started.exists())
                    self.assertFalse(foreign_started.exists())

    def test_explicit_pin_survives_foreign_path_and_import_environment(self):
        with tempfile.TemporaryDirectory(prefix='cmw python selection ') as temporary:
            root = Path(temporary)
            foreign = root / 'python3'
            foreign.write_text('#!/bin/sh\nexit 97\n')
            foreign.chmod(0o700)
            program = 'import json, sys; print(json.dumps([sys.executable, sys.prefix]))'
            command = ('. ' + shlex.quote(str(ROOT / 'scripts/runtime_python.sh')) +
                       '; "$PYTHON_BIN" -c ' + shlex.quote(program))
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ.get('PATH', ''),
                   'PYTHON_BIN': sys.executable, 'PYTHONHOME': '/unusable/foreign-home',
                   'PYTHONPATH': '/unusable/foreign-stdlib'}
            result = subprocess.run(['/bin/bash', '-c', command], env=env,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [sys.executable, sys.prefix])

    def test_missing_relative_or_unexecutable_pin_refuses_without_path_fallback(self):
        command = '. ' + shlex.quote(str(ROOT / 'scripts/runtime_python.sh')) + '; exit 0'
        for pin in (None, 'python3', '/not/an/executable'):
            env = dict(os.environ)
            env.pop('PYTHON_BIN', None)
            if pin is not None:
                env['PYTHON_BIN'] = pin
            with self.subTest(pin=pin):
                result = subprocess.run(['/bin/bash', '-c', command], env=env,
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 64)
                self.assertIn('PYTHON_BIN', result.stderr)
