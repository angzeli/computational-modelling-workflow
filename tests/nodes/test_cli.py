"""Explicit node CLI selection cannot turn a preview into local execution."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main


class NodeCLISelectionTests(unittest.TestCase):
    def call(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(arguments)
        return code, output.getvalue()

    def test_node_only_mutations_and_preview_flags_do_not_create_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'missing'
            with patch('cmw.jobs.store.Store.transaction', side_effect=AssertionError('No queue writes')):
                for arguments in (['start', '--preview'], ['hold', 'J1.1', '--preview'],
                        ['release', 'J1.1', '--request-id', 'a' * 32],
                        ['submit', '--spec', '/not/read.json'],
                        ['fetch', 'J1.1', '--spec', '/not/read.json', '--destination', '/not/created'],
                        ['request', 'a' * 32]):
                    with self.subTest(arguments=arguments):
                        code, output = self.call(['jobs', '--state', str(state), *arguments])
                        self.assertEqual(code, 2)
                        self.assertIn('--node', output)
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
