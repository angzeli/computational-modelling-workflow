"""CLI lifecycle projections preserve existing stored state and authority."""
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs import activity, lifecycle
from cmw.jobs.store import Store


def observed(job, **changes):
    result = {'job_id': job['display_id'], 'attempt_id': job['attempt_id'],
              'persisted_status': job['status'], 'observed_execution': 'WORKER_MISSING',
              'observation_quality': 'partial', 'observed_at': 100.0,
              'age_seconds': 0.0, 'stale': False, 'reconciliation_required': True,
              'summary': 'Stored Run status is not currently verified.',
              'evidence': {key: {'verdict': value, 'reason': 'Fixture evidence.'}
                           for key, value in [('source', 'matched'), ('worker', 'gone'), ('worker_lock', 'unheld'),
                                              ('domain', 'partial'), ('receipt', 'missing'),
                                              ('controller', 'offline')]},
              'limitations': ['Process and lock observations are not atomic.']}
    result.update(changes)
    return SimpleNamespace(to_dict=lambda **kwargs: deepcopy(result))


class LifecycleCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-lifecycle-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['/unexecuted/opaque'], cwd=self.root, name='fixture', engine='Unsupported')
        with self.store.transaction() as con:
            row = self.store.get(con, self.job['id'])
            row.update(status='Run', order=None, started_at=1.0, claim='fixture-claim')
            self.store.save(con, row)
        self.state = self.store.snapshot()
        self.job = self.state['jobs'][0]

    def invoke(self, *args, state=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['jobs', '--state', str(state or self.store.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_observe_is_read_only_and_never_uses_external_discovery(self):
        before = self.store.path.read_bytes()
        with patch.object(lifecycle, 'observe_execution', return_value=observed(self.job)) as collect, \
             patch.object(activity, 'project', side_effect=AssertionError('no external scan')), \
             patch.object(Store, 'snapshot', side_effect=AssertionError('no history projection')), \
             patch('psutil.pids', side_effect=AssertionError('no process enumeration')), \
             patch('psutil.process_iter', side_effect=AssertionError('no process discovery')), \
             patch.object(Store, 'transaction', side_effect=AssertionError('no write')), \
             patch.object(os, 'kill', side_effect=AssertionError('no signal')), \
             patch.object(os, 'killpg', side_effect=AssertionError('no signal')):
            code, text, err = self.invoke('observe', self.job['display_id'], '--json')
        self.assertEqual((code, err), (0, ''))
        payload = json.loads(text)
        self.assertEqual(payload['persisted_status'], 'Run')
        self.assertEqual(payload['observed_execution'], 'WORKER_MISSING')
        self.assertTrue(payload['reconciliation_required'])
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    def test_active_show_is_additive_and_unknown_legacy_projection_is_preserved(self):
        with patch.object(activity, 'project', return_value=self.state):
            for status in ('Starting', 'Run', 'Cancelling', 'Unknown'):
                with self.subTest(status=status):
                    self.state['jobs'][0]['status'] = status
                    with patch.object(lifecycle, 'observe_execution', return_value=observed(self.state['jobs'][0])):
                        code, text, err = self.invoke('show', self.job['display_id'], '--json')
                    result = json.loads(text)
                    self.assertEqual((code, err), (0, ''))
                    self.assertEqual(result['status'], status)
                    self.assertEqual(result['execution_observation']['persisted_status'], status)
                    if status == 'Unknown':
                        self.assertEqual(result['ownership_observation']['worker'], result['execution_observation']['evidence']['worker'])
                        self.assertTrue(result['ownership_observation']['advisory'])
                    else:
                        self.assertNotIn('ownership_observation', result)

    def test_status_and_terminal_show_do_not_start_lifecycle_inspection(self):
        with patch.object(activity, 'project', return_value=self.state), \
             patch.object(lifecycle, 'observe_execution', side_effect=AssertionError('unexpected inspection')):
            self.assertEqual(self.invoke('status', '--json')[0], 0)
            for status in ('Queue', 'Hold', 'Done', 'Fail', 'Cancelled'):
                self.state['jobs'][0]['status'] = status
                code, text, _ = self.invoke('show', self.job['display_id'], '--json')
                self.assertEqual(code, 0)
                self.assertNotIn('execution_observation', json.loads(text))
            self.assertEqual(self.invoke('logs', self.job['display_id'], '--json')[0], 0)

    def test_explicit_human_observation_separates_stored_and_operational_evidence(self):
        with patch.object(lifecycle, 'observe_execution', return_value=observed(self.job)):
            code, text, err = self.invoke('observe', self.job['display_id'])
        self.assertEqual((code, err), (0, ''))
        for expected in ('Persisted status: Run', 'Execution observation: WORKER_MISSING',
                         'Worker: gone', 'Worker lock: unheld', 'Process domain: partial',
                         'Controller: offline', 'Reconciliation: Review required.'):
            self.assertIn(expected, text)

    def test_missing_and_external_jobs_never_create_state_or_observe(self):
        missing = self.root/'absent'
        with patch.object(lifecycle, 'observe_execution', side_effect=AssertionError('no target')):
            for target in ('J9.1', 'E123'):
                code, text, _ = self.invoke('observe', target, '--json', state=missing)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(text)['code'], 'JOBS_ERROR')
        self.assertFalse(missing.exists())

    def test_schema_three_is_refused_without_migration(self):
        with closing(sqlite3.connect(self.store.path)) as con, con:
            control = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
            control['schema'] = 3
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control),))
        before = self.store.path.read_bytes()
        with patch.object(lifecycle, 'observe_execution', side_effect=AssertionError('unsupported source')):
            code, text, _ = self.invoke('observe', self.job['display_id'], '--json')
        self.assertEqual(code, 2)
        self.assertIn('Unsupported Jobs state schema', json.loads(text)['reason'])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_core_observe_imports_without_textual_or_rich(self):
        code = '''import importlib.abc, sys
class BlockUI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'textual', 'rich'}:
            raise AssertionError('Core CLI imported optional UI: ' + fullname)
sys.meta_path.insert(0, BlockUI())
from cmw.cli import main
raise SystemExit(main(sys.argv[1:]))
'''
        result = subprocess.run([sys.executable, '-c', code, 'jobs', '--state', str(self.store.root),
                                 'observe', self.job['display_id'], '--json'], capture_output=True,
                                text=True, timeout=15, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['persisted_status'], 'Run')


if __name__ == '__main__':
    unittest.main()
