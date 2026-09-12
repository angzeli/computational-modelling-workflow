"""Opt-in session membership and fail-closed signaling contracts."""
import signal
import unittest
from unittest.mock import patch, Mock

from cmw.jobs.ownership import group_members, signal_session
from cmw.jobs.store import JobsError


class SessionOwnershipTests(unittest.TestCase):
    def test_leaderless_subgroup_uses_surviving_member(self):
        def sid(pid):
            if pid == 11:
                raise ProcessLookupError()
            return 10
        with patch('cmw.jobs.ownership.owner_alive', return_value=True), \
             patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid}), \
             patch('cmw.jobs.ownership.group_members', return_value=[10, 12]), \
             patch('os.getsid', side_effect=sid), \
             patch('os.getpgid', side_effect=lambda pid: 10 if pid == 10 else 11), \
             patch('os.killpg') as kill:
            signal_session({'pid': 10}, signal.SIGTERM)
            self.assertEqual([call.args for call in kill.call_args_list], [(11, signal.SIGTERM), (10, signal.SIGTERM)])

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
             patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid}), \
             patch('cmw.jobs.ownership.group_members', return_value=[10, 11]), \
             patch('os.getsid', return_value=10), patch('os.getpgid', side_effect=lambda pid: pid), \
             patch('os.killpg') as kill:
            signal_session({'pid': 10}, signal.SIGTERM)
            self.assertEqual([call.args for call in kill.call_args_list], [(11, signal.SIGTERM), (10, signal.SIGTERM)])

    def test_changed_member_birth_never_authorizes_numeric_group(self):
        with patch('cmw.jobs.ownership.owner_alive', side_effect=lambda owner: owner['pid'] == 10), \
             patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid, 'birth': 1}), \
             patch('cmw.jobs.ownership.group_members', return_value=[12]), \
             patch('cmw.jobs.ownership.group_exists', return_value=True), \
             patch('os.getsid', return_value=10), \
             patch('os.getpgid', side_effect=lambda pid: 10 if pid == 10 else 11), \
             patch('os.killpg') as kill:
            with self.assertRaisesRegex(JobsError, 'no verified surviving member'):
                signal_session({'pid': 10}, signal.SIGKILL)
            kill.assert_not_called()

    def test_topology_change_or_unrelated_session_never_signalled(self):
        for moved in ('session', 'group'):
            with self.subTest(moved=moved):
                observations = {'member': 0}
                def sid(pid):
                    if pid == 12:
                        observations['member'] += 1
                        if moved == 'session' and observations['member'] > 1:
                            return 99
                    return 10
                def pgid(pid):
                    if pid == 10:
                        return 10
                    return 99 if moved == 'group' and observations['member'] else 11
                with patch('cmw.jobs.ownership.owner_alive', return_value=True), \
                     patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid}), \
                     patch('cmw.jobs.ownership.group_members', return_value=[12]), \
                     patch('os.getsid', side_effect=sid), patch('os.getpgid', side_effect=pgid), \
                     patch('os.killpg') as kill:
                    with self.assertRaisesRegex(JobsError, 'identity cannot be verified'):
                        signal_session({'pid': 10}, signal.SIGTERM)
                    kill.assert_not_called()

    def test_vanished_witness_uses_another_verified_member(self):
        with patch('cmw.jobs.ownership.owner_alive', side_effect=lambda owner: owner['pid'] != 12), \
             patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid}), \
             patch('cmw.jobs.ownership.group_members', return_value=[12, 13]), \
             patch('os.getsid', return_value=10), \
             patch('os.getpgid', side_effect=lambda pid: 10 if pid == 10 else 11), \
             patch('os.killpg') as kill:
            signal_session({'pid': 10}, signal.SIGTERM)
            self.assertEqual([call.args for call in kill.call_args_list], [(11, signal.SIGTERM), (10, signal.SIGTERM)])

    def test_pinned_owner_loss_during_witness_validation_prevents_signal(self):
        checks = 0
        def alive(owner):
            nonlocal checks
            if owner['pid'] == 10:
                checks += 1
                return checks < 3
            return True
        with patch('cmw.jobs.ownership.owner_alive', side_effect=alive), \
             patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid}), \
             patch('cmw.jobs.ownership.group_members', return_value=[12]), \
             patch('os.getsid', return_value=10), \
             patch('os.getpgid', side_effect=lambda pid: 10 if pid == 10 else 11), \
             patch('os.killpg') as kill:
            with self.assertRaisesRegex(JobsError, 'ownership lost'):
                signal_session({'pid': 10}, signal.SIGKILL)
            kill.assert_not_called()
