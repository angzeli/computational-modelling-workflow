"""A bounded diagnostic file must never backpressure the owned native PTY."""
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import threading
import tty
import unittest
from unittest.mock import Mock, patch

from tests.campaign.pty import ANSI_CAPTURE_BYTES, Session


class TerminalCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-terminal-capture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = Session.__new__(Session)
        self.session.directory = self.root
        self.session.output_bytes = self.session.retained_bytes = 0
        self.session._drain_stop = threading.Event()
        self.session.process = Mock()
        self.session.master = 123

    def test_cap_truncates_only_storage_and_still_reads_and_replies(self):
        # Small deterministic chunks exercise cap crossing and a split DSR query.
        with (patch('tests.campaign.pty.ANSI_CAPTURE_BYTES', 8),
              patch('tests.campaign.pty.select.select', return_value=([123], [], [])),
              patch('tests.campaign.pty.os.read', side_effect=[b'abcdef', b'ghij', b'\x1b[', b'6n', b'']),
              patch('tests.campaign.pty.os.write', return_value=6) as write):
            self.session.drain()
        self.assertEqual((self.root/'terminal.ansi').read_bytes(), b'abcdefgh')
        report = json.loads((self.root/'terminal-capture.json').read_text())
        self.assertEqual(report['total_received_bytes'], 14)
        self.assertEqual(report['retained_bytes'], 8)
        self.assertEqual(report['discarded_bytes'], 6)
        self.assertTrue(report['truncated'])
        write.assert_called_once_with(123, b'\x1b[1;1R')

    def test_short_capture_is_complete_and_exact(self):
        with (patch('tests.campaign.pty.select.select', return_value=([123], [], [])),
              patch('tests.campaign.pty.os.read', side_effect=[b'actual bytes', b''])):
            self.session.drain()
        report = self.session.terminal_capture()
        self.assertEqual(report['total_received_bytes'], len(b'actual bytes'))
        self.assertEqual(report['retained_bytes'], report['total_received_bytes'])
        self.assertEqual(report['discarded_bytes'], 0)
        self.assertFalse(report['truncated'])


class FiniteNativeWriterTests(unittest.TestCase):
    def test_more_than_eight_mib_exits_and_dsr_works_after_capture_cap(self):
        with tempfile.TemporaryDirectory(prefix='cmw-terminal-finite-') as temporary:
            session = Session.__new__(Session)
            session.directory = Path(temporary)
            session.output_bytes = session.retained_bytes = 0
            session._drain_stop = threading.Event()
            session.deadline = None
            session.cleanup_notes = []
            session.master, slave = pty.openpty()
            tty.setraw(slave)
            session._fds = {session.master}
            count = ANSI_CAPTURE_BYTES+65536
            code = ("import os,signal;signal.alarm(4)\n"
                    f"remaining={count};block=b'x'*65536\n"
                    "while remaining: remaining-=os.write(1,block[:remaining])\n"
                    "os.write(1,b'\\x1b[6n')\n"
                    "assert os.read(0,32)==b'\\x1b[1;1R'\n")
            try:
                session.process = subprocess.Popen([sys.executable, '-I', '-B', '-c', code],
                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            finally:
                os.close(slave)
            session.thread = threading.Thread(target=session.drain)
            session.thread.start()
            try:
                self.assertEqual(session.process.wait(timeout=5), 0)
            finally:
                session.close()  # Signals only the original Popen if the finite fixture fails.
            self.assertEqual(session.output_bytes, count+4)
            self.assertEqual(session.retained_bytes, ANSI_CAPTURE_BYTES)
            self.assertEqual((session.directory/'terminal.ansi').stat().st_size, ANSI_CAPTURE_BYTES)
            report = json.loads((session.directory/'terminal-capture.json').read_text())
            self.assertEqual(report['discarded_bytes'], 65536+4)
            self.assertTrue(report['truncated'])
            self.assertFalse(session.thread.is_alive())
            self.assertFalse(session._fds)
