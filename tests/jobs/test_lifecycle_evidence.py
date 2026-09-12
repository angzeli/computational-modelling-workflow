from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.jobs.store import Store
from tests.jobs.lifecycle_evidence import owned_members, retain_lifecycle_evidence, trace_runtime


class EvidenceTests(unittest.TestCase):
    def test_capture_failure_preserves_original_assertion(self):
        @retain_lifecycle_evidence
        def failing(test):
            raise AssertionError('original failure')
        self.store = object()
        with patch('tests.jobs.lifecycle_evidence.capture', side_effect=OSError('disk unavailable')), \
             patch('sys.stderr', new=io.StringIO()):
            with self.assertRaisesRegex(AssertionError, 'original failure'):
                failing(self)

    def test_trace_write_failure_does_not_prevent_signal(self):
        with patch.dict(os.environ, {'CMW_TEST_LIFECYCLE_TRACE': '1'}), \
             patch('os.killpg') as signal_group, patch.object(Path, 'open', side_effect=OSError('unwritable')):
            with trace_runtime(Path('unwritable-test-root')):
                os.killpg(987654, 15)
            signal_group.assert_called_once_with(987654, 15)

    def test_reused_group_identity_is_not_inspected_as_owned(self):
        state = {'jobs': [{'group': {'pid': 10, 'birth': 1}}]}
        with patch('tests.jobs.lifecycle_evidence.identity', return_value={'pid': 10, 'birth': 2}), \
             patch('psutil.pids') as pids:
            self.assertEqual(owned_members(state, Path('test-state')), [])
            pids.assert_not_called()

    def test_failure_preserves_state_receipts_and_bounded_tails_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.store = Store(root/'state')
            job = self.store.add(argv=['/bin/echo', 'synthetic'], cwd=root, name='evidence', hold=True)
            attempt = self.store.root/'attempts'/job['attempt_id']
            attempt.mkdir(parents=True)
            (attempt/'payload-exit.json').write_text('{"exit_code": 143}')
            (attempt/'worker.log').write_text('x'*20000+'last line')

            @retain_lifecycle_evidence
            def failing(test):
                raise AssertionError('injected lifecycle failure')

            with patch.dict(os.environ, {'CMW_TEST_EVIDENCE_DIR': str(root/'evidence')}), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(AssertionError, 'injected lifecycle failure'):
                    failing(self)
            retained = next((root/'evidence').iterdir())
            data = json.loads((retained/'capture.json').read_text())
            self.assertIn('injected lifecycle failure', data['outcome'])
            self.assertLessEqual(data['started_at'], data['captured_at'])
            self.assertLessEqual(data['captured_at'], time.time())
            self.assertEqual(data['snapshot']['jobs'][0]['attempt_id'], job['attempt_id'])
            self.assertEqual(data['receipts'][job['attempt_id']], '{"exit_code": 143}')
            self.assertFalse(data['worker_locks'][job['attempt_id']])
            self.assertEqual(len(next(iter(data['log_tails'].values()))), 16384)
            self.assertTrue(self.store.path.exists())
            self.assertTrue((retained/'state'/self.store.path.name).exists())
