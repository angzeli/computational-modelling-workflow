"""Explicit node CLI selection cannot turn a preview into local execution."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cmw.cli import main
from cmw.jobs import runtime
from cmw.jobs.store import JobsError
from cmw.nodes.protocol import NodeError


class NodeCLISelectionTests(unittest.TestCase):
    def test_local_start_temporary_ownership_refusal_is_exit_75_not_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            message = 'Controller ownership temporarily unverifiable; retry start; nothing launched'
            for json_output in (False, True):
                with self.subTest(json=json_output), patch.object(runtime, 'start',
                        side_effect=runtime.ControllerStartUnavailable('controller.start_token', message)):
                    code, output = self.call(['jobs', '--state', str(state), 'start',
                                              *(['--json'] if json_output else [])])
                self.assertEqual(code, 75)
                self.assertIn('ownership temporarily unverifiable; retry', output)
                if json_output:
                    self.assertEqual(json.loads(output), dict(status='UNVERIFIABLE',
                        code='OWNERSHIP_UNVERIFIABLE', reason=message, exit_code=75))
            with patch.object(runtime, 'start', side_effect=JobsError('Controller is still alive')):
                code, output = self.call(['jobs', '--state', str(state), 'start', '--json'])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)['status'], 'FAILED')
            self.assertFalse(state.exists())

    def test_node_start_temporary_ownership_refusal_is_exit_75(self):
        backend = Mock()
        backend.call.side_effect = NodeError('OWNERSHIP_UNVERIFIABLE',
            'Controller ownership temporarily unverifiable; retry with a fresh preview; nothing launched')
        with patch('cmw.nodes.jobs_cli.connect', return_value=backend):
            code, output = self.call(['jobs', '--node', 'fixture', 'start', '--json'])
        self.assertEqual(code, 75)
        self.assertEqual(json.loads(output)['status'], 'UNVERIFIABLE')
        self.assertEqual(json.loads(output)['code'], 'OWNERSHIP_UNVERIFIABLE')
        backend.call.assert_called_once_with('control.preview', {'action': 'controller.start'}, None)
        backend.close.assert_called_once()

    def call(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(arguments)
        return code, output.getvalue()

    def test_node_only_mutations_and_preview_flags_do_not_create_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'missing'
            with patch('cmw.jobs.store.Store.transaction', side_effect=AssertionError('No queue writes')):
                for arguments in (['start', '--preview'], ['hold', 'J1.1', '--preview'],
                        ['release', 'J1.1', '--request-id', 'a' * 32],
                        ['fetch', 'J1.1', '--spec', '/not/read.json', '--destination', '/not/created'],
                        ['request', 'a' * 32]):
                    with self.subTest(arguments=arguments):
                        code, output = self.call(['jobs', '--state', str(state), *arguments])
                        self.assertEqual(code, 2)
                        self.assertIn('--node', output)
                        self.assertFalse(state.exists())

    def test_local_reviewed_submit_reports_input_error_without_queue_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'missing'
            with patch('cmw.jobs.store.Store.transaction', side_effect=AssertionError('No queue writes')):
                code, output = self.call(['jobs', '--state', str(state), 'submit', '--spec', '/not/read.json'])
            self.assertEqual(code, 2)
            self.assertIn('"status": "ERROR"', output)
            self.assertFalse(state.exists())

    def test_ordinary_status_never_constructs_node_backend(self):
        with tempfile.TemporaryDirectory() as directory, patch('cmw.nodes.connections.ConnectedBackend',
                side_effect=AssertionError('Local-only invocation must not connect')):
            code, output = self.call(['jobs', '--state', str(Path(directory) / 'absent'), 'status', '--json'])
            self.assertEqual(code, 0)
            self.assertIn('controller', output)

    def test_all_nodes_without_registry_refuses_without_local_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'absent'
            code, output = self.call(['jobs', '--nodes-config', str(root), 'watch', '--all-nodes'])
            self.assertEqual(code, 2)
            self.assertIn('UNKNOWN_NODE', output)
            self.assertFalse(root.exists())
