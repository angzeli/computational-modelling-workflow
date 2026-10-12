"""Actual PTY keys and resize against a labelled UI-only fictional authority."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
import termios
import time
import unittest

from tests.jobs.isolated_runtime import environment
from tests.jobs.suspension_ui_fixture import make_fixture
from tests.jobs.test_terminal import terminal, read_until, close_terminal


@unittest.skipUnless(importlib.util.find_spec('textual'), 'Optional Jobs console not installed')
class SuspensionTerminalTests(unittest.TestCase):
    def test_real_keys_resize_stale_repeat_detach_and_reopen(self):
        with tempfile.TemporaryDirectory(prefix='cmw suspension pty ') as temporary:
            root = Path(temporary).resolve()
            store, job = make_fixture(root)
            env = {**environment(), 'TERM': 'xterm-256color', 'CMW_JOBS_STATE': str(store.root)}
            argv = [sys.executable, '-m', 'tests.jobs.suspension_ui_fixture', str(store.root)]
            captured = bytearray()

            def read(fd, needle):
                rendered = read_until(fd, needle)
                captured.extend(rendered)
                return rendered

            def confirm(fd):
                os.write(fd, b'\r')
                read(fd, b'Reason retained')
                os.write(fd, b'\r')

            def resize(pid, fd, width, height):
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))
                os.kill(pid, signal.SIGWINCH)

            pid, fd = terminal(argv, env)
            try:
                resize(pid, fd, 77, 24)
                read(fd, b'Suspend job')
                os.write(fd, b'u')
                read(fd, b'EXECUTION CONTROL')
                os.write(fd, b'ucpxq')
                read(fd, b'ucpxq')
                # First Enter focuses the button; second explicitly confirms.
                confirm(fd)
                read(fd, b'Continue job')
                self.assertEqual(store.snapshot()['jobs'][0]['suspension']['phase'], 'Suspended')
                requests = json.loads((store.root/'ui-submissions.json').read_text())
                self.assertEqual([(item['action'], item['reason']) for item in requests], [('suspend', 'ucpxq')])
                os.write(fd, b'n')
                read(fd, b'human annotation')
                os.write(fd, b'ucpxq')
                read(fd, b'ucpxq')
                os.write(fd, b'\x13')
                read(fd, b'Continue job')
                self.assertEqual(store.snapshot()['jobs'][0]['note'], 'ucpxq')
                os.write(fd, b'q')
            finally:
                close_terminal(pid, fd)
            self.assertEqual(store.snapshot()['jobs'][0]['suspension']['phase'], 'Suspended')
            self.assertFalse(store.snapshot()['controller']['dispatch'])

            pid, fd = terminal(argv, env)
            try:
                resize(pid, fd, 80, 24)
                read(fd, b'Continue job')
                os.write(fd, b'u')
                read(fd, b'EXECUTION CONTROL')
                resize(pid, fd, 140, 40)
                # Native refresh sees a changed request revision; no signalling is involved.
                with store.transaction() as con:
                    current = store.get(con, job['id'])
                    current['suspension']['revision'] += 1
                    store.save(con, current)
                time.sleep(.7)  # One existing 0.5-second UI refresh, not a retry loop.
                confirm(fd)
                read(fd, b'stale')
                self.assertEqual(len(json.loads((store.root/'ui-submissions.json').read_text())), 1)
                os.write(fd, b'\x1b')
                read(fd, b'External:')
                os.write(fd, b'u')
                read(fd, b'EXECUTION CONTROL')
                confirm(fd)
                read(fd, b'Suspend job')
                requests = json.loads((store.root/'ui-submissions.json').read_text())
                self.assertEqual([item['action'] for item in requests], ['suspend', 'continue'])
                self.assertEqual({item['attempt_id'] for item in requests}, {job['attempt_id']})
                os.write(fd, b'x')
                read(fd, b'Type CANCEL')
                os.write(fd, b'\x1b')
                read(fd, b'External:')
                os.write(fd, b'u')
                read(fd, b'EXECUTION CONTROL')
                os.write(fd, b'\x03')
            finally:
                close_terminal(pid, fd)
            self.assertEqual(store.snapshot()['jobs'][0]['suspension']['phase'], 'Run')
            self.assertEqual(store.snapshot()['jobs'][0]['note'], 'ucpxq')
            self.assertFalse(store.snapshot()['controller']['dispatch'])
            if os.environ.get('CMW_SUSPENSION_UI_EVIDENCE'):
                evidence = Path(os.environ['CMW_SUSPENSION_UI_EVIDENCE'])
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence/'portal-pty.bin').write_bytes(captured)
                (evidence/'portal-pty.json').write_text(json.dumps(dict(
                    qualification='UI-only fictional authority; no process suspension claimed',
                    sizes=[[77, 24], [80, 24], [140, 40]], submissions=requests,
                    final_phase='Run', note='ucpxq', dispatch=False, clients_reaped=2), indent=2))
