"""Control freshness across a wall-clock step, restart and missing evidence."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from cmw.jobs import activity, freshness, ownership_hold, runtime, sharing
from cmw.jobs.cli import status_text
from cmw.jobs.store import Store
from cmw.jobs.suspension_protocol import clock_descriptor


def stamp(seconds, boot='fixture-session'):
    return {'monotonic': seconds, 'boot_session_id': boot}


class ClockFreshnessTests(TestCase):
    def test_guard_uses_inclusive_monotonic_window_and_boot(self):
        guard = {'state': 'NO_MATCH', 'stale': False, 'coverage': {'complete': True},
                 'observed_at': 1793633892., 'observed_clock': stamp(100.)}
        for current, allowed in ((stamp(100.), True), (stamp(102.), True),
                                 (stamp(102.001), False), (stamp(99.), False),
                                 (stamp(101., 'another-boot'), False), (None, False)):
            with self.subTest(current=current), patch.object(activity, 'clock_stamp', return_value=current):
                self.assertIs(activity.permits(guard), allowed)
                self.assertEqual(bool(sharing.guard_fresh(guard, current)), allowed)
        del guard['observed_clock']
        self.assertFalse(sharing.guard_fresh(guard, stamp(100.)))

    def test_missing_or_corrupt_stamps_cannot_be_used_as_evidence(self):
        with patch('cmw.jobs.ownership.boot_session_id', side_effect=PermissionError):
            self.assertIsNone(freshness.clock_stamp())
        for invalid in (None, {}, stamp(True), stamp(float('nan')), stamp(-1), stamp(4., '')):
            with self.subTest(invalid=invalid):
                self.assertIsNone(freshness.age_seconds(invalid, stamp(10.)))

    def test_heartbeat_detects_wall_step_once_without_changing_dispatch(self):
        store = Mock()
        control = {'dispatch': True, 'stop': False, 'heartbeat': 100., 'heartbeat_clock': stamp(10.)}
        with patch.object(runtime, 'clock_stamp', side_effect=[stamp(11.), stamp(12.), stamp(1., 'new-boot')]), \
                patch.object(runtime.time, 'time', side_effect=[28892.6, 28893.6, 12.]):
            for _ in range(3):
                runtime.update_heartbeat(store, None, control)
        store.event.assert_called_once_with(None, None, 'Wall clock stepped by +28791.600 s')
        self.assertIs(control['dispatch'], True)
        self.assertIs(control['stop'], False)
        self.assertEqual(control['heartbeat_clock'], stamp(1., 'new-boot'))

    def test_persisted_control_hold_with_uncomparable_clock_escalates_and_retains_control(self):
        # The prior boot may have had a much larger monotonic counter. Waiting
        # for that numeric value to recur could hide uncertainty for days.
        for first, second in ((stamp(10000.), stamp(1., 'new-boot')),
                              (stamp(10000.), stamp(1.)), (None, None), (stamp(10000.), None)):
            with self.subTest(first=first, second=second):
                store, job = Mock(), {'id': 1, 'status': 'Run'}
                with patch.object(ownership_hold, 'clock_stamp', side_effect=[first, second]):
                    ownership_hold.unavailable(store, None, job, 'guardian', 'boot_session', 10000.)
                    self.assertEqual(job['status'], 'Run')
                    self.assertIn('guardian', job['ownership_holds'])
                    ownership_hold.unavailable(store, None, job, 'guardian', 'boot_session', 1.)
                self.assertEqual((job['status'], job['ownership_resume_status']), ('Unknown', 'Run'))
                self.assertIn('guardian', job['ownership_holds'])
                self.assertIn('duration cannot be established', job['reason'])
                ownership_hold.recovered(store, None, job, 'guardian')
                self.assertEqual(job['status'], 'Run')

    def test_launch_grace_ignores_wall_steps_and_expires_at_five_seconds(self):
        store = Mock(root=Path('unused'))
        job = {'status': 'Starting', 'worker': None, 'attempt_id': 'one',
               'claimed_at': -100000., 'claim_clock': stamp(100.)}
        for current, grace in ((stamp(104.999), True), (stamp(105.), False), (stamp(101., 'new'), False)):
            with self.subTest(current=current), patch.object(runtime, 'clock_stamp', return_value=current), \
                    patch.object(runtime, 'mark_unknown') as unknown:
                runtime.reconcile(store, None, job)
                self.assertEqual(unknown.call_count, 0 if grace else 1)

    def test_sleep_inclusive_clock_tied_only_to_boot_session(self):
        with patch('cmw.jobs.suspension_protocol.boot_session_id', return_value='session'), \
                patch('psutil.boot_time', side_effect=AssertionError('Adjusted wall time used')):
            self.assertEqual(clock_descriptor()['boot_session_id'], 'session')

    def test_legacy_or_previous_boot_start_pauses_dispatch_and_explains_why(self):
        for claim_clock, detail in ((None, 'claim stamp missing'), (stamp(1., 'prior'), 'boot session differs')):
            with self.subTest(detail=detail), TemporaryDirectory() as directory:
                store = Store(Path(directory) / 'queue')
                job = store.add(argv=['/bin/true'], cwd=directory, name='Legacy claim')
                with store.transaction() as con:
                    control = store.control(con)
                    control['dispatch'] = True
                    store.set_control(con, control)
                    job.update(status='Starting', claim='claimed')
                    if claim_clock:
                        job['claim_clock'] = claim_clock
                    store.save(con, job)
                    with patch.object(runtime, 'clock_stamp', return_value=stamp(100.)), \
                            patch.object(runtime, 'signal_session') as send, patch.object(runtime, 'detached') as spawn:
                        runtime.reconcile(store, con, store.get(con, job['id']))
                    send.assert_not_called()
                    spawn.assert_not_called()
                state = store.snapshot()
                reason = state['jobs'][0]['reason']
                self.assertEqual(state['jobs'][0]['status'], 'Unknown')
                self.assertFalse(state['controller']['dispatch'])
                self.assertIn('launch grace cannot be established from a legacy or previous-boot stamp', reason)
                self.assertIn(detail, reason)
                self.assertIn(reason, [event['message'] for event in state['events']])
                self.assertIn(reason, status_text(state))
