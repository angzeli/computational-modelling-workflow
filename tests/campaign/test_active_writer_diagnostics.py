"""Failure diagnostics are independent of a responding UI event loop."""
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.campaign.active_writer_pty import DiagnosticSession, command_trace
from tests.campaign.pty import Session


class ActiveWriterDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-writer-trace-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = DiagnosticSession.__new__(DiagnosticSession)
        self.session.directory = self.root
        self.session.process = Mock(pid=123)
        self.session.process.poll.return_value = None
        (self.root/'stack-handler.json').write_text(json.dumps({'pid': 123}))

    def records(self):
        return [json.loads(line) for line in (self.root/'parent-commands.jsonl').read_text().splitlines()]

    def test_deadline_retains_start_error_and_signals_original_live_child(self):
        with patch.object(Session, 'request', side_effect=TimeoutError('original deadline')):
            with self.assertRaisesRegex(TimeoutError, 'original deadline'):
                self.session.request('input_received', after={'inputs': 42})
        self.assertEqual([row['phase'] for row in self.records()], ['start', 'error', 'stack-requested'])
        self.assertEqual(self.records()[0]['command']['after'], {'inputs': 42})
        self.session.process.send_signal.assert_called_once_with(signal.SIGUSR1)

    def test_success_and_reaped_failure_never_signal(self):
        with patch.object(Session, 'request', return_value={'done': True}):
            self.assertEqual(self.session.request('ready'), {'done': True})
        self.assertEqual([row['phase'] for row in self.records()], ['start', 'finish'])
        self.session.process.poll.return_value = 0
        with patch.object(Session, 'request', side_effect=AssertionError('exited')):
            with self.assertRaisesRegex(AssertionError, 'exited'):
                self.session.request('ready')
        self.session.process.send_signal.assert_not_called()

    def test_unregistered_handler_never_sends_terminating_default_signal(self):
        (self.root/'stack-handler.json').unlink()
        with patch.object(Session, 'request', side_effect=TimeoutError('startup deadline')):
            with self.assertRaises(TimeoutError):
                self.session.request('ready')
        self.session.process.send_signal.assert_not_called()
        self.assertEqual(self.records()[-1]['phase'], 'stack-unavailable')

    def test_child_command_start_is_readable_before_later_completion(self):
        command_trace(self.root, 'child-commands', 'ui-start', {'operation': 'input_received'}, state={'focus':'field'})
        rows = [json.loads(line) for line in (self.root/'child-commands.jsonl').read_text().splitlines()]
        self.assertEqual(rows[0]['phase'], 'ui-start')
        self.assertEqual(rows[0]['state'], {'focus':'field'})
