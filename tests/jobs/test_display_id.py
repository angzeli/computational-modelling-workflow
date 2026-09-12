"""Display labels preserve scientific attempts and work across queue controls."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.cli import register
from cmw.jobs.store import JobsError, Store


class DisplayIdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.original = self.store.add(argv=['/bin/echo', 'synthetic'], cwd=self.root,
                                       name='Multiwfn fixture', engine='Multiwfn', hold=True)

    def cli(self, *args):
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers())
        parsed = parser.parse_args(['jobs', '--state', str(self.store.root), *args, '--json'])
        output = io.StringIO()
        with redirect_stdout(output), patch('cmw.jobs.activity.project', lambda store: store.snapshot()):
            code = parsed.handler(parsed)
        return code, json.loads(output.getvalue())

    def test_rename_preserves_attempt_and_controls(self):
        code, renamed = self.cli('rename-id', 'J1.1', 'M1.1')
        self.assertEqual(code, 0)
        self.assertEqual(renamed, {**self.original, 'display_id': 'M1.1'})
        self.assertEqual(self.cli('show', 'M1.1')[1]['attempt_id'], self.original['attempt_id'])
        self.assertEqual(self.cli('logs', 'M1.1')[1]['job_id'], 'M1.1')
        self.assertEqual(self.cli('release', 'M1.1')[1]['status'], 'Queue')
        self.assertEqual(self.cli('hold', 'M1.1')[1]['status'], 'Hold')
        for alias in ('1', 'J1', 'M1.1'):
            with self.store.transaction() as con:
                self.assertEqual(self.store.get(con, alias)['id'], self.original['id'])
        with self.store.transaction() as con:
            with self.assertRaises(JobsError):
                self.store.get(con, 'M1.2')
        self.assertEqual(self.cli('cancel', 'M1.1')[1]['status'], 'Cancelled')

    def test_invalid_labels_do_not_mutate(self):
        observed_at = 1700000000.0
        before = self.store.snapshot(now=observed_at)
        for label in ('E1.1', 'M2.1', 'M1.2', 'M1', 'm1.1', 'MM1.1', 'M1.1\n'):
            with self.subTest(label=label), self.assertRaises(JobsError):
                self.store.rename_id('J1.1', label)
            self.assertEqual(self.store.snapshot(now=observed_at), before)

    def test_dispatch_and_started_attempt_block_rename(self):
        self.store.dispatch(True)
        with self.assertRaisesRegex(JobsError, 'Pause dispatch'):
            self.store.rename_id('J1.1', 'M1.1')
        self.store.dispatch(False)
        for status in ('Starting', 'Run', 'Done', 'Fail', 'Cancelled', 'Unknown'):
            with self.store.transaction() as con:
                job = self.store.get(con, 1)
                job['status'] = status
                self.store.save(con, job)
            with self.subTest(status=status), self.assertRaises(JobsError):
                self.store.rename_id('J1.1', 'M1.1')
