"""Bounded selected-context workers, generic cards and existing controls."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.folder_opener import OpenResult
from cmw.jobs.locations import LocationDescriptor
from cmw.jobs.progress import resolve_context
from cmw.jobs.store import Store
from cmw.progress.models import SourceDescriptor
from cmw.progress.registry import register_adapter
from cmw.progress.service import ObservationService
from tests.jobs.test_progress_cli import FictionalAdapter

try:
    from cmw.jobs.tui import JobsApp, Inspect, NoteEditor
    from textual.widgets import DataTable, Static, TextArea
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require the optional Jobs extra')
class ProgressUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-progress-ui-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root/'state')
        self.jobs = [self.store.add(argv=['/unexecuted/opaque-wrapper'], cwd=self.root,
                      name='HOF-PDOS-TABAPy-CuII-continuation-'+str(i), engine='FICTIONAL', hold=True)
                     for i in range(2)]
        self.source = self.root/'flux.txt'
        self.source.write_text('3.75\n3.25\n')
        self.sources = (SourceDescriptor('flux', str(self.source)),)
        self.opener = Mock(return_value=OpenResult('OPEN_REQUESTED', 'Open requested', str(self.root)))
        activity = patch.object(JobsApp, 'collect_activity')
        activity.start()
        self.addCleanup(activity.stop)
        from cmw.progress.registry import _REGISTRY
        registration = patch.dict(_REGISTRY)
        registration.start()
        self.addCleanup(registration.stop)
        register_adapter('FICTIONAL', FictionalAdapter)

    def app(self, **kwargs):
        return JobsApp(self.store, progress_sources=self.sources, file_opener=self.opener,
                       location_descriptors=(LocationDescriptor('output', str(self.root)),), **kwargs)

    async def finish(self, app, pilot):
        for _ in range(200):
            if not app.progress_busy:
                await pilot.pause()
                return
            await asyncio.sleep(.01)
        self.fail('Progress worker did not complete')

    async def wait_started(self, event):
        for _ in range(200):
            if event.is_set():
                return
            await asyncio.sleep(.01)
        self.fail('Progress worker did not start')

    async def test_fictional_card_details_and_files_note_controls(self):
        app = self.app()
        async with app.run_test(size=(80,24)) as pilot:
            await self.finish(app, pilot)
            card = str(app.query_one('#progress', Static).render())
            for value in ('Charge flux [C/s]', '3.25', '3.75', 'geometry evaluation 4', 'outer iteration 2'):
                self.assertIn(value, card)
            table = app.query_one('#table', DataTable)
            self.assertEqual(list(table.columns)[:3], ['id','files','name'])
            await pilot.press('enter')
            self.assertIsInstance(app.screen, Inspect)
            detail = str(app.screen.query_one('#body', Static).render())
            self.assertIn('inner iteration 2', detail)
            self.assertIn(str(self.source), detail)
            await pilot.press('escape')
            await pilot.resize_terminal(42,24)
            await pilot.pause()
            self.assertLessEqual(app.query_one('#shortcuts').region.bottom, 24)
            self.assertGreaterEqual(table.size.height, 3)
            await pilot.resize_terminal(80,24)
            await pilot.press('n', 'f')
            self.assertIsInstance(app.screen, NoteEditor)
            self.assertEqual(app.screen.query_one(TextArea).text, 'f')
            self.opener.assert_not_called()
            await pilot.press('ctrl+s', 'f')
            for _ in range(100):
                if not app.files_busy:
                    break
                await asyncio.sleep(.01)
            self.opener.assert_called_once()
            self.assertEqual(app.selected_id, self.jobs[0]['display_id'])
            await pilot.press('q')
        self.assertFalse(self.store.snapshot()['controller']['online'])
        self.assertTrue(all(job['status']=='Hold' for job in self.store.snapshot()['jobs']))

    async def test_cached_read_error_keeps_freshness_and_full_details(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.finish(app, pilot)
            snapshot = app.progress_snapshot
            series = snapshot.metrics[0]
            app.progress_snapshot = replace(snapshot, metrics=tuple(
                replace(series, latest=replace(series.latest, semantic_id=f'fixture.{i}'), previous=None)
                for i in range(8)))
            message = 'Synthetic disconnected source ' + 'detail ' * 30
            app.progress_error = message
            app.render_progress()
            await pilot.pause()
            widget = app.query_one('#progress', Static)
            card = str(widget.render())
            self.assertIn('Read unavailable; cached evidence:', card)
            self.assertIn('Evidence', card.splitlines()[-1])
            self.assertLessEqual(len(card.splitlines()), widget.size.height)
            self.assertLessEqual(widget.region.bottom, app.query_one('#shortcuts').region.y)
            await pilot.press('enter')
            self.assertIn(message, str(app.screen.query_one('#body', Static).render()))
            await pilot.press('escape', 'q')

    async def test_delayed_selection_and_binding_results_are_discarded_without_replacement_threads(self):
        for change in ('selection', 'selection-back', 'binding', 'attempt'):
            with self.subTest(change=change):
                started, release = threading.Event(), threading.Event()
                service = ObservationService()
                original = service.poll
                main_thread = threading.get_ident()
                def delayed(context):
                    self.assertNotEqual(threading.get_ident(), main_thread)
                    started.set()
                    release.wait(5)
                    return original(context)
                service.poll = Mock(side_effect=delayed)
                app = self.app(progress_service=service)
                async with app.run_test(size=(80,24)) as pilot:
                    await self.wait_started(started)
                    if change == 'attempt':
                        with self.store.transaction() as con:
                            job = self.store.get(con, self.jobs[0]['id'])
                            job['attempt_id'] = 'a' * 32
                            self.store.save(con, job)
                        app.refresh_state()
                    elif change == 'binding':
                        app.progress_sources = (SourceDescriptor('flux', str(self.root/'other.txt')),)
                        app.sync_progress_selection()
                    else:
                        await pilot.press('down')
                        if change == 'selection-back':
                            await pilot.press('up')
                    for _ in range(8):
                        app.collect_progress()
                    self.assertEqual(service.poll.call_count, 1)
                    self.assertTrue(app.progress_busy)
                    release.set()
                    await self.finish(app, pilot)
                    self.assertIsNone(app.progress_snapshot)
                    await pilot.press('q')

    async def test_detach_releases_a_still_running_reader_only_after_return(self):
        started, release, closed = threading.Event(), threading.Event(), threading.Event()
        service = ObservationService()
        original_poll, original_close = service.poll, service.close
        def delayed(context):
            started.set()
            release.wait(5)
            return original_poll(context)
        def close():
            original_close()
            closed.set()
        service.poll, service.close = Mock(side_effect=delayed), close
        app = self.app(progress_service=service)
        async with app.run_test(size=(80,24)) as pilot:
            await self.wait_started(started)
            await pilot.press('q')
            self.assertEqual(service.poll.call_count, 1)
        self.assertFalse(closed.is_set())
        release.set()
        await self.wait_started(closed)
        self.assertIsNone(app.progress_snapshot)

    async def test_initial_catchup_uses_bounded_slices_without_waiting_a_full_poll_interval(self):
        from cmw.progress.reader import RegularFileReader
        service = ObservationService(reader_factory=lambda source: RegularFileReader(source, byte_budget=4))
        service.poll = Mock(wraps=service.poll)
        app = self.app(progress_service=service)
        async with app.run_test(size=(80,24)) as pilot:
            for _ in range(100):
                if (app.progress_snapshot is not None and not app.progress_busy
                        and not any(source.has_more for source in app.progress_snapshot.sources)):
                    break
                await asyncio.sleep(.01)
            else:
                self.fail('Bounded initial catch-up did not finish within one second')
            self.assertEqual(service.poll.call_count, 3)
            self.assertEqual(app.progress_snapshot.metrics[0].latest.value, 3.25)
            self.assertLessEqual(app.progress_snapshot.sources[0].bytes_read, 4 + 2 * 128)
            await pilot.press('enter')
            self.assertIn('Charge flux', str(app.screen.query_one('#body', Static).render()))
            self.assertEqual(service.poll.call_count, 3)
            await pilot.press('escape', 'q')

    async def test_unknown_engine_retains_files_and_read_failure_preserves_previous_metrics(self):
        service = ObservationService()
        app = self.app(progress_service=service)
        async with app.run_test(size=(77,24)) as pilot:
            await self.finish(app, pilot)
            previous = app.progress_snapshot
            with patch.object(service, 'poll', side_effect=OSError('synthetic disconnected source')):
                app.collect_progress()
                await self.finish(app, pilot)
            self.assertIs(app.progress_snapshot, previous)
            self.assertIn('synthetic disconnected source', app.progress_error)
            app.progress_engine = 'NO_SUCH_ADAPTER'
            app.sync_progress_selection()
            await self.finish(app, pilot)
            self.assertIn('Progress adapter unavailable', str(app.query_one('#progress', Static).render()))
            await pilot.press('f')
            for _ in range(100):
                if not app.files_busy:
                    break
                await asyncio.sleep(.01)
            self.opener.assert_called_once()
            await pilot.press('q')


if __name__ == '__main__':
    unittest.main()
