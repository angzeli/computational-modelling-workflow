"""Real Textual key and mouse paths; all clipboard and opener calls are stubs."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import DEFAULT, Mock, patch

from cmw.jobs.clipboard import CopyResult
from cmw.jobs.store import Store
from tests.jobs.test_snapshot import fixture

try:
    from textual.document._document import Selection as TextSelection
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.widgets import DataTable, Select, Static, TextArea
    from cmw.jobs.copy_ui import CopyPreview, FrozenText, SelectableStatic
    from cmw.jobs.tui import JobsApp, NoteEditor
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console selection tests require optional Jobs extra')
class CopyUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-copy-ui-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state')
        self.jobs = [self.store.add(argv=['/unexecuted/wrapper'], cwd=self.root,
                                   name='Synthetic untruncated name ' * 12 + str(index), hold=True)
                     for index in range(2)]
        for method in ('collect_activity', 'collect_progress', 'collect_execution'):
            patched = patch.object(JobsApp, method)
            patched.start()
            self.addCleanup(patched.stop)
        self.backend = Mock(return_value=CopyResult('LOCAL_ACCEPTED', 'Stub accepted synthetic text'))

    def app(self):
        return JobsApp(self.store, clock=lambda: 112, clipboard_backend=self.backend)

    async def copied(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        self.assertFalse(app.copy_busy)

    async def test_preview_freezes_attempt_refresh_is_explicit_and_copy_is_complete(self):
        app = self.app()
        before = self.store.snapshot()['jobs']
        async with app.run_test(size=(77, 24)) as pilot:
            app.progress_snapshot = fixture(self.jobs[0])
            await pilot.press('y')
            self.assertIsInstance(app.screen, CopyPreview)
            view = app.screen
            area = view.query_one(TextArea)
            initial = area.text
            self.assertIn(self.jobs[0]['name'], initial)
            self.assertIn('3.10870000', initial)
            self.assertTrue(area.read_only)
            await pilot.press('q', 'u', 'x', 'p', 'y')
            self.assertEqual(area.text, initial)
            app.selected_id = self.jobs[1]['display_id']
            app.progress_snapshot = fixture(self.jobs[1])
            await pilot.pause()
            self.assertEqual(area.text, initial)
            await pilot.click('#copy-refresh')
            self.assertEqual(area.text, initial)
            self.assertIn('retained', str(view.query_one('#copy-status', Static).render()))
            await pilot.click('#copy-all')
            await self.copied(app, pilot)
            self.backend.assert_called_with(initial)
            app.selected_id = self.jobs[0]['display_id']
            app.progress_snapshot = replace(fixture(self.jobs[0]), messages=('Explicit refresh evidence',))
            await pilot.click('#copy-refresh')
            self.assertIn('Explicit refresh evidence', area.text)
            view.query_one(Select).value = 'json'
            await pilot.pause()
            self.assertIn('"attempt_id"', area.text)
            await pilot.click('#copy-redact')
            self.assertNotIn('/synthetic private/output.log', area.text)
            await pilot.press('escape', 'q')
        self.assertEqual(self.store.snapshot()['jobs'], before)

    async def test_ctrl_c_selected_text_failure_consumes_event_and_editor_literals_survive(self):
        self.backend.side_effect = OSError('synthetic backend failure')
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press('n', 'q', 'u', 'x', 'p')
            editor = app.screen
            self.assertIsInstance(editor, NoteEditor)
            area = editor.query_one(TextArea)
            self.assertEqual(area.text, 'quxp')
            area.selection = TextSelection((0, 0), (0, 4))
            await pilot.press('ctrl+c')
            await self.copied(app, pilot)
            self.assertIs(app.screen, editor)
            self.backend.assert_called_once_with('quxp')
            self.assertEqual(app.copy_result.status, 'FAILED')
            self.assertEqual(area.text, 'quxp')
            area.selection = TextSelection((0, 4), (0, 4))
            await pilot.press('ctrl+c')
        self.assertEqual(self.store.snapshot()['jobs'][0]['note'], '')
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    async def test_static_selection_freezes_revision_and_no_selection_still_detaches(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            widget = app.query_one('#progress', SelectableStatic)
            widget.update('Synthetic summary revision one')
            app.screen.selections = {widget: Selection(Offset(0, 0), Offset(9, 0))}
            await pilot.pause()
            widget.update('Synthetic summary revision two')
            self.assertIn('revision one', str(widget.render()))
            await pilot.press('ctrl+c')
            await self.copied(app, pilot)
            self.backend.assert_called_once_with('Synthetic')
            self.assertIs(app.screen, app.screen_stack[0])
            app.screen.clear_selection()
            await pilot.pause()
            self.assertIn('revision two', str(widget.render()))
            await pilot.press('ctrl+c')

    async def test_actual_mouse_selection_copies_and_empty_click_does_not_freeze(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            widget = app.query_one('#selected', SelectableStatic)
            await pilot.mouse_down('#selected', offset=(3, 0))
            await pilot.hover('#selected', offset=(12, 0))
            await pilot.mouse_up('#selected', offset=(12, 0))
            selected = app.screen.get_selected_text()
            self.assertTrue(selected)
            frozen = str(widget.render())
            widget.update('Replacement selected summary')
            self.assertEqual(str(widget.render()), frozen)
            await pilot.press('ctrl+c')
            await self.copied(app, pilot)
            self.backend.assert_called_once_with(selected)
            app.screen.clear_selection()
            await pilot.pause()
            widget.selection_updated(Selection(Offset(0, 0), Offset(0, 0)))
            widget.update('Empty selection does not freeze')
            self.assertEqual(str(widget.render()), 'Empty selection does not freeze')
            await pilot.press('q')

    async def test_process_shortcuts_ignore_native_selection_copy_and_detach_still_work(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            widget = app.query_one('#selected', SelectableStatic)
            app.screen.selections = {widget: Selection(Offset(0, 0), Offset(8, 0))}
            await pilot.pause()
            with patch.multiple(app, action_dispatch=DEFAULT, action_hold=DEFAULT,
                                action_order=DEFAULT, action_cancel=DEFAULT, action_suspension=DEFAULT) as controls:
                await pilot.press('p', 'h', 'o', 'x', 'u')
                for control in controls.values():
                    control.assert_not_called()
            await pilot.press('y')
            self.assertIsInstance(app.screen, CopyPreview)
            await pilot.press('escape')
            app.screen.selections = {widget: Selection(Offset(0, 0), Offset(8, 0))}
            await pilot.press('q')
        self.assertTrue(all(job['status'] == 'Hold' for job in self.store.snapshot()['jobs']))
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    async def test_details_frozen_fallback_and_files_drag_never_opens(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press('enter', 'y')
            self.assertIsInstance(app.screen, FrozenText)
            area = app.screen.query_one(TextArea)
            self.assertTrue(area.read_only)
            area.selection = TextSelection((0, 0), (0, 8))
            await pilot.press('ctrl+c')
            await self.copied(app, pilot)
            self.assertIsInstance(app.screen, FrozenText)
            await pilot.press('escape', 'escape')
            table = app.query_one('#table', DataTable)
            columns = table.ordered_columns
            index = next(i for i, column in enumerate(columns) if column.key.value == 'files')
            x = sum(column.get_render_width(table) for column in columns[:index]) + 1
            with patch.object(app, 'action_files') as opening, patch.object(app, 'action_details') as details:
                await pilot.mouse_down('#table', offset=(x, table.header_height))
                await pilot.hover('#table', offset=(x + 3, table.header_height))
                await pilot.mouse_up('#table', offset=(x + 3, table.header_height))
                await pilot.pause()
                opening.assert_not_called()
                details.assert_not_called()
                await pilot.click('#table', offset=(x, table.header_height))
                opening.assert_called_once()
            await pilot.press('q')
        self.assertTrue(all(job['status'] == 'Hold' for job in self.store.snapshot()['jobs']))
