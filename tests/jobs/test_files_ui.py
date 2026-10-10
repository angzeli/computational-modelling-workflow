"""Headless, synthetic Files gestures; no desktop opener or engine is launched."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

try:
    from textual.widgets import DataTable, Static, TextArea
    from cmw.jobs.tui import FilesChooser, Inspect, JobsApp, NoteEditor
except ImportError:
    JobsApp = None

from cmw.jobs.store import Store
from cmw.jobs.locations import LocationDescriptor, discover
from cmw.jobs.folder_opener import OpenResult, open_location


@unittest.skipIf(JobsApp is None, 'Console tests require the optional Jobs extra')
class FilesUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-files-ui-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root/'state')
        self.output = self.root/'output 繁體 ; $(never execute)'
        self.output.mkdir()
        self.descriptors = (LocationDescriptor('output', str(self.output)),)
        self.jobs = [self.store.add(argv=['/bin/echo', 'not launched'], cwd=self.root,
                                   name='Duplicate HOF-PDOS-TABAPy-CuII-continuation-'+str(i), hold=True)
                     for i in range(2)]
        self.opener = Mock(side_effect=lambda path: OpenResult('OPEN_REQUESTED', 'Open requested', path))
        observation = patch.object(JobsApp, 'collect_activity')
        observation.start()
        self.addCleanup(observation.stop)

    def app(self, **kwargs):
        return JobsApp(self.store, file_opener=self.opener,
                       location_descriptors=kwargs.pop('location_descriptors', self.descriptors), **kwargs)

    async def finished(self, app, pilot):
        for _ in range(150):
            if not app.files_busy:
                await pilot.pause()
                return
            await asyncio.sleep(.01)
        self.fail('Files worker did not complete')

    async def started(self, event):
        for _ in range(150):
            if event.is_set():
                return
            await asyncio.sleep(.01)
        self.fail('Files worker did not start')

    def file_offset(self, table, row=0):
        columns = table.ordered_columns
        index = next(i for i, column in enumerate(columns) if column.key.value == 'files')
        return (sum(c.get_render_width(table) for c in columns[:index])+1,
                table.header_height+row)

    async def test_actual_files_click_uses_unselected_row_without_details_or_hover_open(self):
        # Distinct declarative VASP outputs prove the clicked row owns the target.
        self.store = Store(self.root/'click-state')
        outputs = [self.output, self.root/'other output']
        outputs[1].mkdir()
        self.jobs = [self.store.add(argv=['/synthetic/run-vasp.sh', '--input', str(self.root),
                                         '--output', str(output), '--managed-foreground'],
                                   cwd=self.root, name='Synthetic VASP', hold=True)
                     for output in outputs]
        app = self.app(location_descriptors=())
        with patch('cmw.jobs.tui.discover', wraps=discover) as discovery:
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.pause()
                table = app.query_one('#table', DataTable)
                self.assertEqual(app.selected_id, self.jobs[0]['display_id'])
                await pilot.hover('#table', offset=self.file_offset(table, 1))
                self.opener.assert_not_called()
                await pilot.click('#table', offset=self.file_offset(table, 1))
                await self.finished(app, pilot)
                self.assertEqual(discovery.call_args.args[1], self.jobs[1]['id'])
                self.assertEqual(discovery.call_args.kwargs['expected_attempt_id'], self.jobs[1]['attempt_id'])
                self.assertEqual(app.selected_id, self.jobs[1]['display_id'])
                self.assertNotIsInstance(app.screen, (Inspect, FilesChooser))
                self.opener.assert_called_once_with(str(outputs[1]))
                await pilot.press('enter')
                self.assertIsInstance(app.screen, Inspect)
                self.opener.assert_called_once()
                await pilot.press('escape')
                await pilot.click('#table', offset=(1, table.header_height))
                self.assertIsInstance(app.screen, Inspect)
                self.opener.assert_called_once()
                await pilot.press('escape', 'q')
        self.assertFalse(self.store.snapshot()['controller']['online'])
        self.assertEqual([j['status'] for j in self.store.snapshot()['jobs']], ['Hold','Hold'])

    async def test_f_types_in_note_and_opens_only_after_explicit_main_action(self):
        app = self.app()
        async with app.run_test(size=(77,24)) as pilot:
            await pilot.pause()
            await pilot.press('n', 'f')
            self.assertIsInstance(app.screen, NoteEditor)
            self.assertEqual(app.screen.query_one(TextArea).text, 'f')
            self.opener.assert_not_called()
            await pilot.press('ctrl+s', 'f')
            await self.finished(app, pilot)
            self.opener.assert_called_once_with(str(self.output))
            self.assertEqual(self.store.snapshot()['jobs'][0]['note'], 'f')
            self.assertIn('OPEN_REQUESTED', str(app.query_one('#message', Static).render()))
            await pilot.press('q')

    async def test_worker_checks_are_off_ui_thread_and_unchanged_refresh_is_safe(self):
        started, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        def delayed(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            started.set()
            release.wait(5)
            return discover(*args, **kwargs)
        app = self.app()
        with patch('cmw.jobs.tui.discover', side_effect=delayed) as discovery:
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.press('f')
                await self.started(started)
                app.refresh_state()
                await pilot.press('f')
                self.assertEqual(discovery.call_count, 1)
                release.set()
                await self.finished(app, pilot)
                self.opener.assert_called_once_with(str(self.output))
                await pilot.press('q')

    async def test_stale_discovery_selection_resize_reorder_and_disappearance_are_discarded(self):
        for change in ('selection', 'selection-back', 'resize', 'reorder', 'note-editor', 'details', 'disappear'):
            with self.subTest(change=change):
                started, release = threading.Event(), threading.Event()
                def delayed(*args, **kwargs):
                    started.set()
                    release.wait(5)
                    return discover(*args, **kwargs)
                app = self.app()
                with patch('cmw.jobs.tui.discover', side_effect=delayed):
                    async with app.run_test(size=(80,24)) as pilot:
                        await pilot.press('f')
                        await self.started(started)
                        if change in {'selection','selection-back'}:
                            await pilot.press('down')
                            if change == 'selection-back':
                                await pilot.press('up')
                        elif change == 'resize':
                            await pilot.resize_terminal(77,24)
                        elif change in {'note-editor', 'details'}:
                            await pilot.press('n' if change == 'note-editor' else 'enter')
                        elif change == 'reorder':
                            self.store.change(self.jobs[1]['display_id'], 'move', position=1, expected='Hold')
                            app.refresh_state()
                        else:
                            with self.store.transaction() as con:
                                con.execute('DELETE FROM jobs WHERE id=?', (self.jobs[0]['id'],))
                            app.refresh_state()
                        release.set()
                        await self.finished(app, pilot)
                        self.opener.assert_not_called()
                        self.assertNotIsInstance(app.screen, FilesChooser)
                        self.assertIn('expired', str(app.query_one('#message', Static).render()))
                        if change in {'note-editor','details'}:
                            self.assertIsInstance(app.screen, NoteEditor if change == 'note-editor' else Inspect)
                            await pilot.press('escape')
                        await pilot.press('q')

    async def test_change_during_open_recheck_does_not_reach_platform_opener(self):
        started, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            started.set()
            release.wait(5)
            return open_location(*args, **kwargs)
        app = self.app()
        with patch('cmw.jobs.tui.open_location', side_effect=delayed):
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.press('f')
                await self.started(started)
                await pilot.resize_terminal(77,24)
                release.set()
                await self.finished(app, pilot)
                self.opener.assert_not_called()
                await pilot.press('q')

    async def test_multistage_chooser_keeps_full_paths_and_binds_selection(self):
        second = self.root/'second stage'
        second.mkdir()
        descriptors = (LocationDescriptor('output', str(self.output), stage='geometry'),
                       LocationDescriptor('output', str(second), stage='single point'))
        app = self.app(location_descriptors=descriptors)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.press('f')
            await self.finished(app, pilot)
            self.assertIsInstance(app.screen, FilesChooser)
            chooser = app.screen
            text = '\n'.join(str(widget.render()) for widget in chooser.query(Static))
            for value in ('geometry', 'single point', str(self.output), str(second), 'output'):
                self.assertIn(value, text)
            self.opener.assert_not_called()
            index = next(i for i, loc in enumerate(chooser.result.locations) if loc.path == str(self.output))
            button = chooser.query_one(f'#files-location-{index}')
            button.scroll_visible(immediate=True)
            await pilot.pause()
            await pilot.click(f'#files-location-{index}')
            await self.finished(app, pilot)
            self.opener.assert_called_once_with(str(self.output))
            self.opener.reset_mock()
            await pilot.resize_terminal(77,24)
            app.start_file_open(chooser.request, chooser.result, chooser.result.locations[index], chooser)
            self.assertIn('expired', str(chooser.query_one('#files-message', Static).render()))
            self.opener.assert_not_called()
            await pilot.press('escape', 'q')

    async def test_dismissed_chooser_does_not_open_after_worker_recheck(self):
        app = self.app(location_descriptors=())
        started, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            started.set()
            release.wait(5)
            return open_location(*args, **kwargs)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.press('f')
            await self.finished(app, pilot)
            self.assertIsInstance(app.screen, FilesChooser)
            chooser = app.screen
            location = next(loc for loc in chooser.result.locations if loc.accessibility == 'AVAILABLE')
            with patch('cmw.jobs.tui.open_location', side_effect=delayed):
                app.start_file_open(chooser.request, chooser.result, location, chooser)
                await self.started(started)
                await pilot.press('escape')
                release.set()
                await self.finished(app, pilot)
            self.opener.assert_not_called()
            self.assertNotIsInstance(app.screen, FilesChooser)
            await pilot.press('q')

    async def test_missing_preferred_shows_error_and_alternatives_without_fallback(self):
        missing = self.root/'missing output'
        app = self.app(location_descriptors=(LocationDescriptor('output', str(missing)),))
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.press('f')
            await self.finished(app, pilot)
            self.assertIsInstance(app.screen, FilesChooser)
            message = str(app.screen.query_one('#files-message', Static).render())
            self.assertIn('NOT_FOUND', message)
            self.assertIn(str(missing), message)
            self.assertTrue(any(loc.role != 'output' and loc.path for loc in app.screen.result.locations))
            self.opener.assert_not_called()
            self.assertFalse(missing.exists())
            await pilot.press('escape', 'q')

    async def test_required_layouts_preserve_name_usage_order_and_render_chooser(self):
        from tests.jobs.test_usage_ui import usage
        with self.store.transaction() as con:
            job = self.store.get(con, self.jobs[0]['id'])
            job.update(status='Run', order=None, started_at=1700000000)
            self.store.save(con, job)
        app = self.app(clock=lambda: 1700000600)
        async with app.run_test(size=(77,24)) as pilot:
            app.accept_activity({'job_usage': {self.jobs[0]['attempt_id']: usage(cpu_cores=7.13)}})
            for width, height in ((77,24),(80,24),(140,40),(42,24)):
                await pilot.resize_terminal(width,height)
                await pilot.pause()
                table = app.query_one('#table', DataTable)
                columns = list(table.columns)
                self.assertEqual(columns[columns.index('id')+1:columns.index('id')+3], ['files','name'])
                self.assertEqual(table.row_count, 2)
                self.assertEqual(app.row_ids, [job['display_id'] for job in self.jobs])
                if width >= 77:
                    self.assertGreaterEqual(table.columns['name'].width, 30)
                    self.assertLessEqual(sum(c.width+2 for c in table.columns.values()), table.size.width)
                    self.assertLessEqual(app.query_one('#shortcuts').region.bottom, height)
                else:
                    self.assertEqual(table.columns['name'].width, 12)
                    self.assertGreater(table.virtual_size.width, table.size.width)
                if width == 140:
                    self.assertEqual(str(table.get_cell(self.jobs[0]['display_id'], 'cpu_now')), '713%')
                rendered = os.environ.get('CMW_JOBS_FILES_RENDER')
                if rendered:
                    Path(rendered, f'files-{width}x{height}.svg').write_text(app.export_screenshot())
            await pilot.resize_terminal(80,24)
            app.location_descriptors = (LocationDescriptor('output', str(self.output), stage='geometry'),
                                        LocationDescriptor('inputs', str(self.root), stage='single point'))
            await pilot.pause()
            await pilot.press('f')
            await self.finished(app, pilot)
            self.assertIsInstance(app.screen, FilesChooser)
            if os.environ.get('CMW_JOBS_FILES_RENDER'):
                Path(os.environ['CMW_JOBS_FILES_RENDER'], 'files-chooser-80x24.svg').write_text(app.export_screenshot())
            self.opener.assert_not_called()
            await pilot.press('escape','q')
