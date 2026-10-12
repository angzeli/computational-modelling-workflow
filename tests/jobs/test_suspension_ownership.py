"""Bounded cancellation absence probes never broaden signal authority."""
from contextlib import ExitStack
import signal
import unittest
from unittest.mock import patch

from cmw.jobs.ownership import signal_session
from cmw.jobs.store import JobsError


class ControlledAbsenceProbeTests(unittest.TestCase):
    def observe(self, outcome, *, grace=.5, lose_owner=False):
        now = [0.0]
        lost = [False]
        calls = []
        def kill(pgid, signum):
            calls.append((pgid, signum))
            self.assertEqual((pgid, signum), (11, 0))
            if len(calls) == 1 or outcome == 'denied':
                if lose_owner:
                    lost[0] = True
                raise PermissionError('modeled transient zombie group')
            if outcome == 'absent':
                raise ProcessLookupError()
        with ExitStack() as stack:
            stack.enter_context(patch('cmw.jobs.ownership.owner_alive',
                                      side_effect=lambda owner: owner['pid'] == 10 and not lost[0]))
            stack.enter_context(patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid}))
            stack.enter_context(patch('cmw.jobs.ownership.group_members', return_value=[12]))
            stack.enter_context(patch('os.getsid', return_value=10))
            stack.enter_context(patch('os.getpgid', side_effect=lambda pid: 10 if pid == 10 else 11))
            stack.enter_context(patch('os.killpg', side_effect=kill))
            stack.enter_context(patch('cmw.jobs.ownership.time.monotonic', side_effect=lambda: now[0]))
            stack.enter_context(patch('cmw.jobs.ownership.time.sleep', side_effect=lambda seconds: now.__setitem__(0, now[0]+seconds)))
            try:
                signal_session({'pid': 10}, signal.SIGTERM, include_leader=False, absence_grace=grace)
            except JobsError as exc:
                return calls, now[0], str(exc)
        return calls, now[0], None

    def test_transient_denial_then_verified_absence_sends_no_signal(self):
        calls, elapsed, error = self.observe('absent')
        self.assertEqual(calls, [(11, 0), (11, 0)])
        self.assertLessEqual(elapsed, .5)
        self.assertIsNone(error)

    def test_persistent_denial_remains_unknown_after_bounded_observation(self):
        calls, elapsed, error = self.observe('denied')
        self.assertTrue(all(signum == 0 for _, signum in calls))
        self.assertEqual(elapsed, .5)
        self.assertIn('absence cannot be verified', error)

    def test_legacy_default_still_refuses_first_denial(self):
        calls, elapsed, error = self.observe('absent', grace=0)
        self.assertEqual(calls, [(11, 0)])
        self.assertEqual(elapsed, 0)
        self.assertIn('absence cannot be verified', error)

    def test_live_unwitnessed_group_never_authorizes_numeric_signal(self):
        calls, _, error = self.observe('exists')
        self.assertEqual(calls, [(11, 0), (11, 0)])
        self.assertIn('no verified surviving member', error)

    def test_owner_loss_between_observations_refuses_immediately(self):
        calls, _, error = self.observe('absent', lose_owner=True)
        self.assertEqual(calls, [(11, 0)])
        self.assertIn('ownership lost', error)
