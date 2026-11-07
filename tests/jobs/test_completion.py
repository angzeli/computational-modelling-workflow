"""Completion needs a definitive absence observation, even after successful KILL."""
from contextlib import ExitStack
import errno
import unittest
from unittest.mock import patch

from cmw.jobs import runtime
from cmw.jobs.store import JobsError


def permission_failure():
    error = JobsError('Process group absence cannot be verified')
    error.__cause__ = PermissionError(1, 'Operation not permitted')
    return error


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(runtime.time, 'monotonic', side_effect=lambda: self.clock))
        self.sleep = self.patches.enter_context(patch.object(runtime.time, 'sleep', side_effect=self.advance))
        self.signals = self.patches.enter_context(patch.object(runtime.os, 'killpg'))

    def advance(self, seconds):
        self.clock += seconds

    def test_transient_permission_failure_requires_later_kernel_absence(self):
        with patch.object(runtime, 'group_exists', side_effect=[permission_failure(), True, False]) as probe:
            runtime._wait_for_group_end(10)
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.signals.assert_not_called()

    def test_unreadable_absence_recovers_even_after_attention_threshold(self):
        for error in (permission_failure(), OSError(errno.EAGAIN, 'try again')):
            with self.subTest(error=type(error).__name__):
                self.clock = 0
                def probe(pgid):
                    if self.clock < 65:
                        raise error
                    return False
                with patch.object(runtime, 'group_exists', side_effect=probe):
                    runtime._wait_for_group_end(10)
                self.assertGreaterEqual(self.clock, 65)
                self.assertLess(self.clock, 70)
                self.signals.assert_not_called()

    def test_empty_enumeration_cannot_override_existing_kernel_group(self):
        with patch.object(runtime, 'group_exists', side_effect=lambda pgid: self.clock < 65), \
             patch.object(runtime, 'group_members', return_value=[]) as members:
            runtime._wait_for_group_end(10, session=True)
        self.assertGreaterEqual(self.clock, 65)
        members.assert_called_once_with(10, session=True)

    def test_session_members_must_end_after_primary_group_is_absent(self):
        with patch.object(runtime, 'group_exists', return_value=False), \
             patch.object(runtime, 'group_members', side_effect=[[11], []]) as members:
            runtime._wait_for_group_end(10, session=True)
        self.assertEqual(members.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_other_observation_error_is_not_swallowed(self):
        with patch.object(runtime, 'group_exists', side_effect=JobsError('unrelated failure')):
            with self.assertRaisesRegex(JobsError, 'unrelated failure'):
                runtime._wait_for_group_end(10)
        self.sleep.assert_not_called()
