"""Failure-path checks for native-session ownership; no app or engine launch."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.campaign.pty import Session


class SessionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-pty-cleanup-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self):
        session = Session.__new__(Session)
        session.directory = self.root
        session.deadline = None
        session.cleanup_notes = []
        session.master, session.writer = os.pipe()
        session.reader, extra = os.pipe()
        os.close(extra)
        session._fds = {session.master, session.writer, session.reader}
        self.descriptors = tuple(session._fds)
        from threading import Event
        session._drain_stop = Event()
        session.thread = None
        session.process = Mock(returncode=0)
        session.process.poll.return_value = None
        return session

    def assert_closed(self, session):
        self.assertFalse(session._fds)
        for fd in self.descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)
        record = json.loads((self.root/'session-cleanup.json').read_text())
        self.assertEqual(record['open_descriptors'], 0)
        self.assertFalse(record['drain_alive'])

    def test_send_failure_still_reaps_and_closes_all_descriptors(self):
        session = self.fixture()
        with patch.object(session, 'send', side_effect=OSError('terminal closed')):
            self.assertEqual(session.close(), 0)
        session.process.wait.assert_called_once()
        self.assert_closed(session)

    def test_failed_wait_still_closes_and_records_failure(self):
        session = self.fixture()
        session.process.wait.side_effect = OSError('wait failed')
        with patch.object(session, 'send'):
            with self.assertRaises(AssertionError):
                session.close()
        self.assert_closed(session)
        self.assertIn('wait failed', (self.root/'session-cleanup.json').read_text())

    def test_terminate_timeout_kills_only_original_popen_then_reaps(self):
        session = self.fixture()
        session.process.wait.side_effect = [subprocess.TimeoutExpired('owned', 10),
                                           subprocess.TimeoutExpired('owned', 2), 0]
        with patch.object(session, 'send'):
            self.assertEqual(session.close(), 0)
        session.process.terminate.assert_called_once()
        session.process.kill.assert_called_once()
        self.assert_closed(session)

    def test_failed_launch_closes_each_descriptor_and_retains_error(self):
        import pty
        original_pty, original_pipe = pty.openpty, os.pipe
        opened = []
        def observed(call):
            def run():
                result = call()
                opened.extend(result)
                return result
            return run
        directory = self.root/'failed'
        with (patch('tests.campaign.pty.pty.openpty', side_effect=observed(original_pty)),
              patch('tests.campaign.pty.os.pipe', side_effect=observed(original_pipe)),
              patch('tests.campaign.pty.subprocess.Popen', side_effect=FileNotFoundError('missing interpreter'))):
            with self.assertRaises(FileNotFoundError):
                Session(self.root/'unused.json', directory)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertIn('missing interpreter', (directory/'session-startup-failure.json').read_text())
        self.assertEqual(json.loads((directory/'session-cleanup.json').read_text())['open_descriptors'], 0)

    def test_thread_start_failure_reaps_original_child_and_closes_descriptors(self):
        process = Mock(returncode=0)
        process.poll.return_value = None
        thread = Mock(ident=None)
        thread.start.side_effect = RuntimeError('thread start failed')
        thread.is_alive.return_value = False
        directory = self.root/'thread-failed'
        with (patch('tests.campaign.pty.subprocess.Popen', return_value=process),
              patch('tests.campaign.pty.threading.Thread', return_value=thread)):
            with self.assertRaises(RuntimeError):
                Session(self.root/'unused.json', directory)
        process.wait.assert_called_once()
        self.assertEqual(json.loads((directory/'session-cleanup.json').read_text())['open_descriptors'], 0)
        self.assertIn('thread start failed', (directory/'session-startup-failure.json').read_text())
