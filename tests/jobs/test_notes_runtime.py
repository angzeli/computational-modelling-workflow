"""Annotation survives the real supervisor's final write without changing work."""
import json
import sys
import unittest

from cmw.jobs import runtime
from cmw.jobs.ownership import owner_alive
from tests.jobs import test_jobs as lifecycle


class NotesRuntimeTests(unittest.TestCase):
    def setUp(self):
        lifecycle.QueueTests.setUp(self)
        self.release = self.root / 'release'

    def tearDown(self):
        self.release.touch()
        lifecycle.QueueTests.tearDown(self)

    jobs = lifecycle.QueueTests.jobs
    run_queue = lifecycle.QueueTests.run_queue
    completed = lifecycle.QueueTests.completed

    def test_running_annotation_survives_restart_and_terminal_lifecycle(self):
        ready, count = self.root / 'ready', self.root / 'count'
        code = (f'from pathlib import Path; import time; p=Path({str(count)!r}); '
                f'p.write_text(p.read_text()+"x" if p.exists() else "x"); '
                f'Path({str(ready)!r}).touch(); gate=Path({str(self.release)!r})\n'
                'while not gate.exists(): time.sleep(.02)\n'
                'print("fixture completed")')
        job = self.store.add(argv=[sys.executable, '-c', code], cwd=self.root,
                             name='annotation fixture', note='before start')
        waiting = self.store.add(argv=[sys.executable, '-c', 'print("held")'],
                                 cwd=self.root, name='held follower', hold=True)
        self.run_queue()
        lifecycle.wait_for(lambda: ready.exists() and self.jobs()[0]['status'] == 'Run')
        before = self.jobs()[0]
        updated = self.store.annotate(job['id'], 'during Run\nconverged is only a human word',
                                     expected_revision=0, expected_attempt_id=job['attempt_id'])
        for key in ('id', 'display_id', 'attempt_id', 'worker', 'group', 'claim', 'started_at', 'argv'):
            self.assertEqual(updated[key], before[key])
        self.assertEqual(updated['status'], 'Run')
        self.assertTrue(owner_alive(updated['worker']))
        self.assertTrue(owner_alive(updated['group']))
        self.assertTrue(self.store.snapshot()['controller']['dispatch'])
        self.assertEqual((self.jobs()[1]['id'], self.jobs()[1]['status'], self.jobs()[1]['order']),
                         (waiting['id'], 'Hold', 1))

        runtime.stop(self.store)
        lifecycle.wait_for(lambda: not self.store.snapshot()['controller']['online'])
        self.run_queue()
        restarted = self.jobs()[0]
        self.assertEqual(restarted['note'], updated['note'])
        self.assertEqual((restarted['worker'], restarted['group'], restarted['attempt_id']),
                         (before['worker'], before['group'], before['attempt_id']))
        self.release.touch()
        lifecycle.wait_for(lambda: self.jobs()[0]['status'] == 'Done')
        final = self.jobs()[0]
        self.assertEqual((final['note'], final['note_revision']), (updated['note'], 1))
        self.assertEqual(final['attempt_id'], job['attempt_id'])
        self.assertEqual(final['exit_code'], 0)
        self.assertEqual(count.read_text(), 'x')
        self.assertEqual((self.jobs()[1]['status'], self.jobs()[1]['order']), ('Hold', 1))
        self.assertTrue(self.store.snapshot()['controller']['dispatch'])
        receipt = self.store.root / 'attempts' / job['attempt_id'] / 'payload-exit.json'
        receipt_bytes = receipt.read_bytes()
        self.assertEqual(json.loads(receipt_bytes)['exit_code'], 0)
        self.store.annotate(job['id'], 'terminal edit', expected_revision=1,
                            expected_attempt_id=job['attempt_id'])
        self.assertEqual(receipt.read_bytes(), receipt_bytes)
        self.assertNotIn('during Run', (self.store.root / 'controller.log').read_text())
