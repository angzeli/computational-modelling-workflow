"""Doctor routes over synthetic jobs/nodes; no scientific engines or real hosts."""
from __future__ import annotations

from tests.ui_readiness import await_selection_settled

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cmw.jobs.store import Store
from cmw.nodes.protocol import NodeError
from tests.jobs.test_navigation_ui import SyntheticObserver, SyntheticSampler, table_view
from tests.nodes.test_portal import FakeBackend

try:
    from textual.containers import VerticalScroll
    from textual.widgets import DataTable, Static, TextArea
    from cmw.doctor.presentation import DoctorDetails, DoctorReport
    from cmw.jobs.tui import Inspect, JobsApp
    from cmw.nodes.portal import NodesApp, row_key, target_key
except ImportError:
    JobsApp = None


SIZES = ((77, 24), (80, 24), (100, 30), (140, 40))


def report_text(report):
    """Deliberately long, literal text exercises scrolling and endpoint labels."""
    caller = report.get('caller_runtime', report.get('runtime', {}))
    endpoint = report.get('endpoint_runtime', {})
    return ('Caller runtime: ' + caller.get('identity', caller).get('fingerprint', 'unknown') + '\n'
            + 'Endpoint runtime: ' + endpoint.get('identity', endpoint).get('fingerprint', 'not selected') + '\n'
            + '\n'.join(f'Finding {number:02}: read-only evidence [literal] → next safe check'
                        for number in range(80)) + '\nEND OF DOCTOR REPORT')


class DoctorBackend(FakeBackend):
    def __init__(self, alias, *, remote=False):
        super().__init__(alias, remote=remote)
        self.connection.update(host_id='darwin:' + alias, runtime_id='endpoint-' + alias)
        self.doctor_gate = None
        self.doctor_error = None
        for job in self.jobs:
            job['resources'].update(mpi_ranks=8, threads_per_rank=1)
            job['reason'] = ''

    def call(self, operation, params=None, target=None, **kwargs):
        if operation != 'doctor.inspect':
            envelope = super().call(operation, params, target, **kwargs)
            envelope['clock_id'] = '1' * 64
            return envelope
        self.calls.append((operation, deepcopy(params), deepcopy(target)))
        if self.doctor_gate is not None and not self.doctor_gate.wait(5):
            raise TimeoutError('Synthetic doctor barrier expired')
        if self.doctor_error is not None:
            raise self.doctor_error
        if self.fail:
            raise NodeError('UNREACHABLE', 'synthetic unavailable node')
        self.sequence += 1
        runtime = {'fingerprint': 'endpoint-' + self.alias, 'version': 'test'}
        return {'schema': 1, 'node_id': self.node_id, 'queue_id': self.queue_id,
                'queue_alias': self.queue, 'session_id': self.session,
                'sequence': self.sequence, 'observed_at': 112., 'clock_id': '1' * 64,
                'runtime': runtime, 'host': {'platform': 'darwin', 'hardware_id': self.alias},
                'result': {'report': {'schema': 1, 'scope': 'job',
                    'runtime': {'identity': runtime}, 'target': deepcopy(target),
                    'worker': {'status': 'UNKNOWN', 'runtime': None},
                    'capabilities': [], 'findings': [], 'scientific_policy': 'NOT_ASSESSED',
                    'observation': {'status': 'OBSERVED', 'observed_at': 112.,
                        'coverage': 'Synthetic bounded UI fixture', 'transport': {'status': 'LOCAL'},
                        'progress': None, 'binding': None, 'controller': None}}}}


@unittest.skipIf(JobsApp is None, 'Doctor navigation requires the optional Jobs console')
class DoctorUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cmw-doctor-ui-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.main_thread = threading.get_ident()
        formatter = patch('cmw.doctor.service.format_report', side_effect=report_text)
        formatter.start()
        self.addCleanup(formatter.stop)
        runtime = patch('cmw.doctor.service.inspect_runtime',
                        return_value={'fingerprint': 'actual-frontend', 'version': 'test'})
        self.runtime = runtime.start()
        self.addCleanup(runtime.stop)

    async def until(self, pilot, predicate):
        for _ in range(200):
            if predicate():
                await pilot.pause()
                return
            await asyncio.sleep(.02)
        self.fail(f'Doctor UI event barrier did not complete: {type(pilot.app.screen).__name__}; '
                  f'pending={getattr(pilot.app.screen, "pending", None)}')

    def render(self, app, kind, size):
        destination = os.environ.get('CMW_DOCTOR_UI_RENDER_DIR')
        if destination:
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True)
            app.save_screenshot(f'doctor-{kind}-{size[0]}x{size[1]}.svg', path=str(directory))

    def assert_visible_report(self, app, size):
        area = app.screen.query_one(TextArea)
        button = app.screen.query_one('#copy-all')
        self.assertGreaterEqual(area.region.height, 8)
        self.assertGreaterEqual(area.region.x, 0)
        self.assertLessEqual(area.region.right, size[0])
        self.assertLessEqual(button.region.bottom, size[1])
        self.assertIn('[literal]', area.text)

    async def test_local_details_doctor_sizes_target_navigation_and_read_only(self):
        store = Store(self.root / 'state')
        jobs = [store.add(argv=['/unexecuted/fixture'], cwd=self.root,
                          name=f'Doctor held fixture {i}', hold=True) for i in range(25)]
        job = jobs[19]
        before_bytes = store.path.read_bytes()
        before_files = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        calls = []

        def inspect(**kwargs):
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            calls.append(kwargs)
            return {'schema': 1, 'runtime': {'fingerprint': 'loaded-local'}, 'findings': []}

        with patch('cmw.doctor.service.inspect_local', side_effect=inspect):
            app = JobsApp(store, observer=SyntheticObserver(), sampler=SyntheticSampler())
            async with app.run_test(size=SIZES[0]) as pilot:
                await self.until(pilot, lambda: app.state is not None and bool(app.activity_projection))
                await pilot.press('home', *(['down'] * 19))
                await self.until(pilot, lambda: app.selected_id == 'J20.1')
                for size in SIZES:
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    base = app.screen
                    table = app.query_one('#table', DataTable)
                    selected = app.selected()
                    await await_selection_settled(app, selected['id'], selected['attempt_id'])
                    before = table_view(table)
                    machine = str(app.query_one('#machine', Static).render())
                    await pilot.press('enter')
                    self.assertIsInstance(app.screen, Inspect)
                    parent = app.screen
                    await pilot.press('pagedown')
                    self.render(app, 'local-details', size)
                    scroll = parent.query_one(VerticalScroll).scroll_y
                    await pilot.press('d')
                    await self.until(pilot, lambda: isinstance(app.screen, DoctorReport) and not app.screen.pending)
                    self.assertEqual(app.screen.selection, (str(store.root), job['id'], job['attempt_id']))
                    self.assertIn('Caller runtime: loaded-local', app.screen.query_one(TextArea).text)
                    self.assert_visible_report(app, size)
                    self.render(app, 'local-report', size)
                    await pilot.press('pagedown')
                    await pilot.pause()
                    self.assertGreater(app.screen.query_one(TextArea).scroll_y, 0,
                                       f'focus={app.focused!r}; max={app.screen.query_one(TextArea).max_scroll_y}')
                    await pilot.press('escape')
                    await self.until(pilot, lambda: app.screen is parent and not app.navigation_pending(parent))
                    self.assertEqual(parent.query_one(VerticalScroll).scroll_y, scroll)
                    await pilot.press('escape')
                    await self.until(pilot, lambda: app.screen is base and not app.navigation_pending(base))
                    self.assertEqual(table_view(table), before)
                    self.assertEqual(str(app.query_one('#machine', Static).render()), machine)
        self.assertEqual(calls, [{'state': str(store.root), 'job_id': job['id'],
                                'expected_attempt_id': job['attempt_id']}] * len(SIZES))
        self.assertEqual(store.path.read_bytes(), before_bytes)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')), before_files)

    async def test_remote_details_doctor_sizes_distinguish_caller_and_owner(self):
        backends = {'LOCAL': DoctorBackend('LOCAL'), 'REMOTE': DoctorBackend('REMOTE', remote=True)}
        app = NodesApp(backends, clock=lambda: 1000.)
        target = deepcopy(backends['REMOTE'].jobs[2]['target'])
        def inspect_caller(*args, **kwargs):
            self.assertFalse(args, 'Remote state must not be inspected on the frontend')
            self.assertFalse(kwargs, 'Remote paths must stay on their owner')
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            return {'schema': 1, 'runtime': {'identity': {'fingerprint': 'actual-frontend'}},
                    'capabilities': [], 'findings': [], 'observation': {}}

        with patch('cmw.doctor.service.inspect_local', side_effect=inspect_caller):
            async with app.run_test(size=SIZES[0]) as pilot:
                await self.until(pilot, lambda: len(app.rows) == 10)
                startup_capabilities = {owner: [call for call in backend.calls if call[0] == 'capabilities']
                                        for owner, backend in backends.items()}
                for calls in startup_capabilities.values():
                    self.assertEqual(calls, [('capabilities', {}, None)])
                app.select(row_key(target))
                app.render_state()
                app.collect_progress()
                await self.until(pilot, lambda: app.selected_snapshot('REMOTE', backends['REMOTE'].jobs[2]) is not None)
                await self.until(pilot, lambda: not app.navigation_pending())
                for size in SIZES:
                    await pilot.resize_terminal(*size)
                    app.render_state()
                    base = app.screen
                    # Measure the baseline after the resized card and table settle.
                    await self.until(pilot, lambda: app.viewport == size and not app.navigation_pending(base))
                    table = app.query_one('#table', DataTable)
                    table.scroll_to(y=max(0, table.cursor_row - 2), animate=False, immediate=True)
                    await pilot.pause()
                    before = table_view(table)
                    await pilot.press('enter')
                    self.assertIsInstance(app.screen, DoctorDetails)
                    parent = app.screen
                    self.render(app, 'remote-details', size)
                    await pilot.press('d')
                    await self.until(pilot, lambda: isinstance(app.screen, DoctorReport) and not app.screen.pending)
                    self.assertEqual(app.screen.selection, target_key(target))
                    self.assertIsNotNone(app.screen.report, app.screen.query_one(TextArea).text)
                    self.assertEqual({key: app.screen.report['target'][key] for key in target}, target)
                    text = app.screen.query_one(TextArea).text
                    self.assertIn('REMOTE / research / J3.1', text)
                    self.assertIn('Caller runtime: actual-frontend', text)
                    self.assertIn('Endpoint runtime: endpoint-REMOTE', text)
                    self.assert_visible_report(app, size)
                    self.render(app, 'remote-report', size)
                    await pilot.press('escape')
                    await self.until(pilot, lambda: app.screen is parent and not app.navigation_pending(parent))
                    await pilot.press('escape')
                    await self.until(pilot, lambda: app.screen is base and not app.navigation_pending(base))
                    self.assertEqual(table_view(table), before)
                    self.assertEqual(app.selected_key, row_key(target))
        calls = [call for call in backends['REMOTE'].calls if call[0] == 'doctor.inspect']
        self.assertEqual(calls, [('doctor.inspect', {}, target)] * len(SIZES))
        self.assertFalse(any(call[0] == 'doctor.inspect' for call in backends['LOCAL'].calls))
        for owner, backend in backends.items():
            self.assertEqual([call for call in backend.calls if call[0] == 'capabilities'],
                             startup_capabilities[owner], 'Doctor actions must not repeat the startup handshake')

    async def test_slow_closed_remote_report_cannot_retarget_or_reopen(self):
        backends = {'LOCAL': DoctorBackend('LOCAL'), 'REMOTE': DoctorBackend('REMOTE', remote=True)}
        gate = threading.Event()
        self.addCleanup(gate.set)
        backends['REMOTE'].doctor_gate = gate
        app = NodesApp(backends)
        target = deepcopy(backends['REMOTE'].jobs[0]['target'])
        async with app.run_test(size=(80, 24)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            app.select(row_key(target))
            app.render_state()
            await pilot.press('enter', 'd')
            await self.until(pilot, lambda: any(call[0] == 'doctor.inspect' for call in backends['REMOTE'].calls))
            self.assertIsInstance(app.screen, DoctorReport)
            self.assertTrue(app.screen.pending)
            await pilot.press('escape', 'escape')
            app.select(row_key(backends['LOCAL'].jobs[1]['target']))
            app.render_state()
            gate.set()
            await self.until(pilot, lambda: not app.lanes['REMOTE'].busy)
            self.assertIs(app.screen, app.screen_stack[0])
            self.assertEqual(app.selected()[0], 'LOCAL')
            self.assertEqual([call[2] for call in backends['REMOTE'].calls if call[0] == 'doctor.inspect'], [target])
            self.runtime.assert_not_called()

    async def test_old_endpoint_and_cached_remote_evidence_are_explicit(self):
        backend = DoctorBackend('REMOTE', remote=True)
        app = NodesApp({'REMOTE': backend})
        caller = patch('cmw.doctor.service.inspect_local', return_value={
            'runtime': {'identity': {'fingerprint': 'actual-frontend'}},
            'capabilities': [], 'findings': [], 'observation': {},
            'worker': {'status': 'UNKNOWN'}})
        caller.start()
        self.addCleanup(caller.stop)
        async with app.run_test(size=(77, 24)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 5)
            backend.doctor_error = NodeError('UNSUPPORTED_OPERATION', 'Doctor is unsupported by this old endpoint')
            await pilot.press('enter', 'd')
            await self.until(pilot, lambda: isinstance(app.screen, DoctorReport) and not app.screen.pending)
            self.assertIsNotNone(app.screen.report, app.screen.query_one(TextArea).text)
            self.assertIn('UNSUPPORTED_OPERATION', [item['code'] for item in app.screen.report['findings']])
            self.assertEqual(app.screen.report['worker']['status'], 'UNKNOWN')
            self.assertEqual(app.screen.report['endpoint_runtime']['qualification'], 'LAST_REPORTED')
            self.assertEqual(app.screen.report['observation']['status'], 'CACHED')
            self.assertEqual(app.screen.report['target']['attempt_id'], backend.jobs[0]['attempt_id'])
            await pilot.press('escape', 'escape')
            # Dispatch from a fresh Details view, then lose transport on the inspection.
            backend.doctor_error = NodeError('UNREACHABLE', 'synthetic transport stale')
            await pilot.press('enter', 'd')
            await self.until(pilot, lambda: isinstance(app.screen, DoctorReport) and not app.screen.pending)
            self.assertIn('cached', app.screen.query_one(TextArea).text.lower())
            self.assertIsNotNone(app.screen.report, app.screen.query_one(TextArea).text)
            self.assertIn('REMOTE_OBSERVATION_CACHED', [item['code'] for item in app.screen.report['findings']])
            self.assertIn('UNREACHABLE', [item['code'] for item in app.screen.report['findings']])
            self.assertEqual(app.screen.report['worker']['status'], 'UNKNOWN')
            self.assertTrue(app.screen.report['observation']['transport']['cached'])
            self.assertEqual(app.screen.report['endpoint_runtime']['qualification'], 'LAST_REPORTED')
            self.assertEqual(app.screen.report['target']['attempt_id'], backend.jobs[0]['attempt_id'])
