"""Exact identity diagnostics inspect only the recorded PID in an existing queue."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import build_parser
from cmw.jobs import ownership
from cmw.jobs.store import Store


class OwnershipCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'state')
        self.job = self.store.add(argv=['/bin/echo'], cwd=temp.name, name='fixture')

    def diagnose(self, *options):
        args = build_parser().parse_args(['jobs', '--state', str(self.store.root), 'ownership',
                                          self.job['display_id'], *options])
        with redirect_stdout(StringIO()) as output, patch.object(Store, 'transaction', side_effect=AssertionError):
            code = args.handler(args)
        return code, output.getvalue()

    def test_live_process_exact_identity_and_mismatched_token(self):
        owner = ownership.identity(os.getpid())
        for offset, expected in ((0, 'VERIFIED'), (1, 'GONE')):
            with self.store.transaction() as con:
                job = self.store.get(con, self.job['id'])
                job['group'] = {**owner, 'start_token': owner['start_token'] + offset}
                self.store.save(con, job)
            before = self.store.path.read_bytes()
            code, text = self.diagnose('--json')
            result = json.loads(text)
            self.assertEqual((code, result['status']), (2 if offset else 0, expected))
            self.assertEqual(result['record_version'], 2)
            self.assertEqual(result['boot_session'], owner['boot_session'])
            token = result['checks']['start_token']
            self.assertEqual((token['recorded'], token['observed']),
                             (owner['start_token'] + offset, owner['start_token']))
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_missing_worker_reports_retryable_and_never_creates_attempt_files(self):
        names = sorted(self.store.root.iterdir())
        code, text = self.diagnose('--role', 'worker')
        self.assertEqual(code, 75)
        for term in ('UNVERIFIABLE', 'worker', 'record version', 'boot session', 'recorded=', 'observed='):
            self.assertIn(term, text)
        self.assertEqual(sorted(self.store.root.iterdir()), names)

    def test_locked_queue_returns_retryable_without_creating_files(self):
        conn = sqlite3.connect(self.store.path)
        self.addCleanup(conn.close)
        conn.execute('BEGIN EXCLUSIVE')
        names = sorted(self.store.root.iterdir())
        code, text = self.diagnose('--json')
        self.assertEqual(code, 75)
        self.assertEqual(json.loads(text)['deciding_check'], 'queue')
        self.assertEqual(sorted(self.store.root.iterdir()), names)
