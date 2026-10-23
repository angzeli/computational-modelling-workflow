"""Optional thermal observation from test-owned executables only."""
from collections import namedtuple
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import activity, thermal
from cmw.jobs.telemetry import Sampler, age_usage
from tests.jobs.thermal_fixtures import write_fake_macmon


class ThermalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-thermal-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def provider(self, mode='valid', **kwargs):
        executable = write_fake_macmon(self.root / f'macmon-{mode}-{len(list(self.root.iterdir()))}', mode=mode)
        value = thermal.ThermalProvider(executable, system='Darwin', machine='arm64', **kwargs)
        self.addCleanup(value.close)
        return value

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic()+timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail('test-owned provider did not reach expected state')
            time.sleep(.01)

    def parse(self, value, **extra):
        return thermal.parse_macmon(json.dumps({'temp':{'cpu_temp_avg':value}, **extra}),
                                    started_at=100., received_at=102.)

    def test_valid_sensor_only_and_independent_freshness(self):
        result = self.parse(63.2, memory={'ram_used':1}, cpu_usage_pct=.9)
        self.assertEqual(result['temperature_celsius'],63.2)
        self.assertEqual(result['sample_age'],2)
        self.assertNotIn('memory',result)
        result = thermal.age_thermal(result,now=0,elapsed=29)
        self.assertEqual(result['quality'],'stale')
        self.assertEqual(result['sample_age'],31)
        self.assertEqual(thermal.age_thermal(result,now=0,elapsed=30)['quality'],'unavailable')
        machine = {'observed_at':100,'stale':False,'thermal':self.parse(63.2)}
        aged = age_usage(machine,now=110)
        self.assertTrue(aged['stale'])
        self.assertEqual(aged['thermal']['quality'],'fresh')
        disconnected = thermal.age_thermal(self.parse(63.2),now=0,unavailable=True)
        self.assertEqual(disconnected['quality'],'stale')
        self.assertEqual(disconnected['sample_timestamp'],100)
        self.assertEqual(thermal.age_thermal(self.parse(63.2),now=110,elapsed=3)['sample_age'],13)

    def test_invalid_sensor_values_do_not_become_zero(self):
        for value in [None, True, False, 0, -1, 151, '63', {}, [], float('nan'),float('inf'),10**1000]:
            with self.subTest(value=str(value)[:15]), self.assertRaises(thermal.ThermalError):
                self.parse(value)

    def test_schema_units_and_framing(self):
        for payload in ['null','[]','{}','{"temp":{}}','{"temp":{"gpu_temp_avg":61}}',
                        '{"temp":', '{"temp":{}}\n{"temp":',
                        '{"temp":{"cpu_temp_avg":63,"cpu_temp_avg":64}}',
                        '{"schema_version":2,"temp":{"cpu_temp_avg":63}}']:
            with self.subTest(payload=payload),self.assertRaises(thermal.ThermalError):
                thermal.parse_macmon(payload,started_at=100,received_at=100)
        for units in ['Fahrenheit',{},[],None,False]:
            with self.subTest(units=units),self.assertRaises(thermal.ThermalError):
                thermal.parse_macmon(json.dumps({'temp':{'cpu_temp_avg':63,'units':units}}),
                                     started_at=100,received_at=100)

    def test_original_timestamp_is_not_receipt_time(self):
        result = self.parse(63.2,timestamp='1970-01-01T00:01:30Z')
        self.assertEqual((result['sample_timestamp'],result['sample_age']),(90,12))
        for stamp in ['not-a-date','1970-01-01T00:02:00Z','1970-01-01T00:01:30',100,{}]:
            with self.subTest(stamp=stamp),self.assertRaises(thermal.ThermalError):
                self.parse(63.2,timestamp=stamp)
        expired=self.parse(63.2,timestamp='1970-01-01T00:00:01Z')
        self.assertEqual(expired['quality'],'unavailable')
        self.assertIsNone(expired['temperature_celsius'])

    def test_unknown_and_malformed_transport_values_are_unavailable(self):
        valid=self.parse(63.2)
        for value in [None,[],{},'bad',dict(valid,source='other'),dict(valid,quality=[]),
                      dict(valid,units={}),dict(valid,sample_timestamp=False),dict(valid,sample_age=None)]:
            with self.subTest(value=value):
                self.assertEqual(thermal.age_thermal(value,now=0)['quality'],'unavailable')
        self.assertEqual(thermal.age_thermal(valid,now=0),valid)

    def test_configuration_is_optional_and_platform_limited(self):
        for kwargs in [{}, {'executable':'relative'}, {'executable':str(self.root/'absent')},
                       {'executable':str(self.root/'absent'),'system':'Linux'},
                       {'executable':str(self.root/'absent'),'machine':'x86_64'}]:
            provider=thermal.ThermalProvider(**kwargs)
            self.assertEqual(provider.snapshot()['quality'],'unavailable')
            self.assertIsNone(provider.thread)
            provider.close()
        file=self.root/'not-executable';file.write_text('not executable');file.chmod(0o600)
        self.assertIsNone(thermal.ThermalProvider(file).executable)
        file.chmod(0o722)
        self.assertIsNone(thermal.ThermalProvider(file).executable)
        for interval in [0,14,31,None,True,float('nan')]:
            value=thermal.ThermalProvider(file,interval=interval)
            self.assertIsNone(value.executable)
        with patch.dict('os.environ',{'CMW_THERMAL_MACMON':str(file),'CMW_THERMAL_INTERVAL_SECONDS':'bad'}):
            self.assertIsNone(thermal.ThermalProvider.from_environment().executable)

    def test_success_and_many_refreshes_use_one_bounded_operation(self):
        provider=self.provider()
        self.assertEqual(provider.snapshot()['quality'],'unavailable')
        self.wait_for(lambda:provider.sample_count==1)
        for _ in range(200):
            self.assertEqual(provider.snapshot()['temperature_celsius'],63.2)
        calls=[json.loads(x) for x in Path(provider.executable+'.calls').read_text().splitlines()]
        self.assertEqual(calls,[['--version'],['pipe','--help'],['pipe','--samples','1','--interval','1000']])
        self.assertEqual(provider.launch_count,3)
        self.assertIsNone(provider.process)
        self.assertTrue(provider.thread.is_alive())
        provider.close()
        self.assertFalse(provider.thread.is_alive())
        self.assertIsNone(provider.process)
        provider.snapshot()
        self.assertEqual(provider.launch_count,3)

    def test_failure_categories_and_no_raw_stderr(self):
        for mode in ['null','missing','malformed','multiple','empty','fail','version','help','overflow','stderr']:
            with self.subTest(mode=mode):
                provider=self.provider(mode)
                provider.snapshot()
                self.wait_for(lambda:provider.failures==1)
                value=provider.snapshot()
                self.assertEqual(value['quality'],'unavailable')
                self.assertIsNone(value['temperature_celsius'])
                self.assertNotIn('PRIVATE',str(value))
                provider.close()
                self.assertIsNone(provider.process)
                self.assertFalse(provider.thread.is_alive())

    def test_hung_operation_does_not_block_refresh_and_is_reaped(self):
        provider=self.provider('hang',sample_timeout=.15)
        provider.snapshot()
        self.wait_for(lambda:provider.process is not None and provider.launch_count==3)
        process=provider.process
        before=time.monotonic()
        for _ in range(200): provider.snapshot()
        self.assertLess(time.monotonic()-before,.1)
        self.wait_for(lambda:provider.failures==1)
        self.assertIn('timed out',provider.snapshot()['availability_reason'])
        self.assertIsNotNone(process.poll())
        self.assertFalse(psutil.pid_exists(process.pid))

    def test_close_reaps_only_owned_child(self):
        provider=self.provider('hang')
        provider.snapshot()
        self.wait_for(lambda:provider.process is not None and provider.launch_count==3)
        process=provider.process
        before=time.monotonic();provider.close()
        self.assertLess(time.monotonic()-before,1.5)
        self.assertFalse(provider.thread.is_alive())
        self.assertIsNotNone(process.poll())
        self.assertFalse(psutil.pid_exists(process.pid))

    def test_backoff_disables_after_three_failures(self):
        provider=self.provider('fail')
        # Accelerate only this test; configured production intervals stay 15–30s.
        provider.interval=.04
        provider.snapshot()
        self.wait_for(lambda:provider.failures==3)
        provider.thread.join(.1)
        self.assertFalse(provider.thread.is_alive())
        self.assertEqual(provider.launch_count,5)
        self.assertIn('disabled',provider.snapshot()['availability_reason'])
        for _ in range(100):provider.snapshot()
        self.assertEqual(provider.launch_count,5)

    def test_number_to_null_drops_previous_sample(self):
        provider=self.provider('changes')
        provider.interval=.1
        provider.snapshot()
        self.wait_for(lambda:provider.sample_count==1)
        self.assertEqual(provider.snapshot()['temperature_celsius'],63.2)
        self.wait_for(lambda:provider.failures==1)
        self.assertIsNone(provider.snapshot()['temperature_celsius'])

    def test_changed_executable_is_not_run_again(self):
        provider=self.provider()
        provider.interval=.5
        provider.snapshot();self.wait_for(lambda:provider.sample_count==1)
        Path(provider.executable).write_text('untrusted replacement')
        self.wait_for(lambda:provider.failures==1)
        self.assertEqual(provider.launch_count,3)
        self.assertIn('changed',provider.snapshot()['availability_reason'])

    def test_multiple_sessions_remain_bounded(self):
        providers=[self.provider('hang') for _ in range(3)]
        for provider in providers: provider.snapshot()
        self.wait_for(lambda:all(p.launch_count==3 and p.process is not None for p in providers))
        children=[p.process for p in providers]
        self.assertEqual(len({p.pid for p in children}),3)
        for provider in providers:
            for _ in range(100):provider.snapshot()
            self.assertEqual(provider.launch_count,3)
            provider.close()
        self.assertTrue(all(p.poll() is not None for p in children))

    def test_thermal_failure_cannot_change_cpu_ram_or_admission_sampler(self):
        cpu=namedtuple('CPU','user system idle')
        provider=Mock()
        provider.snapshot.side_effect=RuntimeError('PRIVATE')
        sampler=Sampler(thermal=provider,cpu_times=Mock(side_effect=[cpu(10,10,80),cpu(20,20,160)]),
                        virtual_memory=lambda:SimpleNamespace(total=1000,available=250),
                        swap_memory=None,footprint=None,monotonic=Mock(side_effect=[1.,2.]))
        first,base=sampler._machine();sampler.machine_baseline=base
        second,_=sampler._machine()
        self.assertEqual(second['ram_used_bytes'],750)
        self.assertAlmostEqual(second['cpu_percent'],20)
        self.assertEqual(second['thermal']['quality'],'unavailable')
        self.assertNotIn('PRIVATE',str(second))
        sampler.close();provider.close.assert_called_once()
        from cmw.jobs.sharing import AdmissionSampler
        with patch('cmw.jobs.telemetry.ThermalProvider.from_environment',side_effect=AssertionError('No authority provider')):
            admitted=AdmissionSampler()
        self.assertIsNone(admitted.sampler.thermal)

    def test_temporary_activity_sampler_is_closed_even_on_failure(self):
        sampler=Mock();sampler.sample.side_effect=RuntimeError('fixture')
        state={'jobs':[],'controller':{'online':False,'stale':False,'dispatch':False}}
        guard={'state':'NO_MATCH','observations':[],'stale':False,'reason':'fixture'}
        observer=Mock();observer.scan.return_value=guard
        with patch('cmw.jobs.telemetry.Sampler',return_value=sampler),self.assertRaises(RuntimeError):
            activity.project(Mock(),state=state,observer=observer)
        sampler.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
