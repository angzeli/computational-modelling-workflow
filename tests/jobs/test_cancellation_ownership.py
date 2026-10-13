"""Deterministic cancellation races against an identity-aware process model.

No process is launched or signalled. Metadata and syscall injection drive the
actual ownership implementation; every nonzero signal must match the current
owned domain and must exclude protected controls and an unrelated sentinel.
"""
from contextlib import ExitStack
import errno
import signal
import unittest
from unittest.mock import patch

import psutil

from cmw.jobs import ownership
from cmw.jobs.store import JobsError


class ProcessTable:
    """Separate readable identity, topology and kernel group visibility."""

    def __init__(self, testcase):
        self.testcase = testcase
        self.clock = 0.0
        self.events = []
        self.rows = {}
        for pid, pgid, sid in ((10, 10, 10), (13, 10, 10), (14, 10, 10),
                               (11, 11, 10), (20, 20, 10), (21, 21, 10),
                               (90, 90, 90)):
            self.rows[pid] = dict(pid=pid, birth=float(pid), pgid=pgid, sid=sid,
                                  status=psutil.STATUS_RUNNING, present=True,
                                  sid_missing=False, inaccessible=False, ppid=10)
        self.anchor = self.record(10)
        self.launcher_term = None
        self.denied_rank = None
        self.after_denial = None
        self.zero_results = []

    def record(self, pid):
        row = self.rows[pid]
        return dict(pid=pid, birth=row['birth'], host='fixture-host', boot=1.0)

    def row(self, pid):
        row = self.rows.get(pid)
        if row is None or not row['present']:
            raise psutil.NoSuchProcess(pid)
        return row

    def process(self, pid):
        table = self
        table.row(pid)

        class Process:
            def __init__(self):
                self.pid = pid

            def create_time(self):
                table.events.append(('birth', pid))
                row = table.row(pid)
                if row['inaccessible']:
                    raise psutil.AccessDenied(pid)
                return row['birth']

            def status(self):
                table.events.append(('status', pid))
                row = table.row(pid)
                if row['inaccessible']:
                    raise psutil.AccessDenied(pid)
                return row['status']

            def ppid(self):
                table.events.append(('ppid', pid))
                return table.row(pid)['ppid']

        return Process()

    def sid(self, pid):
        self.events.append(('sid', pid))
        try:
            row = self.row(pid)
        except psutil.NoSuchProcess as exc:
            raise ProcessLookupError(errno.ESRCH, 'reaped fixture process') from exc
        if row['sid_missing']:
            raise ProcessLookupError(errno.ESRCH, 'identity readable, topology absent')
        return row['sid']

    def pgid(self, pid):
        self.events.append(('pgid', pid))
        try:
            return self.row(pid)['pgid']
        except psutil.NoSuchProcess as exc:
            raise ProcessLookupError(errno.ESRCH, 'reaped fixture process') from exc

    def killpg(self, pgid, signum):
        self.events.append(('signal', pgid, signum))
        members = [row for row in self.rows.values()
                   if row['present'] and row['pgid'] == pgid]
        if signum == 0:
            if self.zero_results:
                outcome = self.zero_results.pop(0)
                if outcome == 'denied':
                    raise PermissionError(errno.EPERM, 'denied fixture null probe')
                if outcome == 'absent':
                    raise ProcessLookupError(errno.ESRCH, 'fixture group absent')
                return
            if not members:
                raise ProcessLookupError(errno.ESRCH, 'fixture group absent')
            return
        self.testcase.assertNotEqual(pgid, 10, 'protected controls were signalled')
        self.testcase.assertTrue(members, 'numeric group signalled without a live witness')
        self.testcase.assertTrue(all(row['sid'] == 10 for row in members),
                                 'unrelated sentinel was signalled')
        self.testcase.assertEqual(self.rows[10]['birth'], self.anchor['birth'])
        self.testcase.assertTrue(self.rows[10]['present'])
        if pgid == 11 and signum == signal.SIGTERM and self.launcher_term:
            self.launcher_term()
        if pgid == self.denied_rank:
            if self.after_denial:
                self.after_denial()
            raise PermissionError(errno.EPERM, 'denied fixture nonzero signal')

    def remove(self, *pids):
        for pid in pids:
            self.rows[pid]['present'] = False

    def sleep(self, seconds):
        self.clock += seconds

    def install(self, stack):
        stack.enter_context(patch.object(ownership.psutil, 'Process', side_effect=self.process))
        stack.enter_context(patch.object(ownership.psutil, 'pids', side_effect=lambda: list(self.rows)))
        stack.enter_context(patch.object(ownership.psutil, 'boot_time', return_value=1.0))
        stack.enter_context(patch.object(ownership.socket, 'gethostname', return_value='fixture-host'))
        stack.enter_context(patch.object(ownership.os, 'getsid', side_effect=self.sid))
        stack.enter_context(patch.object(ownership.os, 'getpgid', side_effect=self.pgid))
        stack.enter_context(patch.object(ownership.os, 'killpg', side_effect=self.killpg))
        stack.enter_context(patch.object(ownership.time, 'monotonic', side_effect=lambda: self.clock))
        stack.enter_context(patch.object(ownership.time, 'sleep', side_effect=self.sleep))
        # The real helper has a bound Process default. Pass the injected factory
        # while retaining its real enumeration and access-denial semantics.
        original_members = ownership.group_members
        stack.enter_context(patch.object(ownership, 'group_members',
                                         side_effect=lambda pgid, **kwargs: original_members(
                                             pgid, process_factory=self.process, **kwargs)))

    def signals(self):
        return [event[1:] for event in self.events if event[0] == 'signal']


class CancellationOwnershipRaceTests(unittest.TestCase):
    def setUp(self):
        self.table = ProcessTable(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.table.install(self.stack)

    def traverse(self):
        ownership.signal_session(self.table.anchor, signal.SIGTERM,
                                 include_leader=False, absence_grace=.5)

    def assert_protected(self):
        self.assertTrue(all(pgid not in {10, 90} for pgid, sig in self.table.signals() if sig))
        for pid in (10, 13, 14, 90):
            self.assertTrue(self.table.rows[pid]['present'])

    def test_launcher_term_reaps_rank_before_explicit_rank_term(self):
        self.table.launcher_term = lambda: self.table.remove(20)
        self.traverse()
        self.assertEqual(self.table.signals(), [(11, signal.SIGTERM), (20, 0), (21, signal.SIGTERM)])
        self.assert_protected()

    def test_denied_rank_term_observes_absence_without_nonzero_retry(self):
        self.table.denied_rank = 20
        self.table.after_denial = lambda: self.table.remove(20)
        self.table.zero_results = ['denied', 'absent']
        self.traverse()
        self.assertEqual(self.table.signals(), [(11, signal.SIGTERM), (20, signal.SIGTERM),
                                                (20, 0), (20, 0), (21, signal.SIGTERM)])
        self.assertIn(('birth', 20), self.table.events)
        self.assert_protected()

    def test_live_stopped_or_exiting_non_zombie_denial_remains_refusal(self):
        for status in (psutil.STATUS_RUNNING, psutil.STATUS_STOPPED, psutil.STATUS_IDLE):
            with self.subTest(status=status):
                self.table.events.clear()
                self.table.rows[20]['status'] = status
                self.table.denied_rank = 20
                with self.assertRaises(PermissionError):
                    self.traverse()
                self.assertEqual(self.table.signals(), [(11, signal.SIGTERM), (20, signal.SIGTERM)])
                self.assert_protected()

    def test_birth_verified_zombie_requires_independent_kernel_absence(self):
        self.table.denied_rank = 20
        self.table.after_denial = lambda: self.table.rows[20].update(status=psutil.STATUS_ZOMBIE)
        self.table.zero_results = ['denied', 'absent']
        self.traverse()
        self.assertEqual([item for item in self.table.signals() if item[0] == 20],
                         [(20, signal.SIGTERM), (20, 0), (20, 0)])
        self.assert_protected()

    def test_readable_birth_with_sid_esrch_still_requires_group_absence(self):
        self.table.launcher_term = lambda: self.table.rows[20].update(sid_missing=True)
        self.table.zero_results = ['denied', 'absent']
        self.traverse()
        self.assertEqual([item for item in self.table.signals() if item[0] == 20], [(20, 0), (20, 0)])
        self.assertIn(('birth', 20), self.table.events)
        self.assert_protected()

    def test_readable_birth_with_sid_esrch_and_visible_group_refuses(self):
        self.table.launcher_term = lambda: self.table.rows[20].update(sid_missing=True)
        with self.assertRaisesRegex(JobsError, 'no verified surviving member'):
            self.traverse()
        self.assertEqual(self.table.clock, .5)
        self.assertTrue(all(sig == 0 for pgid, sig in self.table.signals() if pgid == 20))
        self.assert_protected()

    def test_reused_rank_pid_cannot_authorize_signal_to_foreign_group(self):
        self.table.launcher_term = lambda: self.table.rows[20].update(birth=999.0, sid=90)
        with self.assertRaises(JobsError):
            self.traverse()
        self.assertEqual([item for item in self.table.signals() if item[1]], [(11, signal.SIGTERM)])
        self.assert_protected()

    def test_group_change_after_enumeration_refuses_before_signal(self):
        self.table.launcher_term = lambda: self.table.rows[20].update(pgid=90)
        with self.assertRaisesRegex(JobsError, 'identity cannot be verified'):
            self.traverse()
        self.assertEqual(self.table.signals(), [(11, signal.SIGTERM)])
        self.assert_protected()

    def test_inaccessible_original_witness_after_denial_is_not_exit(self):
        self.table.denied_rank = 20
        self.table.after_denial = lambda: self.table.rows[20].update(inaccessible=True)
        with self.assertRaises(PermissionError):
            self.traverse()
        self.assertEqual(self.table.signals(), [(11, signal.SIGTERM), (20, signal.SIGTERM)])
        self.assert_protected()

    def test_anchor_sid_loss_after_launcher_term_stops_all_remaining_signals(self):
        self.table.launcher_term = lambda: self.table.rows[10].update(sid_missing=True)
        with self.assertRaises(ProcessLookupError):
            self.traverse()
        self.assertEqual(self.table.signals(), [(11, signal.SIGTERM)])
        self.assert_protected()


class SessionSnapshotRaceTests(unittest.TestCase):
    def setUp(self):
        self.table = ProcessTable(self)
        self.originals = [self.table.record(pid) for pid in (11, 20, 21)]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.table.install(self.stack)

    def snapshot(self):
        result = ownership.snapshot_owned_session(self.table.anchor, self.originals)
        self.assertEqual(self.table.signals(), [])
        return result

    def test_zombies_remain_current_members_and_foreign_sentinel_is_not_inspected(self):
        self.table.rows[20]['status'] = psutil.STATUS_ZOMBIE
        result = self.snapshot()
        members = {row['identity']['pid']: row for row in result['members']}
        self.assertEqual(set(members), {10, 11, 13, 14, 20, 21})
        self.assertEqual(members[20]['status'], psutil.STATUS_ZOMBIE)
        self.assertEqual(members[20]['ppid'], 10)
        self.assertEqual(result['absent'], [])
        self.assertEqual(result['pending'], [])
        self.assertTrue(all(event[0] == 'sid' for event in self.table.events if event[1] == 90))

    def test_known_process_requires_strict_independent_absence(self):
        self.table.remove(20)
        result = self.snapshot()
        self.assertEqual(result['absent'], [self.originals[1]])
        self.assertEqual(result['pending'], [])

    def test_readable_birth_with_sid_esrch_is_pending_not_absent(self):
        self.table.rows[20]['sid_missing'] = True
        result = self.snapshot()
        self.assertEqual([row['identity'] for row in result['pending']], [self.originals[1]])
        self.assertEqual(result['absent'], [])

    def test_readable_birth_with_pgid_esrch_is_pending_not_absent(self):
        def pgid(pid):
            if pid == 20:
                raise ProcessLookupError(errno.ESRCH, 'transient pgid loss')
            return self.table.pgid(pid)
        with patch.object(ownership.os, 'getpgid', side_effect=pgid):
            result = self.snapshot()
        self.assertEqual([row['identity'] for row in result['pending']], [self.originals[1]])
        self.assertEqual(result['absent'], [])

    def test_raw_esrch_identity_query_is_pending_without_independent_no_such_process(self):
        original_identity = ownership.identity
        def identify(pid):
            if pid == 20:
                raise ProcessLookupError(errno.ESRCH, 'raw identity race')
            return original_identity(pid)
        with patch.object(ownership, 'identity', side_effect=identify):
            result = self.snapshot()
        self.assertEqual([row['identity'] for row in result['pending']], [self.originals[1]])
        self.assertEqual(result['absent'], [])

    def test_unreadable_original_zombie_is_pending_not_no_such_process(self):
        original_identity = ownership.identity
        def identify(pid):
            if pid == 20:
                raise psutil.ZombieProcess(pid)
            return original_identity(pid)
        with patch.object(ownership, 'identity', side_effect=identify):
            result = self.snapshot()
        self.assertEqual([row['identity'] for row in result['pending']], [self.originals[1]])
        self.assertEqual(result['absent'], [])

    def test_reused_known_identity_and_escape_cannot_be_adopted(self):
        for change in ({'birth': 999.0}, {'sid': 90}):
            with self.subTest(change=change):
                row = self.table.rows[20]
                row.update(birth=20.0, sid=10)
                row.update(change)
                with self.assertRaises(JobsError):
                    self.snapshot()
                self.assertEqual(self.table.signals(), [])

    def test_known_access_denial_is_not_absence(self):
        self.table.rows[20]['inaccessible'] = True
        with self.assertRaisesRegex(JobsError, 'not fully observable'):
            self.snapshot()
        self.assertEqual(self.table.signals(), [])

    def test_anchor_must_remain_runnable_and_exact(self):
        for change in ({'status': psutil.STATUS_STOPPED}, {'status': psutil.STATUS_ZOMBIE},
                       {'status': psutil.STATUS_TRACING_STOP}, {'birth': 999.0},
                       {'sid_missing': True}):
            with self.subTest(change=change):
                self.table.rows[10].update(status=psutil.STATUS_RUNNING, birth=10.0, sid_missing=False)
                self.table.rows[10].update(change)
                with self.assertRaisesRegex(JobsError, 'authority is unavailable'):
                    self.snapshot()
                self.assertEqual(self.table.signals(), [])

    def test_anchor_loss_at_end_of_scan_refuses_an_otherwise_valid_snapshot(self):
        def sid(pid):
            value = self.table.sid(pid)
            if pid == 90:
                self.table.rows[10]['sid_missing'] = True
            return value
        with patch.object(ownership.os, 'getsid', side_effect=sid), \
                self.assertRaisesRegex(JobsError, 'authority is unavailable'):
            self.snapshot()
        self.assertEqual(self.table.signals(), [])

    def test_late_owned_member_is_current_evidence_and_cannot_be_missed_by_known_list(self):
        self.table.rows[31] = {**self.table.rows[20], 'pid': 31, 'birth': 31.0, 'pgid': 31, 'ppid': 20}
        result = self.snapshot()
        row = next(row for row in result['members'] if row['identity']['pid'] == 31)
        self.assertEqual((row['pgid'], row['ppid']), (31, 20))
        self.assertNotIn(row['identity'], self.originals)

    def test_reaping_after_initial_birth_read_requires_independent_no_such_process(self):
        def sid(pid):
            if pid == 20:
                self.table.remove(pid)
                raise ProcessLookupError(errno.ESRCH, 'reaped during validation')
            return self.table.sid(pid)
        with patch.object(ownership.os, 'getsid', side_effect=sid):
            result = self.snapshot()
        self.assertEqual(result['absent'], [self.originals[1]])
        self.assertEqual(result['pending'], [])

    def test_identity_change_during_scan_and_conflicting_originals_refuse(self):
        original_identity = ownership.identity
        reads = [0]
        def identify(pid):
            value = original_identity(pid)
            if pid == 20:
                reads[0] += 1
                if reads[0] == 2:
                    value['birth'] = 999.0
            return value
        with patch.object(ownership, 'identity', side_effect=identify), \
                self.assertRaisesRegex(JobsError, 'identity changed'):
            self.snapshot()
        self.originals.append({**self.originals[1], 'birth': 999.0})
        with self.assertRaisesRegex(JobsError, 'Conflicting'):
            self.snapshot()
        self.assertEqual(self.table.signals(), [])
