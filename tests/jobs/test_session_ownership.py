"""Opt-in session membership and fail-closed signaling contracts."""
import signal
import unittest
from unittest.mock import patch, Mock

from cmw.jobs.ownership import group_members, signal_session
from cmw.jobs.store import JobsError


class SessionOwnershipTests(unittest.TestCase):
    def test_rank_subgroup_is_included_only_when_opted_in(self):
        process = Mock()
        process.status.return_value = 'running'
        with patch('os.getpgid', side_effect=lambda pid: {10: 10, 11: 11, 12: 12}[pid]), \
             patch('os.getsid', side_effect=lambda pid: {10: 10, 11: 10, 12: 12}[pid]):
            self.assertEqual(group_members(10, pids=[10, 11, 12], process_factory=lambda pid: process), [10])
            self.assertEqual(group_members(10, pids=[10, 11, 12], process_factory=lambda pid: process, session=True), [10, 11])

    def test_unverified_owner_never_signals(self):
        with patch('cmw.jobs.ownership.owner_alive', return_value=False), patch('os.killpg') as kill:
            with self.assertRaises(JobsError):
                signal_session({'pid': 10}, signal.SIGTERM)
            kill.assert_not_called()

    def test_subgroups_signalled_before_pinned_leader(self):
        with patch('cmw.jobs.ownership.owner_alive', return_value=True), \
             patch('cmw.jobs.ownership.group_members', return_value=[10, 11]), \
             patch('os.getsid', return_value=10), patch('os.getpgid', side_effect=lambda pid: pid), \
             patch('os.killpg') as kill:
            signal_session({'pid': 10}, signal.SIGTERM)
            self.assertEqual([call.args for call in kill.call_args_list], [(11, signal.SIGTERM), (10, signal.SIGTERM)])
