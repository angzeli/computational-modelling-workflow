"""Memory semantics and coverage use injected counters, never host enumeration."""
import ctypes
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import telemetry, ownership


def owner(pid, birth=1):
    return dict(pid=pid, birth=birth, host='fixture', boot=1)


class MemoryTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.clock = 10.
        self.processes = {}
        self.identities = {}
        self.members = []
        for pid in range(1, 12):
            process = Mock()
            process.status.return_value = psutil.STATUS_RUNNING
            process.cpu_times.return_value = SimpleNamespace(user=10., system=0.)
            process.memory_info.return_value = SimpleNamespace(rss=1024**3)
            process.ppid.return_value = 2 if pid >= 4 else 1
            process._proc.exe.return_value = ('/vasp/vasp_std' if pid >= 4 else
                                              '/mpi/prterun' if pid == 2 else '/bin/bash')
            self.processes[pid] = process
            self.identities[pid] = owner(pid)
            self.members.append({'identity': owner(pid)})
        self.job = dict(attempt_id='a', status='Run', engine='VASP',
                        group=owner(1), worker=owner(99),
                        resources={'mpi_ranks': 8}, env={'CMW_JOBS_OWN_SESSION': '1'})
        self.state = {'jobs': [self.job]}
        self.guard = {'observations': [], 'stale': False}
        self.memory = SimpleNamespace(total=36*1024**3, available=9*1024**3,
                                      used=12*1024**3, active=8*1024**3,
                                      inactive=7*1024**3, wired=4*1024**3, free=2*1024**3)
        self.footprint = Mock(return_value=2*1024**3)
        self.swap = Mock(return_value=SimpleNamespace(total=8*1024**3, used=3*1024**3, sout=1234))
        self.sampler = telemetry.Sampler(
            process_factory=self.factory, identity_provider=lambda pid: self.identities[pid],
            virtual_memory=lambda: self.memory, cpu_times=lambda: SimpleNamespace(_asdict=lambda: {'idle': 1}),
            footprint=self.footprint, swap_memory=self.swap,
            monotonic=lambda: self.clock, wall_time=lambda: self.clock+100)
        self.membership = patch.object(telemetry, 'observed_group_members', side_effect=lambda *a, **kw: (self.members, ''))
        self.membership.start()
        self.addCleanup(self.membership.stop)
        self.host_scan = patch.object(psutil, 'pids', side_effect=AssertionError('No host scan'))
        self.host_scan.start()
        self.addCleanup(self.host_scan.stop)

    def factory(self, pid):
        if pid not in self.processes:
            raise psutil.NoSuchProcess(pid)
        return self.processes[pid]

    def sample(self):
        return self.sampler.sample(Mock(root=Path('/isolated')), self.state, self.guard, list(self.processes))

    def test_eight_ranks_session_scope_sum_and_same_cpu_members(self):
        first = self.sample()['jobs']['a']
        self.assertEqual(first['rss_bytes'], 11*1024**3)
        self.assertEqual(first['footprint_bytes'], 22*1024**3)
        self.assertEqual((first['expected_vasp_ranks'], first['observed_vasp_ranks']), (8, 8))
        self.assertEqual(first['coverage_quality'], 'complete')
        self.assertEqual(first['scope'], 'verified payload session')
        self.assertEqual((first['memory_members'], first['cpu_sample_members'], first['total_members']), (11, 11, 11))
        self.assertIn('shared', first['memory_semantics'])
        self.assertEqual(len(first['process_samples']), 11)
        self.assertEqual(sum(p['role'] == 'VASP rank' for p in first['process_samples']), 8)
        self.clock += 2
        for process in self.processes.values():
            process.cpu_times.return_value.user += 2
        second = self.sample()['jobs']['a']
        self.assertEqual(second['cpu_cores'], 11)
        self.assertEqual(second['cpu_members'], second['memory_members'])
        self.assertEqual(self.footprint.call_count, 22)

    def test_owned_session_includes_mpi_subgroups_but_not_outside_worker(self):
        self.membership.stop()
        try:
            with patch.object(ownership, 'owner_alive', return_value=True), \
                 patch.object(ownership, 'lock_held', return_value=True), \
                 patch.object(ownership.os, 'getsid', side_effect=lambda pid: 99 if pid == 99 else 1), \
                 patch.object(ownership.os, 'getpgid', side_effect=lambda pid: pid):
                members, reason = ownership.observed_group_members(
                    Mock(root=Path('/isolated')), self.job, [*self.processes, 99],
                    process_factory=self.factory, identity_provider=lambda pid: self.identities[pid])
                self.assertEqual(reason, '')
                self.assertEqual({m['identity']['pid'] for m in members}, set(range(1, 12)))
                self.assertEqual({m['pgid'] for m in members}, set(range(1, 12)))
                usage = self.sampler._workload(members, {}, 20, job=self.job)
                self.assertEqual((usage['observed_vasp_ranks'], usage['memory_members']), (8, 11))
        finally:
            self.membership.start()

    def test_valid_zero_is_measured_but_invalid_cpu_is_not(self):
        self.processes[4].memory_info.return_value.rss = 0
        self.processes[4].cpu_times.return_value.user = float('nan')
        usage = self.sample()['jobs']['a']
        self.assertEqual(usage['rss_bytes'], 10*1024**3)
        self.assertEqual(usage['memory_members'], 11)
        self.assertEqual(usage['cpu_sample_members'], 10)
        self.assertEqual(usage['memory_quality'], 'fresh')

    def test_duplicate_identity_is_counted_once(self):
        self.members += [self.members[0], self.members[3]]
        usage = self.sample()['jobs']['a']
        self.assertEqual((usage['total_members'], usage['rss_bytes']), (11, 11*1024**3))
        self.assertEqual(self.footprint.call_count, 11)

    def test_pid_reuse_rejects_rss_and_footprint(self):
        self.identities[4] = owner(4, 2)
        usage = self.sample()['jobs']['a']
        self.assertEqual(usage['memory_members'], 10)
        self.assertEqual(usage['observed_vasp_ranks'], 7)
        self.assertEqual(usage['coverage_quality'], 'partial')
        self.assertNotIn(4, [p['identity']['pid'] for p in usage['process_samples']])

    def test_disappearing_rank_does_not_shrink_coverage_denominator(self):
        del self.processes[4]
        usage = self.sample()['jobs']['a']
        self.assertEqual((usage['observed_members'], usage['total_members']), (10, 11))
        self.assertEqual(usage['memory_quality'], 'partial')
        self.assertEqual(usage['coverage_quality'], 'partial')

    def test_expected_missing_rank_marks_otherwise_measured_group_partial(self):
        self.members = self.members[:-1]
        usage = self.sample()['jobs']['a']
        self.assertEqual((usage['observed_vasp_ranks'], usage['expected_vasp_ranks']), (7, 8))
        self.assertEqual(usage['memory_quality'], 'partial')
        self.assertIn('7/8', usage['coverage_reason'])

    def test_rank_metadata_unavailable_is_not_zero(self):
        for process in self.processes.values():
            process._proc.exe.side_effect = psutil.AccessDenied()
        usage = self.sample()['jobs']['a']
        self.assertIsNone(usage['observed_vasp_ranks'])
        self.assertEqual(usage['memory_members'], 11)
        self.assertEqual(usage['coverage_quality'], 'partial')

    def test_expected_ranks_never_inferred_from_cpu_slots(self):
        self.job['resources'] = {'cpus': 8, 'mpi_ranks': None}
        self.assertIsNone(self.sample()['jobs']['a']['expected_vasp_ranks'])

    def test_denied_metrics_preserve_separate_subtotals(self):
        self.processes[4].memory_info.side_effect = psutil.AccessDenied(4)
        self.footprint.side_effect = lambda pid: (_ for _ in ()).throw(PermissionError()) if pid == 5 else 2*1024**3
        usage = self.sample()['jobs']['a']
        self.assertEqual(usage['rss_bytes'], 10*1024**3)
        self.assertEqual(usage['footprint_bytes'], 20*1024**3)
        self.assertEqual((usage['memory_quality'], usage['footprint_quality']), ('partial', 'partial'))
        self.assertEqual(usage['cpu_sample_members'], 11)

    def test_missing_negative_and_nonfinite_rss_never_become_zero(self):
        for invalid in (None, -1, float('nan'), float('inf')):
            with self.subTest(invalid=invalid):
                self.processes[4].memory_info.return_value = SimpleNamespace(rss=invalid)
                result = self.sample()
                usage = result['jobs']['a']
                self.assertEqual(usage['rss_bytes'], 10*1024**3)
                self.assertEqual(usage['memory_quality'], 'partial')
                self.assertEqual(result['machine_usage']['memory_quality'], 'fresh')
        self.processes[4].memory_info.return_value = SimpleNamespace()
        self.assertEqual(self.sample()['jobs']['a']['memory_members'], 10)

    def test_missing_footprint_support_preserves_rss(self):
        self.sampler.footprint = None
        usage = self.sample()['jobs']['a']
        self.assertIsNone(usage['footprint_bytes'])
        self.assertEqual(usage['footprint_quality'], 'unavailable')
        self.assertEqual(usage['rss_bytes'], 11*1024**3)
        self.assertIn('unavailable', usage['footprint_reason'])

    def test_machine_categories_do_not_add_swap_or_logical_compression(self):
        self.memory.compressed = 13*1024**3
        machine = self.sample()['machine_usage']
        self.assertEqual(machine['ram_used_bytes'], 27*1024**3)
        self.assertEqual(machine['ram_available_bytes'], 9*1024**3)
        self.assertEqual(machine['memory_categories_bytes']['compressed'], 13*1024**3)
        self.assertEqual(machine['memory_categories_bytes']['used'], 12*1024**3)
        self.assertEqual(machine['swap_used_bytes'], 3*1024**3)
        self.assertEqual(machine['swap_out_bytes'], 1234)
        self.assertEqual(machine['memory_source'], 'psutil.virtual_memory')
        self.assertEqual(self.swap.call_count, 1)

    def test_unreported_compression_remains_unavailable(self):
        self.assertIsNone(self.sample()['machine_usage']['memory_categories_bytes']['compressed'])

    def test_invalid_machine_counters_are_unavailable_without_affecting_job(self):
        for total, available in ((-1, 0), (100, 101), (100, -1), (float('nan'), 0)):
            self.memory.total, self.memory.available = total, available
            result = self.sample()
            self.assertEqual(result['machine_usage']['memory_quality'], 'unavailable')
            self.assertIsNone(result['machine_usage']['ram_used_bytes'])
            self.assertEqual(result['jobs']['a']['memory_quality'], 'fresh')

    def test_stale_projection_and_completion_do_not_claim_current_rss(self):
        first = self.sample()['jobs']['a']
        aged = telemetry.age_usage(first, now=117)
        self.assertTrue(aged['stale'])
        self.assertEqual(aged['age_seconds'], 7)
        self.job['status'] = 'Done'
        self.assertNotIn('a', self.sample()['jobs'])
        self.assertEqual(first['rss_bytes'], 11*1024**3)

    def test_identity_change_during_optional_read_rejects_all_metrics(self):
        def footprint(pid):
            if pid == 4:
                self.identities[pid] = owner(pid, 2)
            return 99
        self.sampler.footprint = footprint
        usage = self.sample()['jobs']['a']
        self.assertEqual((usage['memory_members'], usage['footprint_members'], usage['cpu_sample_members']), (10, 10, 10))


class NativeFootprintTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'macOS libproc only')
    def test_native_reader_samples_only_this_test_process(self):
        from cmw.jobs.native_memory import footprint_reader
        read = footprint_reader()
        self.assertIsNotNone(read)
        self.assertGreater(read(os.getpid()), 0)

    def test_native_v0_layout_and_bytes(self):
        from cmw.jobs.native_memory import RUsageInfoV0, footprint_reader
        self.assertEqual(ctypes.sizeof(RUsageInfoV0), 96)
        self.assertEqual(RUsageInfoV0.ri_phys_footprint.offset, 72)
        def rusage(pid, flavor, buffer):
            self.assertEqual((pid, flavor), (42, 0))
            ctypes.cast(buffer, ctypes.POINTER(RUsageInfoV0)).contents.ri_phys_footprint = 123456789
            return 0
        library = SimpleNamespace(proc_pid_rusage=Mock(side_effect=rusage))
        with patch('cmw.jobs.native_memory.ctypes.CDLL', return_value=library):
            read = footprint_reader(platform='darwin')
        self.assertEqual(read(42), 123456789)
        self.assertEqual(library.proc_pid_rusage.call_count, 1)

    def test_other_platform_and_missing_library_are_unavailable(self):
        from cmw.jobs.native_memory import footprint_reader
        with patch('cmw.jobs.native_memory.ctypes.CDLL', side_effect=AssertionError('No library load')):
            self.assertIsNone(footprint_reader(platform='linux'))
        with patch('cmw.jobs.native_memory.ctypes.CDLL', side_effect=OSError('missing')):
            self.assertIsNone(footprint_reader(platform='darwin'))

    def test_native_denial_is_not_zero(self):
        from cmw.jobs.native_memory import footprint_reader
        library = SimpleNamespace(proc_pid_rusage=Mock(return_value=-1))
        with patch('cmw.jobs.native_memory.ctypes.CDLL', return_value=library):
            read = footprint_reader(platform='darwin')
        with patch('cmw.jobs.native_memory.ctypes.get_errno', return_value=1):
            with self.assertRaises(PermissionError):
                read(42)
