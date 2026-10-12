"""Opt-in session membership and fail-closed signaling contracts."""
import signal
import unittest
from unittest.mock import patch, Mock

from cmw.jobs.ownership import group_members, signal_session
from cmw.jobs.store import JobsError


class SessionOwnershipTests(unittest.TestCase):
    def test_zero_pid_is_not_current_process_membership(self):
        process = Mock()
        process.status.return_value = 'running'
        for session in (False, True):
            with self.subTest(session=session), patch('os.getsid', return_value=10) as sid, \
                 patch('os.getpgid', return_value=10) as pgid:
                self.assertEqual(group_members(10, pids=[0, 10], process_factory=lambda pid: process, session=session), [10])
                (sid if session else pgid).assert_called_once_with(10)

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


class ProtectedLeaderTests(unittest.TestCase):
    def test_subgroup_cleanup_never_targets_protected_leader_group(self):
        with patch('cmw.jobs.ownership.owner_alive', return_value=True), \
             patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid}), \
             patch('cmw.jobs.ownership.group_members', return_value=[10, 12, 13]), \
             patch('os.getsid', return_value=10), \
             patch('os.getpgid', side_effect=lambda pid: 10 if pid in (10, 13) else 11), \
             patch('os.killpg') as kill:
            signal_session({'pid': 10}, signal.SIGTERM, include_leader=False)
            self.assertEqual([call.args for call in kill.call_args_list], [(11, signal.SIGTERM)])

    def test_subgroup_cleanup_refuses_replaced_member_without_signal(self):
        with patch('cmw.jobs.ownership.owner_alive', side_effect=lambda owner: owner['pid'] == 10), \
             patch('cmw.jobs.ownership.identity', side_effect=lambda pid: {'pid': pid, 'birth': 1}), \
             patch('cmw.jobs.ownership.group_members', return_value=[12]), \
             patch('cmw.jobs.ownership.group_exists', return_value=True), \
             patch('os.getsid', return_value=10), \
             patch('os.getpgid', side_effect=lambda pid: 10 if pid == 10 else 11), \
             patch('os.killpg') as kill:
            with self.assertRaises(JobsError):
                signal_session({'pid': 10}, signal.SIGKILL, include_leader=False)
            kill.assert_not_called()


class SendTimeDisappearanceTests(unittest.TestCase):
    def observe(self, witness, probes, *, grace=.5, lose_anchor=False,
                send_error=None, include_leader=False):
        from contextlib import ExitStack
        import psutil
        calls, now, sent, lost, checks = [], [0.0], [False], [False], [0]
        owners = {pid: {'pid': pid, 'birth': 1, 'host': 'fixture', 'boot': 1}
                  for pid in (10, 12, 13)}
        def identify(pid):
            if sent[0] and pid == 12:
                checks[0] += 1
                if witness == 'missing':
                    raise psutil.NoSuchProcess(pid)
                if witness == 'lookup':
                    raise ProcessLookupError()
                if witness == 'unbound-zombie':
                    raise psutil.ZombieProcess(pid)
                if witness == 'identity-denied':
                    raise psutil.AccessDenied(pid)
                if witness == 'identity-permission':
                    raise PermissionError()
                if witness == 'replaced' or (witness == 'zombie-replaced' and checks[0] > 1):
                    return {**owners[pid], 'birth': 2}
            return owners[pid]
        process = Mock()
        if witness == 'status-denied':
            process.status.side_effect = psutil.AccessDenied(12)
        else:
            process.status.return_value = psutil.STATUS_ZOMBIE if witness.startswith('zombie') else 'running'
        def kill(pgid, signum):
            calls.append((pgid, signum))
            if signum:
                if pgid == 11:
                    sent[0] = True
                    raise send_error if send_error is not None else PermissionError(1, 'modeled denied send')
                return
            self.assertEqual(pgid, 11)
            count = sum(sig == 0 for _, sig in calls)
            observed = probes[min(count-1, len(probes)-1)]
            if lose_anchor:
                lost[0] = True
            if observed == 'absent':
                raise ProcessLookupError()
            if observed == 'denied':
                raise PermissionError(1, 'modeled observation denial')
        error = None
        with ExitStack() as stack:
            stack.enter_context(patch('cmw.jobs.ownership.owner_alive',
                                      side_effect=lambda item: not (item['pid'] == 10 and lost[0])))
            stack.enter_context(patch('cmw.jobs.ownership.identity', side_effect=identify))
            factory = stack.enter_context(patch('cmw.jobs.ownership.psutil.Process', return_value=process))
            membership = stack.enter_context(patch('cmw.jobs.ownership.group_members', return_value=[12, 13]))
            stack.enter_context(patch('os.getsid', return_value=10))
            stack.enter_context(patch('os.getpgid', side_effect=lambda pid: 10 if pid == 10 else 11))
            stack.enter_context(patch('os.killpg', side_effect=kill))
            stack.enter_context(patch('cmw.jobs.ownership.time.monotonic', side_effect=lambda: now[0]))
            stack.enter_context(patch('cmw.jobs.ownership.time.sleep', side_effect=lambda seconds: now.__setitem__(0, now[0]+seconds)))
            try:
                signal_session(owners[10], signal.SIGTERM, absence_grace=grace, include_leader=include_leader)
            except Exception as exc:
                error = exc
            membership.assert_called_once_with(10, session=True)
            inspected = factory.call_count
        return calls, now[0], error, inspected

    def test_strict_disappearance_allows_only_kernel_absence_observation(self):
        for witness in ('missing', 'lookup', 'zombie'):
            with self.subTest(witness=witness):
                calls, elapsed, error, _ = self.observe(witness, ['exists', 'denied', 'absent'])
                self.assertIsNone(error)
                self.assertEqual(calls, [(11, signal.SIGTERM), (11, 0), (11, 0), (11, 0)])
                self.assertLessEqual(elapsed, .5)

    def test_alive_inaccessible_or_replaced_witness_refuses_without_retry(self):
        for witness in ('live', 'identity-denied', 'identity-permission', 'status-denied',
                        'replaced', 'zombie-replaced', 'unbound-zombie'):
            with self.subTest(witness=witness):
                calls, elapsed, error, _ = self.observe(witness, ['absent'])
                self.assertIsInstance(error, PermissionError)
                self.assertEqual(calls, [(11, signal.SIGTERM)])
                self.assertEqual(elapsed, 0)

    def test_persistent_group_presence_or_denial_refuses_after_one_grace(self):
        for probes in (['exists'], ['denied'], ['exists', 'denied']):
            with self.subTest(probes=probes):
                calls, elapsed, error, _ = self.observe('missing', probes)
                self.assertIsInstance(error, JobsError)
                self.assertEqual([item for item in calls if item[1]], [(11, signal.SIGTERM)])
                self.assertEqual(elapsed, .5)

    def test_anchor_loss_during_absence_wait_prevents_further_probes(self):
        calls, _, error, _ = self.observe('missing', ['exists', 'absent'], lose_anchor=True)
        self.assertIsInstance(error, JobsError)
        self.assertIn('ownership lost', str(error))
        self.assertEqual(calls, [(11, signal.SIGTERM), (11, 0)])

    def test_legacy_zero_grace_preserves_original_send_failure(self):
        original = PermissionError(1, 'original legacy denial')
        calls, elapsed, error, inspected = self.observe('missing', ['absent'], grace=0, send_error=original)
        self.assertIs(error, original)
        self.assertEqual(calls, [(11, signal.SIGTERM)])
        self.assertEqual((elapsed, inspected), (0, 0))

    def test_other_send_error_is_not_reclassified_or_retried(self):
        for original in (OSError(5, 'unrelated I/O error'), PermissionError(13, 'different permission error')):
            with self.subTest(error=original):
                calls, elapsed, error, inspected = self.observe('missing', ['absent'], send_error=original)
                self.assertIs(error, original)
                self.assertEqual(calls, [(11, signal.SIGTERM)])
                self.assertEqual((elapsed, inspected), (0, 0))

    def test_proven_absence_may_advance_to_original_pinned_group(self):
        calls, _, error, _ = self.observe('zombie', ['absent'], include_leader=True)
        self.assertIsNone(error)
        self.assertEqual(calls, [(11, signal.SIGTERM), (11, 0), (10, signal.SIGTERM)])
