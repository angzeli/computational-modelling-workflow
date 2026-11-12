"""Explicit successor CLI grammar and owner-local forwarding remain core-only."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.cli import register
from cmw.jobs.store import Store
from cmw.jobs.successor_models import SuccessorResult, SuccessorUnverifiable
from tests.jobs.test_catalogue_cli import run_jobs
from tests.nodes import test_successor


class SuccessorCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-successor-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'state')
        self.attempt = uuid4().hex
        self.digest = 'a' * 64
        self.result = SuccessorResult(1, uuid4().hex, uuid4().hex, 1, self.attempt,
                                     'plan', 'READY', self.digest, '{"checkpoint":"refused"}')

    def call(self, arguments, *, local=True):
        return run_jobs(arguments, self.store.root if local else None)

    def test_plan_prints_complete_result_and_writes_no_preview_file(self):
        with patch('cmw.jobs.successor.SuccessorService') as factory:
            factory.return_value.plan.return_value = self.result
            code, output = self.call(['plan-next', 'J1.1', '--spec', '/owner/spec', '--json'])
            self.assertEqual(code, 0, output)
            self.assertEqual(json.loads(output)['payload'], {'checkpoint': 'refused'})
            factory.return_value.plan.assert_called_once_with('J1.1', '/owner/spec', expected_attempt_id=None)
            factory.return_value.materialize.assert_not_called()
            factory.return_value.register.assert_not_called()
        self.assertFalse(self.store.root.exists())

    def test_materialization_and_held_registration_are_separate_explicit_operations(self):
        request = uuid4().hex
        with patch('cmw.jobs.successor.SuccessorService') as factory:
            service = factory.return_value
            service.materialize.return_value = replace(self.result, operation='materialize', status='MATERIALIZED')
            service.register.return_value = replace(self.result, operation='register', status='REGISTERED')
            code, output = self.call(['materialize-next', 'J1.1', '--spec', '/owner/spec',
                '--approve-digest', self.digest, '--expected-attempt-id', self.attempt, '--json'])
            self.assertEqual(code, 0, output)
            service.materialize.assert_called_once_with('J1.1', '/owner/spec', self.digest,
                expected_attempt_id=self.attempt)
            service.register.assert_not_called()
            code, output = self.call(['register-next', 'J1.1', '--materialization', '/owner/result.json',
                '--approve-digest', self.digest, '--request-id', request, '--json'])
            self.assertEqual(code, 0, output)
            service.register.assert_called_once_with('J1.1', '/owner/result.json', self.digest, request,
                expected_attempt_id=None)

    def test_missing_approval_or_registration_request_is_parse_error_and_continue_unchanged(self):
        for args in (['materialize-next', 'J1.1', '--spec', '/owner/spec'],
                     ['register-next', 'J1.1', '--materialization', '/owner/record', '--approve-digest', self.digest],
                     ['continue', 'J1.1', '--spec', '/owner/spec']):
            self.assertEqual(self.call(args)[0], 2)
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers())
        args = parser.parse_args(['jobs', 'continue', 'J1.1', '--preview'])
        self.assertEqual(args.jobs_operation, 'continue')
        self.assertTrue(args.preview)

    def test_blocked_plan_uses_nonzero_exit_without_materialization(self):
        with patch('cmw.jobs.successor.SuccessorService') as factory:
            factory.return_value.plan.return_value = replace(self.result, status='BLOCKED')
            code, output = self.call(['plan-next', 'J1.1', '--spec', '/owner/spec', '--json'])
            self.assertEqual(code, 2, output)
            self.assertEqual(json.loads(output)['status'], 'BLOCKED')
            factory.return_value.materialize.assert_not_called()

    def test_retryable_local_commands_preserve_status_reason_and_exit_75(self):
        with patch('cmw.jobs.successor.SuccessorService') as factory:
            service = factory.return_value
            reason = 'Parent queue temporarily unreadable; retry'
            service.plan.return_value = replace(self.result, status='UNVERIFIABLE',
                payload_json=json.dumps({'closure': {'reason': reason}}))
            service.materialize.return_value = replace(self.result, operation='materialize',
                status='PENDING_VERIFICATION', payload_json=json.dumps({'reason': reason, 'record_path': '/owner/record'}))
            service.register.side_effect = SuccessorUnverifiable(reason)
            for command, options, status, error in (
                    ('plan-next', ['--spec', '/owner/spec'], 'UNVERIFIABLE', 'SUCCESSOR_UNVERIFIABLE'),
                    ('materialize-next', ['--spec', '/owner/spec', '--approve-digest', self.digest],
                     'PENDING_VERIFICATION', 'SUCCESSOR_PENDING_VERIFICATION'),
                    ('register-next', ['--materialization', '/owner/record', '--approve-digest', self.digest,
                                       '--request-id', uuid4().hex], 'UNVERIFIABLE', 'SUCCESSOR_UNVERIFIABLE')):
                with self.subTest(command=command):
                    code, output = self.call([command, 'J1.1', *options, '--json'])
                    self.assertEqual(code, 75, output)
                    value = json.loads(output)
                    self.assertEqual((value['status'], value['code'], value['exit_code']), (status, error, 75))
                    self.assertIn(reason, output)

    def test_remote_preflight_lock_returns_retryable_status(self):
        from cmw.nodes.protocol import NodeError
        with patch('cmw.nodes.jobs_cli.connect') as connect:
            backend = connect.return_value
            backend.call.side_effect = NodeError('QUEUE_UNAVAILABLE', 'Owner queue temporarily unreadable; retry')
            code, output = self.call(['--node', 'fixture', 'plan-next', 'J1.1', '--spec', '/owner/spec',
                                      '--json'], local=False)
        self.assertEqual(code, 75)
        value = json.loads(output)
        self.assertEqual((value['status'], value['code'], value['exit_code']), ('UNVERIFIABLE', 'SUCCESSOR_UNVERIFIABLE', 75))
        backend.call.assert_called_once_with('lookup', {'id': 'J1.1'})

    def test_node_cli_forwards_owner_paths_and_immutable_parent(self):
        fixture = test_successor.SuccessorNodeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        client = fixture.client(readonly=False)
        with patch('cmw.nodes.jobs_cli.connect', return_value=client):
            code, output = self.call(['--node', 'fixture', 'plan-next', fixture.job['display_id'],
                '--spec', '/unreadable-on-frontend/owner-spec.json', '--expected-attempt-id', fixture.job['attempt_id'], '--json'], local=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['attempt_id'], fixture.job['attempt_id'])
        fixture.service.plan.assert_called_once_with(fixture.job['id'], '/unreadable-on-frontend/owner-spec.json',
            expected_attempt_id=fixture.job['attempt_id'])

    def test_cli_grammar_import_does_not_require_textual(self):
        script = ('import argparse,sys;from cmw.jobs.cli import register;p=argparse.ArgumentParser();'
                  'register(p.add_subparsers());p.parse_args(["jobs","plan-next","J1.1","--spec","/owner/spec"]);'
                  'assert "textual" not in sys.modules')
        result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
