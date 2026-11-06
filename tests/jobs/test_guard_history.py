"""Guard observation failures deny admission even when their history is coalesced."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cmw.jobs import activity, guard_history
from cmw.jobs.store import Store


class GuardHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'queue')

    @contextmanager
    def record(self, guard, tick):
        with self.store.transaction() as con, \
                patch.object(guard_history, 'clock_stamp', return_value={
                    'monotonic': tick, 'boot_session_id': 'fixture-boot'}), \
                patch.object(guard_history.time, 'time', return_value=1000 + tick):
            guard_history.record_guard(self.store, con, guard)
            yield con

    def test_timeout_and_budget_exhaustion_then_recovery_make_one_event(self):
        release = threading.Event()
        observer = activity.Observer(lambda *args: release.wait(5))
        try:
            # Hold this collector until its caller has returned the timeout.
            with patch.object(threading.Thread, 'join', return_value=None):
                timeout = observer.scan(self.store, {})
        finally:
            release.set()
            observer.thread.join(5)
        self.assertIn('timed out', timeout['reason'])
        def expired(store, state, _deadline):
            return activity.collect(store, state, -1, pids_provider=lambda: [])
        exhausted = activity.Observer(expired).scan(self.store, {})
        self.assertIn('bounded scan budget', exhausted['reason'])
        for tick, guard in enumerate((timeout, exhausted)):
            with self.record(guard, tick) as con:
                latest = self.store.control(con)['external_guard']
                self.assertFalse(activity.permits(latest))
                self.assertEqual(latest['state'], 'UNAVAILABLE')
        recovered = {'state': 'BUSY', 'reason': 'Fixture computation observed'}
        with self.record(recovered, 2) as con:
            self.assertEqual(self.store.control(con)['external_guard'], recovered)
            messages = [row[0] for row in con.execute('SELECT message FROM events')]
        self.assertEqual(len(messages), 1)
        for piece in ('2 unavailable scan(s)', '1000.000 to 1002.000', '2.000 s', 'timed out', 'bounded scan budget', 'BUSY'):
            self.assertIn(piece, messages[0])

    def test_slow_or_disabled_recovery_keeps_separate_events(self):
        with patch.dict(os.environ, {'CMW_JOBS_GUARD_RECOVERY_SECONDS': '1'}):
            with self.record({'state': 'UNAVAILABLE', 'reason': 'timeout'}, 2):
                pass
            with self.record({'state': 'NO_MATCH', 'reason': 'clear'}, 3) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)

    def test_recovery_window_range_and_invalid_setting_warning(self):
        for setting, expected, invalid in [
                ('0', 0., False), ('1.5', 1.5, False), ('60', 60., False),
                ('', 5., True), ('five', 5., True), ('-1', 5., True),
                ('60.1', 5., True), ('1e308', 5., True), ('nan', 5., True), ('inf', 5., True)]:
            with self.subTest(setting=setting), self.store.transaction() as con, \
                    patch.dict(os.environ, {'CMW_JOBS_GUARD_RECOVERY_SECONDS': setting}):
                control = self.store.control(con)
                before = con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
                guard_history.configure_recovery(self.store, con, control)
                self.assertEqual(control['guard_recovery_seconds'], expected)
                after = con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
                self.assertEqual(after - before, int(invalid))
                if invalid:
                    warning = con.execute('SELECT message FROM events ORDER BY id DESC LIMIT 1').fetchone()[0]
                    self.assertIn('CMW_JOBS_GUARD_RECOVERY_SECONDS', warning)
                    self.assertIn('using 5 seconds', warning)

    def test_workers_use_controller_window_and_snapshot_reports_effective_setting(self):
        with self.store.transaction() as con, patch.dict(os.environ, {'CMW_JOBS_GUARD_RECOVERY_SECONDS': '1'}):
            control = self.store.control(con)
            guard_history.configure_recovery(self.store, con, control)
            self.store.set_control(con, control)
        with patch.dict(os.environ, {'CMW_JOBS_GUARD_RECOVERY_SECONDS': '60'}):
            with self.record({'state': 'UNAVAILABLE', 'reason': 'timeout'}, 2):
                pass
            with self.record({'state': 'NO_MATCH', 'reason': 'clear'}, 4) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
        self.assertEqual(self.store.snapshot()['controller']['guard_recovery_seconds'], 1.)

    def test_flood_preserves_lifecycle_and_queue_events_on_disk_and_in_snapshot(self):
        with self.store.transaction() as con:
            self.store.event(con, 1, 'Execution started')
            self.store.event(con, None, 'Controller online')
            for index in range(210):
                guard_history.record_guard(self.store, con, {'state': 'BUSY', 'reason': str(index)})
            self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0], 102)
        messages = [row['message'] for row in self.store.snapshot()['events']]
        self.assertEqual(messages[:2], ['Execution started', 'Controller online'])
        self.assertEqual(len(messages), 32)
