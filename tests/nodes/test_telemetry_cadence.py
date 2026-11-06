"""Node telemetry cadence with injected clocks/counters; no host process reads."""
from collections import namedtuple
from copy import deepcopy
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.activity import Observer
from cmw.jobs.telemetry import Sampler, empty_usage
from cmw.jobs.thermal import unavailable_thermal
from cmw.nodes.telemetry import INTERVAL, NodeTelemetry, UNAVAILABLE_AFTER

CPU = namedtuple('CPU', 'user system idle')


class NodeTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.mono, self.wall = 10.0, 100.0
        self.state = {'jobs': [], 'controller': {}, 'scheduler': {'mode': 'sequential'}}
        self.guard = {'state': 'NO_MATCH', 'stale': False, 'observations': [], '_pids': []}
        self.store = Mock()
        self.store.snapshot.side_effect = lambda: deepcopy(self.state)
        self.observer = Mock()
        self.observer.scan.side_effect = lambda *a, **kw: deepcopy(self.guard)
        self.window_clock = patch('cmw.jobs.sharing.time.time', side_effect=lambda: self.wall)
        self.window_clock.start()
        self.addCleanup(self.window_clock.stop)

    def advance(self, amount):
        self.mono += amount
        self.wall += amount

    def provider(self, factory=None):
        if factory is None:
            factory = lambda: Sampler(cpu_times=lambda: CPU(self.mono*2, self.mono, self.mono),
                virtual_memory=lambda: SimpleNamespace(total=1000, available=400),
                swap_memory=None, thermal=None, footprint=None,
                process_factory=Mock(side_effect=AssertionError('No host process reads')),
                monotonic=lambda: self.mono, wall_time=lambda: self.wall)
        provider = NodeTelemetry(self.store, sampler_factory=factory, observer_factory=lambda: self.observer,
            monotonic=lambda: self.mono, wall_time=lambda: self.wall, autostart=False)
        self.addCleanup(provider.close)
        return provider

    def snapshot(self, provider):
        return provider.sample(self.store, deepcopy(self.state), deepcopy(self.guard), [])

    def warm(self, provider):
        self.assertTrue(provider.start(background=False))
        self.assertTrue(provider.collect_once())
        self.advance(INTERVAL)
        self.assertTrue(provider.collect_once())
        return self.snapshot(provider)['machine_usage']

    def test_reads_are_nonblocking_cached_snapshots_without_collecting(self):
        provider = self.provider()
        unavailable = self.snapshot(provider)['machine_usage']
        self.assertEqual(unavailable['cpu_quality'], 'unavailable')
        self.assertEqual(unavailable['sampler_status'], 'unavailable')
        self.store.snapshot.assert_not_called()
        self.observer.scan.assert_not_called()
        self.warm(provider)
        calls = self.store.snapshot.call_count
        for _ in range(3):
            self.snapshot(provider)
        self.assertEqual(self.store.snapshot.call_count, calls)
        self.assertEqual(self.observer.scan.call_count, calls)

    def test_five_second_polling_does_not_drive_or_expire_cpu_baseline(self):
        provider = self.provider()
        provider.start(background=False)
        provider.collect_once()
        for _ in range(2):
            self.advance(2)
            provider.collect_once()
        self.advance(1.2)
        first = self.snapshot(provider)['machine_usage']
        self.assertEqual(first['cpu_quality'], 'fresh')
        self.assertAlmostEqual(first['age_seconds'], 1.2)
        self.advance(.8)
        provider.collect_once()
        for _ in range(2):
            self.advance(2)
            provider.collect_once()
        self.advance(.4)
        second = self.snapshot(provider)['machine_usage']
        self.assertEqual(second['cpu_quality'], 'fresh')
        self.assertAlmostEqual(second['cpu_percent'], 75.)
        self.assertEqual(self.store.snapshot.call_count, 6)

    def test_inclusive_freshness_and_expiry_clear_measurements(self):
        provider = self.provider()
        self.warm(provider)
        self.advance(5)
        self.assertFalse(self.snapshot(provider)['machine_usage']['stale'])
        self.advance(.001)
        stale = self.snapshot(provider)['machine_usage']
        self.assertTrue(stale['stale'])
        self.assertEqual(stale['cpu_quality'], 'stale')
        self.assertEqual(stale['sampler_status'], 'stale')
        self.assertIsNotNone(stale['ram_used_bytes'])
        self.advance(UNAVAILABLE_AFTER-5)
        expired = self.snapshot(provider)['machine_usage']
        self.assertEqual(expired['sampler_status'], 'unavailable')
        self.assertEqual(expired['memory_quality'], 'unavailable')
        self.assertIsNone(expired['cpu_percent'])
        self.assertIsNone(expired['ram_used_bytes'])

    def test_forward_wall_step_cannot_expire_a_recent_sample(self):
        provider = self.provider()
        self.warm(provider)
        self.wall += 28791.6
        self.mono += .5
        current = self.snapshot(provider)['machine_usage']
        self.assertEqual(current['age_seconds'], .5)
        self.assertFalse(current['stale'])

    def test_backwards_wall_clock_cannot_revive_old_sample(self):
        provider = self.provider()
        self.warm(provider)
        self.mono += 6
        self.wall -= 100
        stale = self.snapshot(provider)['machine_usage']
        self.assertEqual(stale['age_seconds'], 6)
        self.assertTrue(stale['stale'])
        self.mono += 10
        self.assertEqual(self.snapshot(provider)['machine_usage']['cpu_quality'], 'unavailable')

    def test_stop_marks_retained_sample_stale_and_close_is_terminal(self):
        provider = self.provider()
        self.warm(provider)
        provider.stop()
        stopped = self.snapshot(provider)['machine_usage']
        self.assertTrue(stopped['stale'])
        self.assertIn('stopped', stopped['reason'])
        self.assertFalse(provider.collect_once())
        provider.close()
        self.assertFalse(provider.start(background=False))

    def test_restart_uses_new_baselines_and_discards_prior_window(self):
        provider = self.provider()
        self.warm(provider)
        previous = provider._sampler
        provider.stop()
        self.assertTrue(provider.start(background=False))
        self.assertIsNot(provider._sampler, previous)
        self.assertEqual(provider.evidence()['samples'], [])
        provider.collect_once()
        self.assertEqual(self.snapshot(provider)['machine_usage']['cpu_quality'], 'warming-up')
        self.advance(2)
        provider.collect_once()
        self.assertEqual(self.snapshot(provider)['machine_usage']['cpu_quality'], 'fresh')

    def test_failed_sample_never_becomes_fresh_cached_success(self):
        provider = self.provider()
        self.warm(provider)
        self.store.snapshot.side_effect = OSError('synthetic failure')
        self.assertFalse(provider.collect_once())
        result = self.snapshot(provider)['machine_usage']
        self.assertTrue(result['stale'])
        self.assertIn('sample failed: OSError', result['reason'])
        self.assertTrue(provider.evidence()['samples'][-1]['machine_usage']['stale'])

    def test_polling_never_fabricates_additional_sustained_samples(self):
        provider = self.provider()
        provider.start(background=False)
        provider.collect_once()
        for _ in range(3):
            self.advance(.6)
            evidence = provider._sharing_window.add(self.snapshot(provider), source='client')
            self.assertEqual(len(evidence['samples']), 1)
        self.advance(.2)
        provider.collect_once()
        evidence = provider._sharing_window.add(self.snapshot(provider), source='client')
        self.assertEqual(len(evidence['samples']), 2)
        self.assertEqual(evidence['samples'][0]['observed_at'], 100.)
        self.assertAlmostEqual(evidence['samples'][1]['observed_at'], 102.)

    def test_late_result_after_stop_is_discarded_and_samples_do_not_overlap(self):
        entered, finish = threading.Event(), threading.Event()
        sampler = Mock()
        def blocked(*args):
            entered.set()
            self.assertTrue(finish.wait(2))
            return {'machine_usage': {**Sampler._empty_machine(), 'observed_at': self.wall},
                    'jobs': {}, 'external': {}}
        sampler.sample.side_effect = blocked
        provider = self.provider(lambda: sampler)
        provider.start(background=False)
        worker = threading.Thread(target=provider.collect_once)
        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            self.assertFalse(provider.collect_once())
            provider.stop()
            self.assertFalse(provider.start(background=False))
        finally:
            finish.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(provider._last)
        self.assertEqual(sampler.sample.call_count, 1)
        sampler.close.assert_called_once_with()

    def test_restart_waits_for_timed_out_underlying_read_and_discards_late_value(self):
        entered, finish = threading.Event(), threading.Event()
        def slow_cpu():
            entered.set()
            finish.wait(5)
            return CPU(100, 10, 200)
        def factory():
            return Sampler(cpu_times=slow_cpu, virtual_memory=lambda: SimpleNamespace(total=1000, available=500),
                swap_memory=None, thermal=None, footprint=None,
                monotonic=lambda:self.mono, wall_time=lambda:self.wall)
        provider = self.provider(factory)
        provider.start(background=False)
        try:
            provider.collect_once()
            self.assertTrue(entered.is_set())
            old_sampler = provider._sampler
            self.assertIsNone(self.snapshot(provider)['machine_usage']['cpu_percent'])
            provider.stop()
            self.assertFalse(provider.start(background=False))
        finally:
            finish.set()
            old_sampler.thread.join(2)
        self.assertIsNone(old_sampler.last)
        self.assertTrue(provider.start(background=False))
        provider.collect_once()
        self.assertEqual(self.snapshot(provider)['machine_usage']['cpu_quality'], 'warming-up')

    def test_failed_cached_sample_cannot_reset_age_after_wall_clock_steps_back(self):
        sampler = Mock()
        from cmw.jobs.freshness import clock_stamp
        result = {'machine_usage':{**Sampler._empty_machine(), 'observed_at':self.wall, 'observed_clock':clock_stamp(self.mono),
                                  'cpu_percent':75., 'cpu_quality':'fresh'}, 'jobs':{}, 'external':{}}
        sampler.sample.side_effect = lambda *a: deepcopy(result)
        provider = self.provider(lambda:sampler)
        provider.start(background=False)
        provider.collect_once()
        self.advance(3)
        self.wall -= 100
        result['machine_usage'].update(stale=True, age_seconds=0.)
        provider.collect_once()
        self.advance(3)
        aged = self.snapshot(provider)['machine_usage']
        self.assertEqual(aged['age_seconds'], 6.)
        self.assertTrue(aged['stale'])

    def test_endpoint_display_marks_legacy_and_prior_boot_samples_stale(self):
        sampler = Mock()
        provider = self.provider(lambda: sampler)
        provider.start(background=False)
        for stamp in (None, {'boot_session_id': 'previous-boot', 'monotonic': self.mono}):
            with self.subTest(stamp=stamp):
                usage = {**Sampler._empty_machine(), 'observed_at': self.wall, 'cpu_percent': 75., 'cpu_quality': 'fresh'}
                if stamp is not None:
                    usage['observed_clock'] = stamp
                sampler.sample.return_value = {'machine_usage': usage, 'jobs': {}, 'external': {}}
                self.assertTrue(provider.collect_once())
                retained = self.snapshot(provider)['machine_usage']
                self.assertTrue(retained['stale'])
                self.assertIsNone(retained['age_seconds'])
                self.assertEqual((retained['cpu_percent'], retained['cpu_quality']), (75., 'stale'))

    def test_restart_waits_for_timed_out_observer_and_discards_late_membership(self):
        entered, finish = threading.Event(), threading.Event()
        def collector(*args):
            entered.set()
            finish.wait(5)
            return deepcopy(self.guard)
        sampler = Mock()
        sampler.sample.return_value = {'machine_usage': Sampler._empty_machine(), 'jobs': {}, 'external': {}}
        factory = Mock(side_effect=lambda: Observer(collector))
        provider = NodeTelemetry(self.store, sampler_factory=lambda: sampler, observer_factory=factory,
            monotonic=lambda: self.mono, wall_time=lambda: self.wall, autostart=False)
        self.addCleanup(provider.close)
        provider.start(background=False)
        previous = provider._observer
        try:
            with patch('cmw.jobs.activity.SCAN_BUDGET', .01):
                self.assertTrue(provider.collect_once())
            self.assertTrue(entered.is_set())
            self.assertTrue(previous.thread.is_alive())
            provider.stop()
            self.assertFalse(provider.start(background=False))
            factory.assert_called_once_with()
        finally:
            finish.set()
            if previous.thread is not None:
                previous.thread.join(2)
        self.assertFalse(previous.thread.is_alive())
        self.assertIsNone(previous.last)
        self.assertTrue(provider.start(background=False))
        self.assertIsNot(provider._observer, previous)
        self.assertEqual(factory.call_count, 2)

    def test_worker_restart_refuses_while_previous_thread_is_alive(self):
        provider = self.provider()
        provider._thread = Mock()
        provider._thread.is_alive.return_value = True
        self.assertFalse(provider.start(background=False))
        provider._thread = None
        self.assertTrue(provider.start(background=False))

    def test_cadence_uses_quiet_interval_after_slow_sample_no_catchup(self):
        provider = self.provider()
        provider.start(background=False)
        waits=[]
        stop=Mock()
        stop.is_set.return_value=False
        def tick(**kwargs):
            self.advance(.4 if not waits else 3)
        def wait(delay):
            waits.append(delay)
            self.advance(delay)
            return len(waits) == 2
        stop.wait.side_effect=wait
        with patch.object(provider, 'collect_once', side_effect=tick):
            provider._run(provider._generation, stop)
        self.assertAlmostEqual(waits[0], 1.6)
        self.assertEqual(waits[1], 2.)
        self.assertFalse(provider._running)

    def test_usage_is_filtered_by_current_attempt_owner_and_external_members(self):
        owner={'pid': 1, 'birth': 1, 'host': 'fixture', 'boot': 1}
        self.state['jobs']=[{'id':1,'attempt_id':'a','status':'Run','group':owner}]
        self.guard['observations']=[{'id':'E1','engine':'VASP','members':[{'identity':owner}]}]
        usage={**empty_usage(), 'observed_at': self.wall, 'age_seconds':0., 'rss_bytes':123,
               'memory_quality':'fresh'}
        sampler=Mock()
        sampler.sample.return_value={'machine_usage': {**Sampler._empty_machine(), 'observed_at':self.wall},
                                     'jobs': {'a':usage}, 'external': {'E1':usage}}
        provider=self.provider(lambda:sampler)
        provider.start(background=False)
        provider.collect_once()
        first=self.snapshot(provider)
        self.assertEqual(first['jobs']['a']['rss_bytes'],123)
        self.assertEqual(first['external']['E1']['rss_bytes'],123)
        self.state['jobs'][0]['group']={**owner,'birth':2}
        self.guard['observations'][0]['members']=[{'identity':{**owner,'birth':2}}]
        changed=self.snapshot(provider)
        self.assertIsNone(changed['jobs']['a']['rss_bytes'])
        self.assertIsNone(changed['external']['E1']['rss_bytes'])
        self.assertEqual(changed['jobs']['a']['memory_quality'],'unavailable')
        self.assertEqual(changed['external']['E1']['memory_quality'],'unavailable')

    def test_status_stale_membership_cannot_present_cached_external_usage_as_fresh(self):
        owner={'pid':1,'birth':1}
        self.guard['observations']=[{'id':'E1','engine':'VASP','members':[{'identity':owner}]}]
        sampler=Mock()
        sampler.sample.return_value={'machine_usage':Sampler._empty_machine(), 'jobs':{},
            'external':{'E1':{**empty_usage(), 'observed_at':self.wall, 'memory_quality':'fresh'}}}
        provider=self.provider(lambda:sampler)
        provider.start(background=False)
        provider.collect_once()
        self.guard['stale']=True
        result=self.snapshot(provider)['external']['E1']
        self.assertTrue(result['stale'])
        self.assertEqual(result['memory_quality'],'stale')

    def test_fallback_usage_keeps_original_job_owner_and_external_membership(self):
        owner = {'pid': 1, 'birth': 1, 'host': 'fixture', 'boot': 1}
        self.state['jobs'] = [{'id': 1, 'attempt_id': 'a', 'status': 'Run', 'group': owner}]
        self.guard['observations'] = [{'id': 'E1', 'engine': 'VASP', 'members': [{'identity': owner}]}]
        usage = {**empty_usage(), 'observed_at': self.wall, 'age_seconds': 0., 'rss_bytes': 123,
                 'memory_quality': 'fresh'}
        result = {'machine_usage': Sampler._empty_machine(), 'jobs': {'a': deepcopy(usage)},
                  'external': {'E1': deepcopy(usage)}}
        sampler = Mock()
        sampler.sample.side_effect = lambda *args: deepcopy(result)
        provider = self.provider(lambda: sampler)
        provider.start(background=False)
        provider.collect_once()
        self.assertEqual(self.snapshot(provider)['jobs']['a']['rss_bytes'], 123)
        self.assertEqual(self.snapshot(provider)['external']['E1']['rss_bytes'], 123)
        self.state['jobs'][0]['group'] = {**owner, 'birth': 2}
        self.guard['observations'][0]['members'] = [{'identity': {**owner, 'birth': 2}}]
        for section, key in (('jobs', 'a'), ('external', 'E1')):
            result[section][key].update(stale=True, memory_quality='stale')
        for _ in range(2):
            self.advance(2)
            self.assertTrue(provider.collect_once())
            current = self.snapshot(provider)
            for section, key in (('jobs', 'a'), ('external', 'E1')):
                self.assertIsNone(current[section][key]['rss_bytes'])
                self.assertEqual(current[section][key]['memory_quality'], 'unavailable')
        # A genuinely new sample can bind to the new owner/members again.
        self.advance(2)
        for section, key in (('jobs', 'a'), ('external', 'E1')):
            result[section][key].update(observed_at=self.wall, stale=False, memory_quality='fresh')
        self.assertTrue(provider.collect_once())
        current = self.snapshot(provider)
        self.assertEqual(current['jobs']['a']['rss_bytes'], 123)
        self.assertEqual(current['external']['E1']['rss_bytes'], 123)

    def test_snapshot_is_independent_copy_and_thermal_age_is_not_doubled(self):
        thermal={**unavailable_thermal(), 'temperature_celsius':60., 'sample_timestamp':self.wall-10,
                 'sample_age':10., 'quality':'fresh'}
        sampler=Mock()
        sampler.sample.return_value={'machine_usage':{**Sampler._empty_machine(), 'observed_at':self.wall,
                                                      'thermal':thermal}, 'jobs':{}, 'external':{}}
        provider=self.provider(lambda:sampler)
        provider.start(background=False)
        provider.collect_once()
        self.advance(2)
        snapshot=self.snapshot(provider)
        self.assertEqual(snapshot['machine_usage']['thermal']['sample_age'],12.)
        snapshot['machine_usage']['thermal']['temperature_celsius']=999
        self.assertEqual(self.snapshot(provider)['machine_usage']['thermal']['temperature_celsius'],60.)

    def test_invalid_interval_is_rejected_before_start(self):
        for interval in (True, 0, -1, 5, float('nan'), float('inf')):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                NodeTelemetry(self.store, interval=interval)
