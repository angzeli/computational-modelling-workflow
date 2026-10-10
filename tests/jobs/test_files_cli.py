"""Read-only location inspection and explicit, stubbed directory opening."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs.store import Store


class FilesCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-files-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'state')

    def invoke(self, *args, state=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['jobs', '--state', str(state or self.store.root), 'files', *args])
        return code, out.getvalue(), err.getvalue()

    def job(self, *, known=False):
        source = self.root / 'input 繁體'
        output = self.root / 'results with spaces; $(never)'
        source.mkdir(exist_ok=True)
        output.mkdir(exist_ok=True)
        argv = ['/unexecuted/run-vasp.sh', '--input', str(source), '--output', str(output),
                '--binary', 'std', '--ranks', '8', '--ncore', '4', '--kpar', '1',
                '--mpi-mode', 'synthetic', '--restart', 'none', '--timeout', '10',
                '--stop-before', '0', '--managed-foreground'] if known else ['/unexecuted/opaque-wrapper', '--output', str(output)]
        job = self.store.add(argv=argv, cwd=source, name='literal [name]', hold=True)
        return job, output

    def test_unknown_wrapper_lists_truthfully_without_writing_or_opening(self):
        job, _ = self.job()
        before = self.store.path.read_bytes()
        with patch('cmw.jobs.folder_opener.request_open') as opening:
            code, text, err = self.invoke(job['display_id'], '--json')
            self.assertEqual((code, err), (0, ''))
            payload = json.loads(text)
            self.assertEqual(payload['status'], 'LOCATION_UNKNOWN')
            self.assertTrue(any(x['role'] == 'working-directory' and x['path'] == job['cwd'] for x in payload['locations']))
            self.assertTrue(any(x['role'] == 'output' and x['path'] is None for x in payload['locations']))
            opening.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(self.store.snapshot()['controller']['online'])
        code, text, _ = self.invoke(job['display_id'])
        self.assertEqual(code, 0)
        self.assertIn('LOCATION_UNKNOWN', text)
        self.assertIn(job['cwd'], text)

    def test_known_runner_output_is_separate_and_open_is_explicit(self):
        job, output = self.job(known=True)
        before = self.store.path.read_bytes()
        with patch('cmw.jobs.folder_opener.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '', '')
            code, text, err = self.invoke(job['display_id'], '--json')
            self.assertEqual((code, err), (0, ''))
            listed = json.loads(text)
            targets = [x for x in listed['locations'] if x['role'] == 'output']
            self.assertEqual(targets[0]['path'], str(output))
            self.assertNotEqual(targets[0]['path'], job['cwd'])
            run.assert_not_called()
            with patch('cmw.jobs.folder_opener.sys.platform', 'darwin'):
                code, text, err = self.invoke(job['display_id'], '--open', '--role', 'output', '--json')
            self.assertEqual((code, err), (0, ''))
            self.assertEqual(json.loads(text)['open']['status'], 'OPEN_REQUESTED')
            self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '--', str(output)])
            self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_missing_output_reports_failure_with_alternatives_and_no_fallback(self):
        job, output = self.job(known=True)
        output.rmdir()
        with patch('cmw.jobs.folder_opener.request_open') as opening:
            code, text, _ = self.invoke(job['display_id'], '--open', '--json')
            self.assertEqual(code, 2)
            payload = json.loads(text)
            self.assertIn(payload['open']['status'], {'NOT_FOUND', 'NOT_CREATED_YET'})
            self.assertTrue(any(x['path'] == job['cwd'] for x in payload['locations']))
            opening.assert_not_called()

    def test_missing_state_unknown_key_and_external_never_create_or_guess(self):
        missing = self.root / 'missing-state'
        code, text, _ = self.invoke('J1.1', '--json', state=missing)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(text)['code'], 'JOBS_ERROR')
        self.assertFalse(missing.exists())
        job, _ = self.job()
        with patch('cmw.jobs.folder_opener.request_open') as opening:
            code, text, _ = self.invoke(job['display_id'], '--open', '--key', 'foreign-state-key', '--json')
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(text)['open']['status'], 'LOCATION_UNKNOWN')
            code, text, _ = self.invoke('E123', '--json')
            self.assertEqual(code, 2)
            opening.assert_not_called()

    def test_files_core_command_imports_without_textual_or_rich(self):
        job, _ = self.job()
        code = '''import importlib.abc, sys
class BlockUI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'textual', 'rich'}:
            raise AssertionError('Core CLI imported optional UI: ' + fullname)
sys.meta_path.insert(0, BlockUI())
from cmw.cli import main
raise SystemExit(main(sys.argv[1:]))
'''
        completed = subprocess.run([sys.executable, '-c', code, 'jobs', '--state', str(self.store.root),
                                    'files', job['display_id'], '--json'],
                                   capture_output=True, text=True, timeout=15,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['attempt_id'], job['attempt_id'])


if __name__ == '__main__':
    unittest.main()
