"""Installed command grammar remains core-only and mutations are explicit."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.cli import register
from cmw.jobs.store import Store
from tests.nodes import test_catalogue as node_catalogue_tests


def run_jobs(arguments, state_root=None):
    """Run the core `jobs` grammar in process; return (exit code, stdout+stderr)."""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers())
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        try:
            args = parser.parse_args(['jobs', *(['--state', str(state_root)] if state_root else []), *arguments])
            code = args.handler(args)
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue()


class CatalogueCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-catalogue-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root/'state')

    def call(self, arguments, *, state=True):
        return run_jobs(arguments, self.store.root if state else None)

    def add(self):
        return self.store.add(argv=['/bin/true'], cwd=self.root, name='同名 [literal].*', note='long ' + '字'*4000, hold=True)

    def test_core_query_missing_state_read_does_not_backfill(self):
        code, output = self.call(['query', '--json'])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['total_count'], 0)
        self.assertFalse(self.store.root.exists())
        code, output = self.call(['query', '--limit', '101', '--json'])
        self.assertEqual(code, 2, output)
        self.assertFalse(self.store.root.exists())

    def test_unicode_query_edit_cas_request_replay_and_original_name(self):
        job = self.add()
        args = ['catalogue', 'edit', job['display_id'], '--project', 'ZIS', '--series', '比較',
                '--tag', '[literal]', '--alias', 'Human alias', '--expected-revision', '0',
                '--expected-attempt-id', job['attempt_id'], '--request-id', uuid4().hex, '--json']
        code, output = self.call(args)
        self.assertEqual(code, 0, output)
        row = json.loads(output)
        self.assertEqual((row['revision'], row['project'], row['series']), (1, 'ZIS', '比較'))
        self.assertEqual(self.call(args), (code, output))
        code, output = self.call(['query', '--text', '[literal].*', '--project', 'ZIS', '--tag', '[literal]', '--json'])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['matching_count'], 1)
        code, output = self.call(['catalogue', 'show', job['display_id'], '--json'])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['name'], job['name'])
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'], 'Hold')
        self.assertEqual(self.call(['catalogue', 'edit', job['display_id'], '--project', 'bad', '--expected-revision', '0',
            '--expected-attempt-id', job['attempt_id'], '--json'])[0], 2)
        self.assertEqual(self.call(['catalogue', 'edit', job['display_id'], '--project', 'bad', '--expected-revision', '1',
            '--expected-attempt-id', uuid4().hex, '--json'])[0], 2)

    def test_core_import_does_not_load_textual(self):
        script = ('import argparse,sys; from cmw.jobs.cli import register; p=argparse.ArgumentParser(); '
                  'register(p.add_subparsers()); a=p.parse_args(["jobs","--state",sys.argv[1],"query","--json"]); '
                  'code=a.handler(a); assert "textual" not in sys.modules; raise SystemExit(code)')
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.store.root)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['total_count'], 0)

    def test_remote_cli_uses_exact_owner_and_federation_reports_partial(self):
        fixture = node_catalogue_tests.CatalogueNodeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        client = fixture.client(readonly=False)
        with patch('cmw.nodes.jobs_cli.connect', return_value=client):
            code, output = self.call(['--node', 'fixture', 'catalogue', 'show', fixture.job['display_id'], '--json'], state=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['attempt_id'], fixture.job['attempt_id'])
        self.assertEqual(self.call(['query', '--all-nodes', '--json'])[0], 2)
        self.assertEqual(self.call(['query', '--owner-cursor', 'node=bad', '--json'])[0], 2)

    def test_evidence_fifo_is_refused_without_waiting(self):
        self.store.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        job = self.add()
        fifo = self.root/'declared.json'
        os.mkfifo(fifo)
        code, output = self.call(['catalogue', 'relate', job['display_id'],
            '--parent-node', uuid4().hex, '--parent-queue', uuid4().hex,
            '--parent-job', '1', '--parent-attempt', uuid4().hex, '--kind', 'comparison',
            '--expected-revision', '0', '--expected-attempt-id', job['attempt_id'],
            '--evidence', str(fifo), '--json'])
        self.assertEqual(code, 2, output)
        self.assertIn('regular file', output)

    def test_relation_intent_roundtrip_has_no_scientific_claim(self):
        self.store.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        job = self.add()
        args = ['catalogue', 'relate', job['display_id'], '--parent-node', uuid4().hex,
                '--parent-queue', uuid4().hex, '--parent-job', '1', '--parent-attempt', uuid4().hex,
                '--kind', 'repeat', '--expected-revision', '0', '--expected-attempt-id', job['attempt_id'], '--json']
        code, output = self.call(args)
        self.assertEqual(code, 0, output)
        record = json.loads(output)
        self.assertEqual(record['relations'][0]['basis'], 'intent')
        self.assertEqual(record['relations'][0]['resolution'], 'UNRESOLVED_REMOTE')
        code, output = self.call(['catalogue', 'retract', job['display_id'], record['relations'][0]['relation_id'],
            '--expected-revision', str(record['revision']), '--expected-attempt-id', job['attempt_id'], '--json'])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['relations'][0]['state'], 'RETRACTED')
