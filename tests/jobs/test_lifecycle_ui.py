"""Selected-attempt lifecycle facts remain separate from stored and scientific state."""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.store import Store
from cmw.progress.models import Metric, MetricSeries, ProgressSnapshot, RunContext

try:
    from cmw.jobs.tui import JobsApp, Inspect, NoteEditor
    from textual.widgets import DataTable, Static
except ImportError:
    JobsApp = None


@dataclass(frozen=True)
class FixedObservation:
    job_id: int
    attempt_id: str
    observed_execution: str
    observed_at: float
    reconciliation_required: bool = False
    suspension: dict | None = None

    def to_dict(self, now=None):
        age = max(0, (self.observed_at if now is None else now) - self.observed_at)
        return dict(job_id=self.job_id, attempt_id=self.attempt_id, persisted_status='Run',
                    observed_execution=self.observed_execution, observed_at=self.observed_at,
                    age_seconds=age, stale=age > 5, observation_quality='qualified',
                    reconciliation_required=self.reconciliation_required,
                    evidence={'controller': {'online': False},
                              **({'suspension': self.suspension} if self.suspension is not None else {})},
                    limitations=['Worker and anchor only; members not enumerated'])


@unittest.skipIf(JobsApp is None, 'Console tests require the optional Jobs extra')
class LifecycleUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-lifecycle-ui-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'state')
        for index in range(3):
            self.store.add(argv=['/unexecuted/fixture'], cwd=self.root,
                           name=f'Fictional detached attempt {index}', engine='UNSUPPORTED', hold=True)
        with self.store.transaction() as con:
            for job_id in (1, 2):
                job = self.store.get(con, job_id)
                job.update(status='Run', order=None, started_at=100, finished_at=None)
                self.store.save(con, job)
        self.jobs = self.store.snapshot()['jobs']
        self.now = 100000
        self.main_thread = threading.get_ident()
        for method in ('collect_activity', 'collect_progress'):
            mocked = patch.object(JobsApp, method)
            mocked.start()
            self.addCleanup(mocked.stop)

    def provider(self, category='VERIFIED_WORKER'):
        def observe(store, job, *, state=None, now=None):
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            self.assertIs(store, self.store)
            self.assertEqual(set(state), {'schema', 'controller'})
            self.assertFalse(state['controller']['online'])
            return FixedObservation(job['id'], job['attempt_id'], category, now,
                                    category == 'WORKER_MISSING')
        return Mock(side_effect=observe)

    def app(self, provider):
        app = JobsApp(self.store, clock=lambda: self.now, execution_provider=provider)
        schedule = app.set_interval
        def set_interval(interval, callback=None, **kwargs):
            # Provider counts follow explicit fixture actions, not CI rendering speed.
            if callback == app.collect_execution:
                kwargs['pause'] = True
            timer = schedule(interval, callback, **kwargs)
            if callback == app.collect_execution:
                # Retain the paused timer until app shutdown and fixture cleanup.
                self.addCleanup(timer.stop)
            return timer
        mocked = patch.object(app, 'set_interval', side_effect=set_interval)
        mocked.start()
        self.addCleanup(mocked.stop)
        return app

    async def until(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail('Bounded fixture did not finish')

    async def settled(self, app, pilot):
        await self.until(lambda: app.execution_observation is not None and not app.execution_busy)
        await pilot.pause()

    def old_scientific_snapshot(self, app):
        job = app.selected()
        context = RunContext('fixture', 'UNSUPPORTED', job_id=job['id'], attempt_id=job['attempt_id'])
        return ProgressSnapshot(context, 'fixture', status='OBSERVED', phase='Fictional progress',
            metrics=(MetricSeries(Metric('fixture.value', 'Fictional value', 'Invented UI fixture',
                                        'arb.', 1.25)),),
            read_health='OK', last_new_evidence_at=self.now - 39600, completeness='COMPLETE')

    def card(self, app):
        return str(app.query_one('#execution', Static).render())

    async def test_detached_and_missing_worker_are_visible_at_all_required_sizes_without_mutation(self):
        before = self.store.path.read_bytes()
        for category in ('VERIFIED_WORKER', 'WORKER_MISSING'):
            provider = self.provider(category)
            app = self.app(provider)
            async with app.run_test(size=(77, 24)) as pilot:
                await self.settled(app, pilot)
                app.progress_snapshot = self.old_scientific_snapshot(app)
                app.render_progress()
                for width, height in ((77, 24), (80, 24), (140, 40)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    card = self.card(app)
                    self.assertIn('Stored: Run', card)
                    self.assertIn('Verified worker + anchor' if category == 'VERIFIED_WORKER'
                                  else 'UNVERIFIED — worker missing', card)
                    self.assertEqual('Reconciliation required' in card, category == 'WORKER_MISSING')
                    self.assertIn('Offline', str(app.query_one('#controller', Static).render()))
                    table = app.query_one('#table', DataTable)
                    self.assertEqual(str(table.get_cell('J1.1', 'status')), 'Run')
                    widget = app.query_one('#execution', Static)
                    self.assertEqual(widget.size.height, 2)
                    self.assertGreaterEqual(widget.region.y, table.region.bottom)
                    self.assertLessEqual(widget.region.bottom, height - 2)
                    self.assertGreaterEqual(table.size.height, 3)
                    if width < 110:
                        self.assertLessEqual(app.query_one('#shortcuts').region.bottom, height)
                    output = os.environ.get('CMW_LIFECYCLE_RENDER_DIR')
                    if output:
                        Path(output).mkdir(parents=True, exist_ok=True)
                        Path(output, f'{category.lower()}-{width}x{height}.svg').write_text(app.export_screenshot())
                await pilot.press('enter')
                self.assertIsInstance(app.screen, Inspect)
                details = str(app.screen.query_one('#body', Static).render())
                self.assertIn('EXECUTION OBSERVATION', details)
                self.assertIn(category, details)
                await pilot.press('escape', 'q')
            self.assertEqual(provider.call_count, 1)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    async def test_pending_and_unavailable_do_not_claim_fresh_execution(self):
        started, release = threading.Event(), threading.Event()
        def unavailable(*args, **kwargs):
            started.set()
            release.wait(5)
            raise PermissionError('Fictional denied observation')
        app = self.app(Mock(side_effect=unavailable))
        async with app.run_test(size=(77, 24)) as pilot:
            await self.until(started.is_set)
            self.assertIn('Execution: Observation pending', self.card(app))
            release.set()
            await self.until(lambda: not app.execution_busy)
            self.assertIn('Execution: Observation unavailable', self.card(app))
            self.assertIn('not freshly verified', self.card(app))
            await pilot.press('q')

    async def test_stale_observation_age_does_not_mutate_or_refresh_every_half_second(self):
        provider = self.provider()
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            before = self.store.path.read_bytes()
            self.now += 9
            for _ in range(4):
                app.refresh_state()
            self.assertIn('Execution: STALE', self.card(app))
            self.assertIn('Observed 9s ago', self.card(app))
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(before, self.store.path.read_bytes())
            await pilot.press('q')

    async def test_selection_attempt_worker_and_status_changes_discard_pending_results(self):
        for change in ('selection', 'selection-back', 'attempt', 'worker', 'status'):
            with self.subTest(change=change):
                started, release = threading.Event(), threading.Event()
                original = self.provider()
                def delayed(*args, **kwargs):
                    started.set()
                    release.wait(5)
                    return original(*args, **kwargs)
                provider = Mock(side_effect=delayed)
                app = self.app(provider)
                async with app.run_test(size=(77, 24)) as pilot:
                    await self.until(started.is_set)
                    first_key = app.execution_selection
                    if change.startswith('selection'):
                        await pilot.press('down')
                        if change == 'selection-back':
                            await pilot.press('up')
                    else:
                        with self.store.transaction() as con:
                            job = self.store.get(con, 1)
                            if change == 'attempt':
                                job['attempt_id'] = 'a' * 32
                            elif change == 'worker':
                                job['worker'] = {'pid': 9999999, 'birth': 1, 'host': 'fictional', 'boot': 1}
                            else:
                                job['status'] = 'Unknown'
                            self.store.save(con, job)
                        app.refresh_state()
                    for _ in range(5):
                        app.collect_execution()
                    await pilot.resize_terminal(140, 40)
                    await pilot.pause()
                    self.assertEqual(provider.call_count, 1)
                    self.assertIsNone(app.execution_observation)
                    self.assertIn('Observation pending', self.card(app))
                    release.set()
                    await self.settled(app, pilot)
                    self.assertEqual(provider.call_count, 2)
                    self.assertEqual(app.execution_observation.attempt_id, app.selected()['attempt_id'])
                    self.assertEqual(app.execution_observation.job_id, app.selected()['id'])
                    if change != 'selection-back':
                        self.assertNotEqual(first_key, app.execution_selection)
                    await pilot.press('q')

    async def test_note_update_does_not_invalidate_attempt_observation(self):
        started, release = threading.Event(), threading.Event()
        original = self.provider()
        def delayed(*args, **kwargs):
            started.set()
            release.wait(5)
            return original(*args, **kwargs)
        provider = Mock(side_effect=delayed)
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.until(started.is_set)
            key = app.execution_selection
            self.store.annotate('J1.1', 'Human note while observation pending')
            app.refresh_state()
            self.assertEqual(key, app.execution_selection)
            release.set()
            await self.settled(app, pilot)
            self.assertEqual(provider.call_count, 1)
            await pilot.press('n')
            self.assertIsInstance(app.screen, NoteEditor)
            await pilot.press('escape', 'l')
            self.assertIsInstance(app.screen, Inspect)
            self.assertTrue(app.screen.logs)
            await pilot.press('escape', 'q')

    async def test_teardown_does_not_accept_pending_observation_or_spawn_replacements(self):
        started, release, returned = threading.Event(), threading.Event(), threading.Event()
        original = self.provider()
        def delayed(*args, **kwargs):
            started.set()
            release.wait(5)
            try:
                return original(*args, **kwargs)
            finally:
                returned.set()
        provider = Mock(side_effect=delayed)
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.until(started.is_set)
            await pilot.press('q')
        self.assertTrue(app.execution_closed)
        self.assertTrue(app.execution_busy)
        release.set()
        await self.until(returned.is_set)
        await self.until(lambda: not app.execution_busy)
        self.assertIsNone(app.execution_observation)
        app.collect_execution()
        self.assertEqual(provider.call_count, 1)

    async def test_old_scientific_evidence_and_parser_ok_do_not_override_worker_observation(self):
        for category in ('VERIFIED_WORKER', 'WORKER_MISSING'):
            app = self.app(self.provider(category))
            async with app.run_test(size=(80, 24)) as pilot:
                await self.settled(app, pilot)
                app.progress_snapshot = self.old_scientific_snapshot(app)
                app.render_progress()
                scientific = str(app.query_one('#progress', Static).render())
                self.assertIn('Scientific:', scientific)
                self.assertIn('Evidence 660.0m ago', scientific)
                self.assertIn('Parser: OK', scientific)
                self.assertIn('Verified worker + anchor' if category == 'VERIFIED_WORKER'
                              else 'UNVERIFIED', self.card(app))
                self.assertEqual(app.selected()['status'], 'Run')
                await pilot.press('enter')
                self.assertIn('parser success does not verify execution',
                              str(app.screen.query_one('#body', Static).render()))
                await pilot.press('escape', 'q')

    async def test_nonactive_selection_does_not_schedule_observation(self):
        provider = self.provider()
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            await pilot.press('down', 'down')
            self.assertEqual(app.selected()['status'], 'Hold')
            self.assertIsNone(app.execution_observation)
            self.assertFalse(app.query_one('#execution', Static).display)
            app.collect_execution()
            self.assertEqual(provider.call_count, 2)  # Only the two selected active jobs.
            await pilot.press('q')


    async def test_controlled_phase_and_unverified_suspension_keep_observation_and_controls_distinct(self):
        from textual.widgets import Button
        for phase, category, verdict in (('Suspended', 'CONTROLLED_PHASE', 'inhibited'),
                                         ('Unverified', 'OWNERSHIP_UNVERIFIED', 'unverified')):
            with self.subTest(phase=phase):
                with self.store.transaction() as con:
                    job = self.store.get(con, 1)
                    job['suspension'] = dict(phase=phase, revision=2, desired=phase,
                                             inhibited=True, reason='Fictional stored control phase')
                    self.store.save(con, job)
                def observe(store, job, *, state=None, now=None):
                    return FixedObservation(job['id'], job['attempt_id'], category, now,
                        phase == 'Unverified', dict(verdict=verdict, phase=phase, inhibited=True,
                                                  revision=2, scope='Stored metadata only'))
                app = self.app(Mock(side_effect=observe))
                async with app.run_test(size=(77, 24)) as pilot:
                    await self.settled(app, pilot)
                    app.progress_snapshot = self.old_scientific_snapshot(app)
                    app.render_progress()
                    for width, height in ((77, 24), (80, 24), (140, 40)):
                        await pilot.resize_terminal(width, height)
                        await pilot.pause()
                        card = self.card(app)
                        self.assertIn('Stored: Run', card)
                        self.assertIn('Stored phase: ' + phase, card)
                        self.assertIn(phase, str(app.query_one('#selected', Static).render()))
                        button = app.query_one('#suspension-action', Button)
                        self.assertEqual(button.disabled, phase == 'Unverified')
                        self.assertEqual(str(button.label), '[U] Continue job' if phase == 'Suspended'
                                         else '[U] Suspend job')
                        if phase == 'Unverified':
                            self.assertIn('UNVERIFIED', card)
                            self.assertIn('Reconciliation required', card)
                            self.assertNotIn('Verified worker', card)
                        else:
                            self.assertIn('Worker verified; control phase stored', card)
                            self.assertNotIn('Reconciliation required', card)
                        widget = app.query_one('#execution', Static)
                        self.assertLessEqual(widget.region.bottom, button.region.y)
                        self.assertGreaterEqual(app.query_one('#table', DataTable).size.height, 3)
                        self.assertLessEqual(button.region.bottom, height)
                        output = os.environ.get('CMW_LIFECYCLE_RENDER_DIR')
                        if output:
                            Path(output).mkdir(parents=True, exist_ok=True)
                            Path(output, f'combined-{phase.lower()}-{width}x{height}.svg').write_text(app.export_screenshot())
                    await pilot.press('enter')
                    details = str(app.screen.query_one('#body', Static).render())
                    self.assertIn('Original attempt retained', details)
                    self.assertIn('EXECUTION OBSERVATION', details)
                    await pilot.press('escape', 'x')
                    self.assertEqual(app.screen.__class__.__name__, 'Prompt')
                    await pilot.press('escape', 'q')
                self.assertEqual(self.store.snapshot()['jobs'][0]['status'], 'Run')

    async def test_pending_execution_result_is_invalidated_by_control_protocol_or_termination_change(self):
        for field in ('suspension', 'worker_protocol', 'termination_requested'):
            with self.subTest(field=field):
                started, release = threading.Event(), threading.Event()
                original = self.provider()
                def delayed(*args, **kwargs):
                    started.set()
                    release.wait(5)
                    return original(*args, **kwargs)
                provider = Mock(side_effect=delayed)
                app = self.app(provider)
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.until(started.is_set)
                    key = app.execution_selection
                    with self.store.transaction() as con:
                        job = self.store.get(con, 1)
                        job[field] = ({'phase': 'Unverified', 'revision': 3, 'inhibited': True}
                                      if field == 'suspension' else 99 if field == 'worker_protocol'
                                      else True)
                        self.store.save(con, job)
                    app.refresh_state()
                    self.assertNotEqual(key, app.execution_selection)
                    self.assertIsNone(app.execution_observation)
                    self.assertIn('Observation pending', self.card(app))
                    self.assertEqual(provider.call_count, 1)
                    release.set()
                    await self.settled(app, pilot)
                    self.assertEqual(provider.call_count, 2)
                    await pilot.press('q')


if __name__ == '__main__':
    unittest.main()
