"""D04: every historical refusal implementation fails safe on the new code.

The pinned snippets are verbatim functions/exception handlers, not re-created
logic. Git history has one protocol response reader and two CLI refusal
handlers; the fixture records the covered revisions and both deployed runtimes.
CI needs neither Git history nor a subprocess to exercise those implementations.
"""
import ast
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from cmw.nodes import protocol


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/nodes/ownership-refusal-clients.json'
CODE = 'OWNERSHIP_UNVERIFIABLE'
MESSAGE = 'Controller replacement temporarily unverifiable; nothing launched; retry start'
REQUEST_ID = '1' * 32
PINNED = {
    'NodeError': {'402bb1face16dd51a3b5754c9af94174c70fb2e9a5a36680fe12a84031cf8f41'},
    'response': {'cf6936b5f4b910f1d6fcfcc3f5515445509bf4e458025d964cb832190acda564'},
    'handle.except': {
        '149bc0f28fc492a04e75a54580476a5d471a07d8715520611f88b41651eae7cd',
        '777c6f9de25c73fd6da8e5829dec39c719b4f158e6f72ee4d96549d63b379e22',
    },
}


class OwnershipRefusalCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())
        self.refusal = {'protocol': 1, 'request_id': REQUEST_ID, 'ok': False,
                        'error': {'code': CODE, 'message': MESSAGE}}

    def verify_source(self, record):
        digest = hashlib.sha256(record['source'].encode()).hexdigest()
        self.assertEqual(digest, record['sha256'])
        self.assertIn(digest, PINNED[record['symbol']])

    def historical_reader(self, record):
        error = self.fixture['node_error']
        self.verify_source(error)
        self.verify_source(record)
        namespace = {'PROTOCOL': self.fixture['protocol']}
        exec(compile(error['source'], str(FIXTURE) + ':NodeError', 'exec'), namespace)
        exec(compile(record['source'], str(FIXTURE) + ':response', 'exec'), namespace)
        return namespace

    def test_pinned_sources_cover_all_historical_variants_and_deployed_revisions(self):
        records = [self.fixture['node_error'], *self.fixture['readers'],
                   *self.fixture['cli_refusal_handlers']]
        for record in records:
            with self.subTest(symbol=record['symbol'], commit=record['commit']):
                self.verify_source(record)
                self.assertRegex(record['commit'], r'^[0-9a-f]{40}$')
        self.assertEqual(len(self.fixture['readers']), 1)
        self.assertEqual(len(self.fixture['cli_refusal_handlers']), 2)
        for symbol, coverage in self.fixture['coverage'].items():
            self.assertEqual(set(coverage), PINNED['handle.except' if symbol == 'handle' else symbol])
            covered = {revision for revisions in coverage.values() for revision in revisions}
            for short, revision in self.fixture['deployed'].items():
                self.assertTrue(revision.startswith(short))
                self.assertIn(revision, covered)
        self.assertEqual(set(self.fixture['deployed']), {'032ddbb', '31616be'})

    def test_historical_reader_refuses_before_success_decoding(self):
        for record in self.fixture['readers']:
            with self.subTest(commit=record['commit']):
                namespace = self.historical_reader(record)
                decode_success = Mock()
                with self.assertRaises(namespace['NodeError']) as refused:
                    value = namespace['response'](self.refusal, REQUEST_ID)
                    decode_success(value)
                self.assertEqual(refused.exception.code, CODE)
                self.assertEqual(str(refused.exception), MESSAGE)
                decode_success.assert_not_called()

    def test_both_historical_cli_handlers_report_refusal_and_exit_two(self):
        for record in self.fixture['cli_refusal_handlers']:
            self.verify_source(record)
            namespace = self.historical_reader(self.fixture['readers'][0])
            namespace['json'] = json
            # Embed the unchanged historical except clause around a supplied
            # endpoint call. No historical connection or enrollment is opened.
            source = ('def historical_cli(args, call):\n' + textwrap.indent(
                'try:\n    return call()\n' + record['source'], '    '))
            tree = ast.parse(source)
            self.assertEqual(len(tree.body[0].body[0].handlers), 1)
            exec(compile(tree, str(FIXTURE) + ':handle.except', 'exec'), namespace)
            for operation in ('start', 'plan-next'):
                with self.subTest(commit=record['commit'], operation=operation):
                    decode_success = Mock()
                    def call():
                        value = namespace['response'](self.refusal, REQUEST_ID)
                        decode_success(value)
                        return 0
                    output = io.StringIO()
                    with redirect_stdout(output):
                        status = namespace['historical_cli'](
                            SimpleNamespace(jobs_operation=operation, json=True), call)
                    self.assertEqual(status, 2)
                    self.assertEqual(json.loads(output.getvalue()), {'code': CODE, 'message': MESSAGE})
                    decode_success.assert_not_called()

    def test_current_reader_keeps_protocol_one_and_the_same_refusal_shape(self):
        self.assertEqual(protocol.PROTOCOL, self.fixture['protocol'])
        self.assertEqual(protocol.PROTOCOL, 1)
        self.assertIs(self.refusal['ok'], False)
        self.assertEqual(set(self.refusal['error']), {'code', 'message'})
        with self.assertRaises(protocol.NodeError) as refused:
            protocol.response(protocol.decode(protocol.encode(self.refusal)), REQUEST_ID)
        self.assertEqual(refused.exception.code, CODE)
        self.assertEqual(str(refused.exception), MESSAGE)
