"""Nested MPI family identity fixtures; no real process-table or engine access."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

import psutil

from cmw.jobs import activity, sharing, telemetry
from tests.jobs import test_activity as activity_fixtures
from tests.jobs import test_sharing_policy as sharing_fixtures
from tests.jobs.test_activity import owner, process
from tests.jobs.test_telemetry import CPU


class MpiFamilyTests(unittest.TestCase):
    collect = activity_fixtures.ActivityTests.collect

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cmw-mpi-family-')
        self.addCleanup(temp.cleanup)
        self.store = SimpleNamespace(root=Path(temp.name))
        self.external_dir = self.store.root/'external'
        self.external_dir.mkdir()

    def topology(self, ranks=8):
        leader = process(100, '/bin/bash', parent=99, group=100, session=100)
        leader.cmdline.return_value = ['/bin/bash', str(Path(activity.__file__).with_name('payload.sh').resolve())]
        wrapper = process(101, '/bin/python3', parent=100, group=100, session=100)
        launcher = process(102, '/mpi/prterun', parent=101, group=100, session=100)
        processes = {100: leader, 101: wrapper, 102: launcher}
        processes.update({pid: process(pid, parent=102, session=100) for pid in range(110,110+ranks)})
        for p in processes.values():
            p.terminal.return_value = None
            p.cwd.return_value = str(self.external_dir)
        return processes

    def reserve(self, result):
        self.assertEqual(len(result['observations']), 1)
        return sharing.make_reservation(result['observations'][0], result, 8, 4)

    def test_payload_family_evidence_identity_and_telemetry_membership(self):
        processes = self.topology()
        guard = self.collect(processes, state={'jobs': [], '_telemetry_membership': True})
        self.assertEqual(guard['state'], 'BUSY')
        self.assertTrue(guard['coverage']['complete'])
        self.assertFalse(activity.permits(guard))
        self.assertEqual(len(guard['observations']), 1)
        family = guard['observations'][0]
        self.assertEqual(family['nproc'], 8)
        self.assertEqual(family['pid'], 110)
        self.assertEqual(family['launcher'], owner(102))
        self.assertEqual(family['family']['session_leader'], owner(100))
        self.assertEqual([a['identity']['pid'] for a in family['family']['ancestry']], [102,101,100])
        self.assertEqual({m['pid'] for m in family['members']}, set(range(110,118)))
        self.assertTrue(all(m['ppid']==102 and m['sid']==100 and m['exe']=='/engine/vasp_std'
                            and m['ownership']=='External / unattributed' for m in family['members']))
        again = self.collect(processes)['observations'][0]
        self.assertEqual((family['id'], sharing.fingerprint(family)), (again['id'], sharing.fingerprint(again)))
        clock = [10.0]
        for pid in range(110,118):
            processes[pid].cpu_times.return_value = SimpleNamespace(user=1.0, system=0.0)
            processes[pid].memory_info.return_value = SimpleNamespace(rss=pid)
        sampler = telemetry.Sampler(process_factory=lambda pid: processes[pid], identity_provider=owner,
            cpu_times=lambda: CPU(1,0,10,0,0,0), virtual_memory=lambda: SimpleNamespace(total=10000, available=8000),
            monotonic=lambda: clock[0])
        with patch.object(telemetry.os, 'getpgid', side_effect=lambda pid: processes[pid].group_value):
            sampler.sample(self.store, {'jobs':[]}, guard, guard['_pids'])
        clock[0] += 1
        for pid in range(110,118):
            processes[pid].cpu_times.return_value.user += .5
        with patch.object(telemetry.os, 'getpgid', side_effect=lambda pid: processes[pid].group_value):
            usage = sampler.sample(self.store, {'jobs':[]}, guard, guard['_pids'])['external'][family['id']]
        self.assertEqual(usage['cpu_cores'], 4)
        self.assertEqual(usage['rss_bytes'], sum(range(110,118)))
        self.assertEqual((usage['cpu_members'], usage['memory_members'], usage['total_members']), (8,8,8))

    def test_existing_launcher_session_and_homogeneous_executable_boundary(self):
        for exe in activity.LAUNCHERS:
            with self.subTest(exe=exe):
                processes = {10:process(10, '/mpi/'+exe, session=10),
                             11:process(11, parent=10, session=10),12:process(12, parent=10, session=10)}
                self.assertEqual(self.collect(processes)['observations'][0]['nproc'], 2)
        processes = self.topology(2)
        processes[111].exe_value = '/different/vasp_std'
        self.assertEqual(len(self.collect(processes)['observations']), 2)

    def test_shared_payload_or_generic_shell_never_merges_two_launchers(self):
        processes = self.topology(2)
        processes[103] = process(103, '/mpi/prterun', parent=101, group=100, session=100)
        processes[103].terminal.return_value = None
        processes[112] = process(112, parent=103, session=100)
        processes[113] = process(113, parent=103, session=100)
        result = self.collect(processes)
        self.assertEqual(sorted(o['nproc'] for o in result['observations']), [2,2])
        self.assertEqual({o['launcher']['pid'] for o in result['observations']}, {102,103})
        processes[100].cmdline.return_value = ['/bin/bash']
        self.assertEqual(len(self.collect(processes)['observations']), 4)

    def test_cwd_shell_name_terminal_and_unverified_ancestry_are_insufficient(self):
        for change in ('shell', 'tty', 'unrelated', 'session', 'cycle', 'unknown_launcher'):
            with self.subTest(change=change):
                p = self.topology(2)
                if change=='shell': p[100].cmdline.return_value = ['/bin/bash','/other/payload.sh']
                if change=='tty': p[100].terminal.return_value = '/dev/ttys001'
                if change=='unrelated': p[101].ppid.return_value = 99
                if change=='session': p[101].session_value = 99
                if change=='cycle': p[101].ppid.return_value = 102
                if change=='unknown_launcher': p[102].exe_value = '/bin/mpi-looking-runner'
                self.assertEqual(len(self.collect(p)['observations']), 2)
        p = self.topology(2)
        p[110].ppid.return_value = 100
        p[111].ppid.return_value = 100
        self.assertEqual(len(self.collect(p)['observations']), 2)

    def test_identity_exec_and_topology_replacement_invalidate_reservation(self):
        for change in ('launcher_birth','leader_birth','wrapper_birth','rank_birth','launcher_exe','rank_exe','topology'):
            with self.subTest(change=change):
                p = self.topology(2)
                reservation = self.reserve(self.collect(p))
                births = {'launcher_birth':102,'leader_birth':100,'wrapper_birth':101,'rank_birth':110}
                identities = lambda pid: owner(pid, 2 if pid==births.get(change) else 1)
                if change=='launcher_exe': p[102].exe_value = '/other/prterun'
                if change=='rank_exe': p[110].exe_value = p[111].exe_value = '/other/vasp_std'
                if change=='topology': p[102].ppid.return_value = 100
                current = self.collect(p, identities=identities)
                self.assertEqual(sharing.reservation_state(reservation,current,time.time()), 'expired')

    def test_membership_change_and_launcher_loss_do_not_keep_reservation(self):
        p = self.topology(2)
        reservation = self.reserve(self.collect(p))
        del p[111]
        self.assertEqual(sharing.reservation_state(reservation,self.collect(p),time.time()), 'expired')
        del p[102]
        current = self.collect(p)
        self.assertEqual(current['state'], 'BUSY')
        self.assertEqual(current['observations'][0]['nproc'], 1)
        self.assertIsNone(current['observations'][0]['launcher'])
        self.assertEqual(sharing.reservation_state(reservation,current,time.time()), 'expired')

    def test_final_collection_revalidates_session_leader_and_rank_parent(self):
        for change in ('leader_loss','leader_reuse','rank_parent'):
            with self.subTest(change=change):
                p = self.topology(2)
                counts = {}
                def identities(pid):
                    counts[pid] = counts.get(pid,0)+1
                    if pid==100 and counts[pid]>2:
                        if change=='leader_loss': raise psutil.NoSuchProcess(pid)
                        if change=='leader_reuse': return owner(pid,2)
                    if change=='rank_parent' and pid in (110,111) and counts[pid]>=3:
                        p[pid].ppid.return_value = 100
                    return owner(pid)
                result = self.collect(p, identities=identities)
                self.assertFalse(activity.permits(result))
                if change=='rank_parent':
                    self.assertFalse(result['coverage']['complete'])
                else:
                    self.assertEqual(len(result['observations']), 2)
                    self.assertTrue(all(o['launcher'] is None and o['family'] is None for o in result['observations']))

    def test_grouped_family_reaches_unchanged_reservation_and_resource_checks(self):
        policy = sharing_fixtures.SharingPolicyTests()
        policy.setUp()
        self.addCleanup(policy.doCleanups)
        policy.state['jobs'] = [policy.auxiliary]
        processes = self.topology()
        policy.guard = self.collect(processes)
        self.assertEqual(policy.assertBlocked()['reason'], 'External workload has no valid coexistence reservation')
        policy.state['scheduler']['external_reservation'] = sharing.make_reservation(
            policy.guard['observations'][0], policy.guard, 6, 8)
        self.assertTrue(policy.evaluate()['permitted'])
        policy.state['scheduler']['memory_gib'] = 11
        self.assertIn('Memory commitment', policy.assertBlocked()['reason'])
        policy.state['scheduler']['memory_gib'] = 12
        processes[200] = process(200, session=100)  # Same cwd/session never joins this launcher family.
        policy.guard = self.collect(processes)
        self.assertEqual(policy.assertBlocked()['reason'], 'Additional external computation blocks admission')
