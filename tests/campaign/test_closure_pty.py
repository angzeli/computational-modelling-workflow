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


class ActiveScreenRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_predecessor_is_retargeted_without_waiting_for_its_refresh(self):
        import asyncio
        from types import SimpleNamespace
        from tests.campaign.paint_barrier import active_screen_refresh
        def screen():
            return SimpleNamespace(is_mounted=True, is_attached=True, size=(80, 24),
                                   _layout_required=False, _task=asyncio.Future())
        old, current = screen(), screen()
        entered = asyncio.Event()
        async def old_refresh():
            entered.set()
            await asyncio.Future()  # The real removed-screen refresh never fires.
        async def current_refresh():
            return True
        old.wait_for_refresh, current.wait_for_refresh = old_refresh, current_refresh
        app = SimpleNamespace(screen=old)
        trace = []
        waiter = asyncio.create_task(active_screen_refresh(app, expected=lambda: app.screen is current,
                                                          records=trace, timeout=.2))
        await entered.wait()
        old.is_mounted = old.is_attached = False
        app.screen = current
        old._task.set_result(None)
        result = await waiter
        self.assertEqual(result['current']['object'], id(current))
        self.assertEqual(result['waited'][0]['outcome'], 'retargeted')
        self.assertFalse(current._task.cancelled())
        self.assertEqual(result['outcome'], 'refreshed')

    async def test_live_screen_without_refresh_still_times_out_with_exact_object(self):
        import asyncio
        from types import SimpleNamespace
        from tests.campaign.paint_barrier import active_screen_refresh
        async def blocked():
            await asyncio.Future()
        current = SimpleNamespace(is_mounted=True, is_attached=True, size=(80, 24),
                                  _layout_required=False, _task=asyncio.Future(), wait_for_refresh=blocked)
        trace = []
        with self.assertRaises(TimeoutError):
            await active_screen_refresh(SimpleNamespace(screen=current), records=trace, timeout=.02)
        self.assertEqual(trace[0]['outcome'], 'deadline')
        self.assertEqual(trace[0]['current']['object'], id(current))
        self.assertFalse(current._task.cancelled())


class CompactVisualContractTests(unittest.TestCase):
    def test_required_inventory_has_exact_distinct_coverage_and_synthetic_header(self):
        from tests.campaign.visual_contract_v2 import plan, synthetic_machine
        from cmw.jobs.cli import machine_text
        value = plan()
        self.assertEqual(len(value['core']), 24)
        self.assertEqual(len(value['modals']), 8)
        self.assertEqual(len({row['id'] for row in value['core']+value['modals']}), 32)
        self.assertEqual(machine_text(synthetic_machine()), value['synthetic_machine_header'])
        self.assertEqual({tuple(row['size']) for row in value['modals']}, {(80, 24)})
