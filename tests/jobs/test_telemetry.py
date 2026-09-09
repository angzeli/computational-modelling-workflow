"""Usage samples from injected counters only; no host process-table reads."""
import copy
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import activity, telemetry, ownership

CPU = namedtuple('CPU', 'user system idle iowait guest guest_nice')


def identity(pid, birth=1):
    return {'pid':pid, 'birth':birth, 'boot':1, 'host':'fixture'}


def process(cpu=0, rss=100):
    p = Mock()
    p.status.return_value = psutil.STATUS_SLEEPING
    p.cpu_times.return_value = SimpleNamespace(user=cpu, system=0)
    p.memory_info.return_value = SimpleNamespace(rss=rss)
    return p


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.clock = 10.0
        self.wall = 100.0
        self.processes = {1:process(10,100), 2:process(20,200)}
        self.owners = {pid:identity(pid) for pid in self.processes}
        self.machine = CPU(100,20,300,10,0,0)
        self.cpu_provider = Mock(side_effect=lambda:self.machine)
        self.memory_provider = Mock(return_value=SimpleNamespace(total=1000,available=250,used=900))
        self.sampler = telemetry.Sampler(process_factory=self.factory, identity_provider=lambda pid:self.owners[pid],
                                        cpu_times=self.cpu_provider,virtual_memory=self.memory_provider,
                                        monotonic=lambda:self.clock, wall_time=lambda:self.wall)
        self.store = Mock(root=Path('/fixture/state'))
        self.state = {'jobs':[]}
        self.guard = {'state':'BUSY','observations':[{'id':'E1','members':self.members(1,2)}], 'stale':False}
        self.no_pids = patch.object(psutil,'pids',side_effect=AssertionError('Unexpected host enumeration'))
        self.no_pids.start()
        self.addCleanup(self.no_pids.stop)

    def factory(self,pid):
        if pid not in self.processes:
            raise psutil.NoSuchProcess(pid)
        return self.processes[pid]

    def members(self,*pids):
        return [{'identity':self.owners[pid]} for pid in pids]

    def sample(self):
        return self.sampler.sample(self.store,self.state,self.guard,[])['external']['E1']

    def advance(self,seconds=2):
        self.clock += seconds
        self.wall += seconds

    def test_managed_membership_failure_is_independent_of_machine_and_guard(self):
        from cmw.jobs.store import JobsError
        self.state['jobs'] = [{'attempt_id': 'a', 'status': 'Run'}]
        with patch.object(telemetry, 'observed_group_members', side_effect=JobsError('inaccessible')):
            result = self.sampler.sample(self.store, self.state, self.guard, [])
        self.assertEqual(result['machine_usage']['memory_quality'], 'fresh')
        self.assertEqual(result['jobs']['a']['memory_quality'], 'unavailable')
        self.assertEqual(self.guard['state'], 'BUSY')
        self.assertEqual(self.state['jobs'][0]['status'], 'Run')

    def test_missing_managed_snapshot_retains_only_stale_usage(self):
        self.state['jobs'] = [{'attempt_id': 'a', 'status': 'Run'}]
        with patch.object(telemetry, 'observed_group_members', return_value=(self.members(1), '')):
            first = self.sampler.sample(self.store, self.state, self.guard, [])
        self.advance()
        with patch.object(telemetry, 'observed_group_members', side_effect=AssertionError('No rediscovery')):
            result = self.sampler.sample(self.store, self.state, self.guard, None)
        usage = result['jobs']['a']
        self.assertTrue(usage['stale'])
        self.assertEqual(usage['rss_bytes'], first['jobs']['a']['rss_bytes'])
        self.assertEqual(usage['age_seconds'], 2)

    def test_warmup_known_core_sum_and_valid_idle_zero(self):
        first = self.sample()
        self.assertIsNone(first['cpu_cores'])
        self.assertEqual(first['cpu_quality'],'warming-up')
        self.assertEqual(first['rss_bytes'],300)
        self.assertEqual(first['memory_quality'],'fresh')
        self.advance()
        self.processes[1].cpu_times.return_value.user += 1
        self.processes[2].cpu_times.return_value.user += 3
        second = self.sample()
        self.assertEqual(second['cpu_cores'],2)
        self.assertEqual(second['cpu_quality'],'fresh')
        self.assertEqual(second['cpu_members'],2)
        self.advance()
        idle = self.sample()
        self.assertEqual(idle['cpu_cores'],0)
        self.assertEqual(idle['cpu_quality'],'fresh')

    def test_new_member_is_partial_not_an_invented_zero(self):
        self.guard['observations'][0]['members'] = self.members(1)
        self.sample()
        self.advance()
        self.processes[1].cpu_times.return_value.user += 2
        self.guard['observations'][0]['members'] = self.members(1,2)
        usage = self.sample()
        self.assertEqual(usage['cpu_cores'],1)
        self.assertEqual((usage['cpu_members'],usage['total_members']),(1,2))
        self.assertEqual(usage['cpu_quality'],'partial')
        self.assertEqual(usage['memory_quality'],'fresh')

    def test_independent_metric_denials_preserve_subtotals(self):
        self.sample()
        self.advance()
        self.processes[1].cpu_times.side_effect = psutil.AccessDenied(1)
        self.processes[2].memory_info.side_effect = psutil.AccessDenied(2)
        self.processes[2].cpu_times.return_value.user += 4
        usage = self.sample()
        self.assertEqual(usage['cpu_cores'],2)
        self.assertEqual(usage['rss_bytes'],100)
        self.assertEqual((usage['cpu_members'],usage['memory_members'],usage['total_members']),(1,1,2))
        self.assertEqual((usage['cpu_quality'],usage['memory_quality']),('partial','partial'))
        self.assertIn('CPU inaccessible',usage['reason'])
        self.assertIn('RSS inaccessible',usage['reason'])

    def test_disappearance_prunes_baseline_and_pid_birth_resets(self):
        self.sample()
        self.advance()
        del self.processes[2]
        usage = self.sample()
        self.assertEqual(usage['total_members'],1)
        self.assertEqual(len(self.sampler.baselines),1)
        self.owners[1] = identity(1,birth=2)
        mismatch = self.sample()
        self.assertIsNone(mismatch['cpu_cores'])
        self.assertIsNone(mismatch['rss_bytes'])
        self.guard['observations'][0]['members'] = self.members(1)
        self.processes[1].cpu_times.return_value.user = 1000
        self.advance()
        self.assertEqual(self.sample()['cpu_quality'],'warming-up')

    def test_identity_change_during_read_rejects_both_metrics(self):
        self.guard['observations'][0]['members'] = self.members(1)
        self.sampler.identity = Mock(side_effect=[identity(1),identity(1,2)])
        usage = self.sample()
        self.assertIsNone(usage['cpu_cores'])
        self.assertIsNone(usage['rss_bytes'])
        self.assertEqual(usage['memory_members'],0)
        self.assertEqual(self.sampler.baselines,{})

    def test_machine_delta_is_nonblocking_and_ram_uses_available(self):
        self.sample()
        first = self.sampler.last['machine_usage']
        self.assertEqual(first['cpu_quality'],'warming-up')
        self.assertEqual(first['ram_used_bytes'],750)
        self.advance()
        # 10 busy, 10 idle; guest counters must not be counted a second time.
        self.machine = CPU(108,22,308,12,99,99)
        self.sample()
        self.assertEqual(self.sampler.last['machine_usage']['cpu_percent'],50)
        self.assertEqual(self.sampler.last['machine_usage']['cpu_quality'],'fresh')
        self.cpu_provider.assert_called_with()
        self.memory_provider.assert_called_with()

    def test_machine_metric_failure_leaves_other_metric_available(self):
        self.cpu_provider.side_effect = psutil.AccessDenied()
        self.sample()
        machine = self.sampler.last['machine_usage']
        self.assertIsNone(machine['cpu_percent'])
        self.assertEqual(machine['memory_quality'],'fresh')
        self.assertEqual(machine['ram_used_bytes'],750)

    def test_stale_guard_preserves_old_usage_without_reading_members(self):
        prior = self.sample()
        self.advance()
        self.guard['stale'] = True
        self.sampler.process_factory = Mock(side_effect=AssertionError('Stale members must not be read'))
        usage = self.sample()
        self.assertEqual(usage['rss_bytes'],prior['rss_bytes'])
        self.assertTrue(usage['stale'])
        self.assertEqual(usage['age_seconds'],2)
        self.assertIn('membership observation stale',usage['reason'])

    def test_age_projection_is_pure_and_marks_stale(self):
        usage = self.sample()
        original = copy.deepcopy(usage)
        aged = telemetry.age_usage(usage,now=self.wall+telemetry.MAX_AGE+1)
        self.assertTrue(aged['stale'])
        self.assertEqual(usage,original)

    def test_timeout_one_inflight_discards_late_baselines(self):
        self.sample()
        baseline = copy.deepcopy(self.sampler.baselines)
        accepted = copy.deepcopy(self.sampler.last)
        release = threading.Event()
        entered = threading.Event()
        def blocked():
            entered.set()
            release.wait(2)
            return SimpleNamespace(user=999,system=0)
        self.processes[1].cpu_times.side_effect = blocked
        self.advance()
        try:
            with patch.object(telemetry,'SAMPLE_BUDGET',.02):
                usage = self.sample()
                self.assertTrue(entered.is_set())
                self.assertTrue(usage['stale'])
                thread = self.sampler.thread
                self.assertTrue(self.sample()['stale'])
                self.assertIs(self.sampler.thread,thread)
                self.assertEqual(self.sampler.baselines,baseline)
                self.assertEqual(self.sampler.last,accepted)
        finally:
            release.set()
            self.sampler.thread.join(2)
        self.assertEqual(self.sampler.baselines,baseline)
        self.assertEqual(self.sampler.last,accepted)
        self.processes[1].cpu_times.side_effect = None
        self.processes[1].cpu_times.return_value.user = 12
        self.processes[2].cpu_times.return_value.user = 20
        usage = self.sample()
        self.assertEqual(usage['cpu_cores'],1)
        self.assertFalse(usage['stale'])

    def test_cpu_interval_uses_monotonic_time_not_wall_clock(self):
        self.guard['observations'][0]['members'] = self.members(1)
        self.sample()
        self.clock += 4
        self.wall -= 10000
        self.processes[1].cpu_times.return_value.user += 2
        usage = self.sample()
        self.assertEqual(usage['cpu_cores'],.5)
        self.assertEqual(usage['cpu_quality'],'fresh')

    def test_unrelated_observation_metrics_remain_separate_and_denial_stays_busy(self):
        self.guard['observations'] = [{'id':'E1','members':self.members(1)},
                                      {'id':'E2','members':self.members(2)}]
        self.sample()
        self.advance()
        self.processes[1].cpu_times.return_value.user += 2
        self.processes[2].cpu_times.return_value.user += 6
        result = self.sampler.sample(self.store,self.state,self.guard,[])
        self.assertEqual(result['external']['E1']['cpu_cores'],1)
        self.assertEqual(result['external']['E2']['cpu_cores'],3)
        self.assertEqual(result['external']['E1']['rss_bytes'],100)
        self.assertEqual(result['external']['E2']['rss_bytes'],200)
        self.advance()
        for p in self.processes.values():
            p.cpu_times.side_effect = psutil.AccessDenied()
            p.memory_info.side_effect = psutil.AccessDenied()
        result = self.sampler.sample(self.store,self.state,self.guard,[])
        for usage in result['external'].values():
            self.assertIsNone(usage['cpu_cores'])
            self.assertIsNone(usage['rss_bytes'])
            self.assertEqual((usage['cpu_quality'],usage['memory_quality']),('unavailable','unavailable'))
        self.assertEqual(self.guard['state'],'BUSY')
        self.assertFalse(activity.permits(self.guard))

    def test_expired_cpu_baselines_warm_up_without_losing_current_memory(self):
        self.sample()
        self.advance(telemetry.MAX_AGE+1)
        self.processes[1].cpu_times.return_value.user += 12
        self.machine = CPU(110,20,310,10,0,0)
        usage = self.sample()
        self.assertEqual(usage['cpu_quality'],'warming-up')
        self.assertIsNone(usage['cpu_cores'])
        self.assertEqual(usage['rss_bytes'],300)
        self.assertEqual(usage['memory_quality'],'fresh')
        machine = self.sampler.last['machine_usage']
        self.assertEqual(machine['cpu_quality'],'warming-up')
        self.assertIsNone(machine['cpu_percent'])
        self.assertEqual(machine['ram_used_bytes'],750)
        self.assertEqual(machine['memory_quality'],'fresh')
        self.advance(2)
        self.processes[1].cpu_times.return_value.user += 2
        self.machine = CPU(120,20,320,10,0,0)
        usage = self.sample()
        self.assertEqual(usage['cpu_quality'],'fresh')
        self.assertEqual(usage['cpu_cores'],1)
        self.assertEqual(self.sampler.last['machine_usage']['cpu_percent'],50)

    def verified_members(self, *, leader_alive=False, leader_reused=False,
                         guardian=True, locked=True, member_owner=None, session=10, group=10):
        job = {'group':identity(10),'worker':identity(9),'attempt_id':'attempt'}
        def identities(pid):
            if pid==10:
                if leader_reused:
                    return identity(10,2)
                if not leader_alive:
                    raise psutil.NoSuchProcess(pid)
            return member_owner if pid==1 and member_owner else identity(pid)
        with patch.object(ownership,'owner_alive',return_value=guardian), \
             patch.object(ownership,'lock_held',return_value=locked), \
             patch.object(ownership.os,'getsid',return_value=session), \
             patch.object(ownership.os,'getpgid',return_value=group):
            return ownership.observed_group_members(self.store,job,[1],
                process_factory=self.factory,identity_provider=identities)

    def test_missing_group_leader_preserves_verified_surviving_child(self):
        members, reason = self.verified_members()
        self.assertEqual(members,[{'identity':identity(1),'pgid':10}])
        self.assertEqual(reason,'')

    def test_reused_leader_and_missing_guardian_or_lock_reject_membership(self):
        for conditions in [{'leader_reused':True},{'guardian':False},{'locked':False}]:
            with self.subTest(conditions=conditions):
                members,reason = self.verified_members(**conditions)
                self.assertEqual(members,[])
                self.assertTrue(reason)

    def test_membership_requires_matching_host_boot_session_and_group(self):
        for conditions in [{'member_owner':{**identity(1),'host':'other'}},
                           {'member_owner':{**identity(1),'boot':2}},
                           {'session':99},{'group':99}]:
            with self.subTest(conditions=conditions):
                members,_ = self.verified_members(**conditions)
                self.assertEqual(members,[])

    def test_sampling_cannot_mutate_guard_or_store(self):
        original = copy.deepcopy((self.state,self.guard))
        self.assertFalse(activity.permits(self.guard))
        self.sample()
        self.assertEqual((self.state,self.guard),original)
        self.assertFalse(activity.permits(self.guard))
        self.assertEqual(self.store.mock_calls,[])
        self.assertEqual(self.state['jobs'],[])
