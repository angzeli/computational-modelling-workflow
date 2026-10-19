"""Core-only handoff routes capture explicit attempts without state mutation."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.jobs import test_progress_cli as base


class SnapshotCliTests(unittest.TestCase):
    setUp = base.ProgressCliTests.setUp
    invoke = base.ProgressCliTests.invoke

    def test_readable_and_json_snapshot_keep_full_identity_and_same_facts(self):
        before = self.store.path.read_bytes()
        args = (self.job['display_id'], '--source', f'flux={self.source}')
        code, text, err = self.invoke(*args, '--snapshot', 'summary')
        self.assertEqual((code, err), (0, ''))
        for fact in (self.job['name'], self.job['attempt_id'], '3.25', 'Charge flux', 'not assessed', 'UTC'):
            self.assertIn(fact, text)
        code, serialized, err = self.invoke(*args, '--snapshot', 'json')
        payload = json.loads(serialized)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(payload['job']['attempt_id'], self.job['attempt_id'])
        self.assertEqual(payload['progress']['metrics'][0]['latest']['value'], 3.25)
        self.assertEqual(payload['scientific_acceptance'], 'not assessed')
        self.assertNotIn('argv', payload['job'])
        self.assertNotIn('env', payload['job'])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_diagnostics_redaction_and_option_errors(self):
        code, text, _ = self.invoke(self.job['display_id'], '--source', f'flux={self.source}',
                                    '--snapshot', 'diagnostics', '--redact-paths')
        self.assertEqual(code, 0)
        self.assertNotIn(str(self.source), text)
        self.assertNotIn(str(self.store.root), text)
        for options in (('--snapshot', 'summary', '--history'), ('--redact-paths',)):
            code, text, _ = self.invoke(self.job['display_id'], *options, '--json')
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(text)['code'], 'JOBS_ERROR')

    def test_core_handoff_never_imports_optional_ui(self):
        script = '''import importlib.abc,sys
class NoUI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'textual','rich'}:
            raise AssertionError('Optional UI imported: '+fullname)
sys.meta_path.insert(0,NoUI())
from cmw.cli import main
raise SystemExit(main(sys.argv[1:]))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, 'jobs', '--state', str(self.store.root),
                                 'progress', self.job['display_id'], '--snapshot', 'json'],
                                capture_output=True, text=True, timeout=15,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['job']['attempt_id'], self.job['attempt_id'])

    def test_attempt_changed_during_observation_refuses_handoff(self):
        from cmw.progress.service import ObservationService
        original = ObservationService.poll
        def changed(observer, context):
            snapshot = original(observer, context)
            with self.store.transaction() as con:
                job = self.store.get(con, self.job['id'])
                job['attempt_id'] = 'different-attempt'
                self.store.save(con, job)
            return snapshot
        with patch.object(ObservationService, 'poll', changed):
            code, text, _ = self.invoke(self.job['display_id'], '--snapshot', 'json', '--json')
        self.assertEqual(code, 2)
        self.assertIn('Attempt identity changed', text)
