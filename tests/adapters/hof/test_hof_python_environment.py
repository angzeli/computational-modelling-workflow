"""Generated queue status commands must not inherit a foreign Python runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cmw.adapters.hof.batch_queue import _master_script as batch_script
from cmw.adapters.hof.command_queue import _master_script as command_script
from cmw.adapters.hof.full_chain import _master_script as full_chain_script


class HofPythonEnvironmentTests(unittest.TestCase):
    def test_status_uses_only_bound_python_environment(self):
        with tempfile.TemporaryDirectory(prefix='cmw HOF import pin ') as temporary:
            root = Path(temporary).resolve()
            cmw_root = root / 'synthetic cmw'
            package = cmw_root / 'src/cmw/adapters/hof'
            package.mkdir(parents=True)
            for directory in (package, package.parent, package.parent.parent):
                (directory / '__init__.py').write_text('')
            probe = (
                'import json, os, sys\n'
                'result = {"executable": sys.executable, "prefix": sys.prefix,\n'
                '          "home": os.environ.get("PYTHONHOME"),\n'
                '          "pythonpath": os.environ.get("PYTHONPATH"), "path": sys.path}\n'
                'if os.environ.get("HOF_EXPECT_COPIED_YAML") == "1":\n'
                '    import yaml\n'
                '    result["yaml"] = yaml.__file__\n'
                'print(json.dumps(result))\n'
            )
            for filename in ('command_queue_cli.py', 'batch_queue_cli.py'):
                (package / filename).write_text(probe)
            python_runtime = root / 'qualified Python source'
            (python_runtime / 'yaml').mkdir(parents=True)
            copied_yaml = python_runtime / 'yaml/__init__.py'
            copied_yaml.write_text('"""Synthetic copied YAML source marker."""\n')
            foreign = root / 'foreign import path'
            foreign.mkdir()
            marker = root / 'foreign-import-used'
            (foreign / 'sitecustomize.py').write_text(
                f'from pathlib import Path\nPath({str(marker)!r}).write_text("foreign")\n')
            common = {'cmw_root': cmw_root, 'python_bin': Path(sys.executable), 'steps': []}
            scripts = {
                'command': command_script(**common, queue_id='synthetic'),
                'batch': batch_script(**common, batch_id='synthetic', project_root=root,
                                      minimum_free_gb=0),
                'full_chain': full_chain_script(**common, queue_id='synthetic', project_root=root,
                                                minimum_free_gb=0, python_runtime=python_runtime),
            }
            for name, text in scripts.items():
                script = root / f'{name}.sh'
                script.write_text(text)
                expected_paths = [str(cmw_root / 'src')]
                if name == 'full_chain':
                    expected_paths.append(str(python_runtime))
                for contamination in ('home', 'imports'):
                    with self.subTest(master=name, contamination=contamination):
                        marker.unlink(missing_ok=True)
                        environment = {
                            **os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                            'PYTHONHOME': str(root / 'nonexistent Python home') if contamination == 'home' else '',
                            'PYTHONPATH': str(foreign),
                            'HOF_EXPECT_COPIED_YAML': '1' if name == 'full_chain' else '',
                        }
                        result = subprocess.run(['/bin/bash', str(script), '--status'],
                            env=environment, cwd=root, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        observed = json.loads(result.stdout.splitlines()[0])
                        self.assertEqual(observed['executable'], sys.executable)
                        self.assertEqual(observed['prefix'], sys.prefix)
                        self.assertIsNone(observed['home'])
                        self.assertEqual(observed['pythonpath'], os.pathsep.join(expected_paths))
                        self.assertNotIn(str(foreign), observed['path'])
                        self.assertFalse(marker.exists(), 'foreign sitecustomize ran')
                        if name == 'full_chain':
                            self.assertEqual(observed['yaml'], str(copied_yaml))
