"""Host thermal presentation uses synthetic observations, never a live sensor."""
from copy import deepcopy
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.jobs.cli import machine_detail, machine_text
from cmw.jobs.snapshot import capture_snapshot, render_snapshot


def thermal(*, age=12., quality='fresh', temperature=63.2, now=112.):
    return dict(temperature_celsius=temperature, temperature_kind='average CPU sensors',
                source='macmon', units='Celsius', sample_timestamp=now-age, sample_age=age,
                quality=quality, availability_reason='Synthetic sensor fixture',
                freshness_seconds=30., unavailable_seconds=60.)


def machine(*, temp=63.2, cpu=71., ram=27.1, total=36., age=12., quality='fresh', now=112.):
    return dict(cpu_percent=cpu, cpu_quality='fresh', memory_quality='fresh',
                ram_used_bytes=ram*2**30, ram_total_bytes=total*2**30,
                ram_available_bytes=(total-ram)*2**30, memory_source='psutil.virtual_memory()',
                observed_at=now, age_seconds=0., stale=False,
                thermal=thermal(age=age, quality=quality, temperature=temp, now=now))


class MachineThermalFormatTests(unittest.TestCase):
    def test_exact_requested_fresh_and_missing_strings(self):
        value = machine()
        self.assertEqual(machine_text(value), 'Machine: CPU 71%; avg 63°C RAM 27.1 / 36.0 GiB')
        value.pop('thermal')
        self.assertEqual(machine_text(value), 'Machine: CPU 71%; avg — RAM 27.1 / 36.0 GiB')
        self.assertNotIn('[psutil T−avail]', machine_text(value))

    def test_existing_cpu_ram_quality_and_precision(self):
        for cpu_quality, expected in (('warming-up', '…'), ('unavailable', '—')):
            value = machine(); value.update(cpu_percent=None, cpu_quality=cpu_quality)
            self.assertEqual(machine_text(value), f'Machine: CPU {expected}; avg 63°C RAM 27.1 / 36.0 GiB')
        value = machine(); value['ram_used_bytes'] = None
        self.assertEqual(machine_text(value), 'Machine: CPU 71%; avg 63°C RAM —')
        value = machine(); value['stale'] = True
        self.assertEqual(machine_text(value), 'Machine: CPU 71%~; avg 63°C RAM 27.1 / 36.0 GiB~')

    def test_thermal_stale_expired_and_null_are_distinct(self):
        self.assertEqual(machine_text(machine(age=40., quality='stale')), 'Machine: CPU 71%; avg 63°C~ RAM 27.1 / 36.0 GiB')
        self.assertIn('avg — RAM', machine_text(machine(age=61.)))
        for value in (None, {}, {'temperature_celsius': 63.}):
            self.assertIn('avg — RAM', machine_text({**machine(), 'thermal': value}))

    def test_invalid_or_unsupported_samples_never_display_as_cpu_average(self):
        for bad in (True, False, math.nan, math.inf, -math.inf, 0., -5., 151., '63'):
            self.assertIn('avg — RAM', machine_text(machine(temp=bad)))
        for field, bad in (('units', 'Fahrenheit'), ('temperature_kind', 'GPU'), ('source', 'unknown')):
            value = machine(); value['thermal'][field] = bad
            self.assertIn('avg — RAM', machine_text(value))

    def test_details_preserve_memory_semantics_and_independent_thermal_clock(self):
        text = machine_detail(machine())
        for token in ('psutil T−avail', 'total − available', 'platform', 'reclaimable/cache',
                      'psutil.virtual_memory()', 'System swap used', 'Activity Monitor',
                      'average CPU sensors', 'Celsius', 'macmon', 'fresh', '12.0s', 'not atomic'):
            self.assertIn(token, text)

    def test_details_explain_provider_recovery_without_changing_machine_line(self):
        value = machine(temp=None, quality='unavailable')
        value['thermal'].update(
            provider_status='disabled', provider_version='0.10.0', observation_status='unavailable',
            failure_category='sample_timeout', consecutive_failures=3, successful_samples=2,
            retry_after_seconds=None, retry_guidance='Close and reopen the observer after correcting the problem.',
            last_successful_observation=dict(temperature_celsius=63.2, sample_timestamp=100., sample_age=145.))
        text = machine_detail(value)
        for token in ('Thermal provider: disabled', 'version: 0.10.0', 'observation: unavailable',
                      '3 consecutive', 'category: sample_timeout', 'successful samples: 2',
                      'historical, not current evidence', '63.2 Celsius', 'reported age: 145.0s',
                      'Close and reopen'):
            self.assertIn(token, text)
        self.assertNotIn('Thermal retry delay', text)
        self.assertEqual(machine_text(value), 'Machine: CPU 71%; avg — RAM 27.1 / 36.0 GiB')
        value['thermal'].update(provider_status='backoff', consecutive_failures=1, retry_after_seconds=12.5)
        self.assertIn('Thermal retry delay (reported): 12.5s.', machine_detail(value))

    def test_details_bound_and_clean_recovery_text(self):
        value = machine()
        value['thermal'].update(retry_guidance='\x1b[31m' + 'x' * 300 + '\nprivate overflow')
        text = machine_detail(value)
        self.assertIn('Thermal recovery: ' + 'x' * 256, text)
        self.assertNotIn('x' * 257, text)
        self.assertNotIn('private overflow', text)
        self.assertNotIn('\x1b', text)

    def test_copy_keeps_recovery_and_identity_with_explicit_nested_allowlists(self):
        value = machine(temp=None, quality='unavailable')
        value['thermal'].update(
            provider_status='disabled', provider_version='0.10.0', failure_category='sensors_unavailable',
            consecutive_failures=3, successful_samples=1, retry_after_seconds=None,
            retry_guidance='Close and reopen the observer.',
            last_successful_observation=dict(temperature_celsius=63.2, sample_timestamp=100., sample_age=145.,
                                             raw='private sensor dump', path='/private/provider/source'),
            provider_identity=dict(device=1, inode=22, size=1000, modified_ns=123, changed_ns=124,
                                   owner_uid=501, group_gid=20, mode=33261, sha256='a' * 64,
                                   executable='/private/provider/executable', raw='private identity dump'))
        job = dict(id=1, display_id='J1.1', attempt_id='immutable', name='Synthetic', engine='VASP', status='Hold')
        snap = capture_snapshot(job, machine_usage=value, captured_at=245., runtime={})
        copied = snap.to_dict()['machine_usage']['thermal']
        self.assertEqual(copied['provider_status'], 'disabled')
        self.assertEqual(copied['consecutive_failures'], 3)
        self.assertEqual(copied['last_successful_observation'],
                         dict(temperature_celsius=63.2, sample_timestamp=100., sample_age=145.))
        self.assertEqual(copied['provider_identity']['inode'], 22)
        self.assertEqual(copied['provider_identity']['sha256'], 'a' * 64)
        for mode in ('summary', 'diagnostics', 'json'):
            text = render_snapshot(snap, mode=mode)
            self.assertIn('sensors_unavailable', text)
            self.assertNotIn('private', text)
        value['thermal']['provider_identity']['inode'] = 23
        self.assertEqual(snap.to_dict()['machine_usage']['thermal']['provider_identity']['inode'], 22)

    def test_copy_freezes_host_telemetry_without_scientific_or_private_fields(self):
        value = machine(); value['thermal'].update(stderr='private raw stderr', argv=['private command'])
        job = dict(id=1, display_id='J1.1', attempt_id='immutable', name='Synthetic', engine='VASP', status='Hold')
        snap = capture_snapshot(job, machine_usage=value, captured_at=112., runtime={})
        value['thermal']['temperature_celsius'] = 76.
        self.assertEqual(snap.to_dict()['machine_usage']['thermal']['temperature_celsius'], 63.2)
        self.assertIsNone(snap.to_dict()['progress'])
        for mode in ('summary', 'diagnostics', 'json'):
            text = render_snapshot(snap, mode=mode)
            self.assertIn('macmon', text)
            self.assertNotIn('private raw stderr', text)
            self.assertNotIn('private command', text)
        self.assertIn('avg 63°C', render_snapshot(snap))


try:
    from cmw.jobs.tui import JobsApp
    from cmw.nodes.portal import NodesApp, row_key
    from cmw.progress.service import ObservationService
    from cmw.jobs.progress import resolve_context
    from textual.widgets import DataTable, Static
except ImportError:
    JobsApp = None


def save_render(app, name):
    root = os.environ.get('CMW_THERMAL_RENDERS')
    if root:
        directory = Path(root); directory.mkdir(parents=True, exist_ok=True)
        (directory / (name + '.svg')).write_text(app.export_screenshot())
        (directory / (name + '.card.txt')).write_text(str(app.query_one('#progress', Static).render()))


@unittest.skipIf(JobsApp is None, 'Optional Jobs UI extra unavailable')
class MachineThermalTuiTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, pilot, predicate):
        for _ in range(80):
            if predicate():
                return
            await pilot.pause(.05)
        self.fail('Synthetic frontend observation did not settle')

    async def test_local_actual_views_keep_frozen_scientific_card_and_rows(self):
        from tests.jobs import ui_polish_acceptance as acceptance
        sizes = ((77, 24), (80, 24), (100, 30), (140, 40))
        with tempfile.TemporaryDirectory() as temporary:
            store, cases = acceptance.make_fixture(Path(temporary) / 'fixture')
            state, selected = acceptance.scenario_state(store.snapshot(now=acceptance.NOW), cases, 'vasp_active')
            projection = acceptance.scenario_projection(state, 'vasp_active')
            sources = {job['id']: paths for job, paths in cases.values()}
            def resolver(selected_store, job_id, **kwargs):
                kwargs['sources'] = sources.get(job_id, ())
                return resolve_context(selected_store, job_id, **kwargs)
            service = ObservationService(clock=lambda: acceptance.NOW)
            app = JobsApp(store, clock=lambda: acceptance.NOW, progress_service=service, progress_resolver=resolver)
            app.selected_id = selected['display_id']
            app.activity_projection = deepcopy(projection)
            app.job_usage = deepcopy(projection['job_usage'])
            before = {path: path.read_bytes() for path in store.root.rglob('*') if path.is_file()}
            with patch.object(store, 'snapshot', side_effect=lambda **_: deepcopy(state)), \
                    patch.object(JobsApp, 'collect_activity'), \
                    patch('time.time', return_value=acceptance.NOW):
                async with app.run_test(size=sizes[0]) as pilot:
                    await acceptance.settle(app, pilot)
                    app.query_one('#table', DataTable).move_cursor(row=app.row_ids.index(selected['display_id']), animate=False)
                    await pilot.pause()
                    for size in sizes:
                        await pilot.resize_terminal(*size)
                        await pilot.pause()
                        baseline = str(app.query_one('#progress', Static).render())
                        for quality, age, shown in (('fresh', 12., '63°C'), ('stale', 40., '63°C~'), ('unavailable', 61., '—')):
                            app.activity_projection['machine_usage'] = machine(now=acceptance.NOW, age=age, quality=quality)
                            app.refresh_state()
                            await pilot.pause()
                            line = str(app.query_one('#machine', Static).render())
                            self.assertEqual(line, f'Machine: CPU 71%; avg {shown} RAM 27.1 / 36.0 GiB')
                            self.assertEqual(app.query_one('#machine').size.height, 1)
                            self.assertEqual(str(app.query_one('#progress', Static).render()), baseline)
                            self.assertGreaterEqual(app.query_one('#table').size.height, 4)
                            self.assertLessEqual(app.query_one('#command-bar').region.bottom, size[1])
                            for token in ('┌─', 'SCF', 'FORCE', 'ELECTRONIC', 'OUTCAR SCF', 'eV/Å', 'EDIFF', 'source-local'):
                                self.assertIn(token, baseline)
                            save_render(app, f'local-{quality}-{size[0]}x{size[1]}')
            self.assertEqual(before, {path: path.read_bytes() for path in store.root.rglob('*') if path.is_file()})

    async def test_portal_two_nodes_render_own_readings_disconnect_copy_and_reconnect(self):
        from tests.nodes.test_portal import FakeBackend
        class ThermalBackend(FakeBackend):
            def __init__(self, alias, **kwargs):
                super().__init__(alias, **kwargs)
                self.machine = machine(temp=63.2 if alias == 'MBP' else 76.4,
                    cpu=71. if alias == 'MBP' else 88., ram=27.1 if alias == 'MBP' else 51.2,
                    total=36. if alias == 'MBP' else 64.)
            def call(self, operation, *args, **kwargs):
                result = super().call(operation, *args, **kwargs)
                if operation == 'status':
                    result['result']['state']['machine_usage'] = deepcopy(self.machine)
                return result
        backends = {'MBP': ThermalBackend('MBP'), 'MINI': ThermalBackend('MINI', remote=True)}
        app = NodesApp(backends, clock=lambda: 1000.)
        async with app.run_test(size=(77, 24)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            job = backends['MINI'].jobs[0]
            app.select(row_key(job['target'])); app.collect_progress()
            await self.until(pilot, lambda: app.selected_snapshot('MINI', job) is not None)
            for size in ((77, 24), (80, 24), (100, 30), (140, 40)):
                await pilot.resize_terminal(*size); await pilot.pause()
                app.render_state(); await pilot.pause()
                baseline = str(app.query_one('#progress', Static).render())
                for quality, age, shown in (('fresh', 12., '76°C'), ('stale', 40., '76°C~'), ('unavailable', 61., '—')):
                    for owner in backends:
                        record = app.lanes[owner].state['machine_usage']['thermal']
                        record.update(quality=quality, sample_age=age, sample_timestamp=112.-age)
                    app.render_state(); await pilot.pause()
                    summary = str(app.query_one('#node-summary', Static).render())
                    self.assertIn('MINI Online 0s', summary)
                    self.assertIn(f'CPU 88%; avg {shown} RAM 51.2 / 64.0 GiB', summary)
                    self.assertIn('CPU 71%; avg ' + ('63°C' if quality == 'fresh' else '63°C~' if quality == 'stale' else '—'), summary)
                    self.assertTrue(all(len(line) <= size[0]-2 for line in summary.splitlines()), summary)
                    self.assertEqual(str(app.query_one('#progress', Static).render()), baseline)
                    self.assertGreaterEqual(app.query_one('#table').size.height - 1, 3)
                    self.assertIn('MINI / research / J1.1', str(app.query_one('#selected', Static).render()))
                    save_render(app, f'portal-{quality}-{size[0]}x{size[1]}')
            app.lanes['MINI'].error = 'Synthetic SSH unavailable'
            app.render_state(); await pilot.pause()
            self.assertIn('MINI Unreachable 0s', str(app.query_one('#node-summary', Static).render()))
            self.assertIn('avg —', machine_text(app.machine_usage('MINI')))
            snap = app.capture_copy_snapshot().to_dict()
            self.assertEqual(snap['node_origin']['alias'], 'MINI')
            self.assertEqual(snap['machine_usage']['thermal']['quality'], 'unavailable')
            self.assertEqual(snap['machine_usage']['thermal']['source'], 'macmon')
            self.assertIsNotNone(snap['progress'])
            self.assertIn('Last reported Run', str(app.query_one('#selected', Static).render()))
            app.lanes['MINI'].next_poll = 0
            await self.until(pilot, lambda: app.lanes['MINI'].error is None)
            self.assertEqual(machine_text(app.machine_usage('MINI')), 'Machine: CPU 88%; avg 76°C RAM 51.2 / 64.0 GiB')
            self.assertEqual(app.selected()[1]['target'], job['target'])

    async def test_local_unmount_awaits_owned_hung_provider_cleanup(self):
        from cmw.jobs.store import Store
        from cmw.jobs.telemetry import Sampler
        from cmw.jobs.thermal import ThermalProvider
        from tests.jobs.thermal_fixtures import write_fake_macmon
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = ThermalProvider(write_fake_macmon(root / 'macmon', mode='hang'),
                system='Darwin', machine='arm64')
            provider.snapshot()
            sampler = Sampler(thermal=provider)
            app = JobsApp(Store(root / 'state'), sampler=sampler)
            try:
                with patch.object(app, 'collect_activity'):
                    async with app.run_test(size=(77, 24)) as pilot:
                        await self.until(pilot, lambda: provider.launch_count == 3)
                        owned = provider.process
                        self.assertIsNotNone(owned)
                        self.assertIsNone(owned.poll())
                self.assertFalse(provider.thread.is_alive())
                self.assertIsNotNone(owned.poll())
            finally:
                provider.close()

    async def test_single_node_main_keeps_exact_machine_formatter(self):
        from tests.nodes.test_portal import FakeBackend
        backend = FakeBackend('MINI', remote=True)
        original = backend.call
        def call(operation, *args, **kwargs):
            result = original(operation, *args, **kwargs)
            if operation == 'status':
                result['result']['state']['machine_usage'] = machine()
            return result
        backend.call = call
        app = NodesApp({'MINI': backend}, clock=lambda: 1000.)
        async with app.run_test(size=(77, 24)) as pilot:
            await self.until(pilot, lambda: app.lanes['MINI'].state is not None)
            self.assertEqual(str(app.query_one('#node-summary', Static).render()).splitlines()[1],
                'Machine: CPU 71%; avg 63°C RAM 27.1 / 36.0 GiB')
            self.assertLessEqual(app.query_one('#node-summary').size.height, 2)
        self.assertTrue(backend.closed)

    async def test_transport_eof_immediately_marks_cached_thermal_and_node_stale(self):
        from cmw.nodes.protocol import NodeError
        from tests.nodes.test_portal import FakeBackend
        backend = FakeBackend('MINI', remote=True)
        original = backend.call
        def call(operation, *args, **kwargs):
            result = original(operation, *args, **kwargs)
            if operation == 'status':
                result['result']['state']['machine_usage'] = machine()
            return result
        backend.call = call
        app = NodesApp({'MINI': backend}, clock=lambda: 1000.)
        async with app.run_test(size=(77, 24)) as pilot:
            await self.until(pilot, lambda: app.lanes['MINI'].state is not None)
            self.assertIn('avg 63°C RAM', machine_text(app.machine_usage('MINI')))
            app.accept('MINI', ('status', {}, None, None), None,
                       NodeError('DISCONNECTED', 'Synthetic endpoint stdout EOF'))
            self.assertIsNotNone(app.lanes['MINI'].error)
            self.assertFalse(app.lanes['MINI'].fresh(app.clock()))
            self.assertIn('avg 63°C~ RAM', machine_text(app.machine_usage('MINI')))
            self.assertIn('Unreachable', str(app.query_one('#node-summary', Static).render()))
            self.assertIn('Last reported Run', str(app.query_one('#selected', Static).render()))
            copied = app.capture_copy_snapshot().to_dict()
            self.assertEqual(copied['machine_usage']['thermal']['quality'], 'stale')
            self.assertEqual(copied['machine_usage']['thermal']['sample_timestamp'], 100.)
            self.assertIn('node unreachable', copied['node_origin']['connection'])
