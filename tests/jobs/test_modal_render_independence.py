"""Partial widget removal must not discard live modal results or bookkeeping."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.source_selection import SourceInspection, SourceValidation
from cmw.progress.models import HistoryEntry, HistoryPage, RunContext
from cmw.progress.registry import SourcePlan

try:
    from textual.widgets import Button, Static, TextArea
    from cmw.doctor.presentation import DoctorReport
    from cmw.jobs.history_ui import ProgressHistory
    from cmw.jobs.navigation import NavigationApp, NavigationTable
    from cmw.jobs.sources_ui import Sources
except ImportError:
    NavigationApp = None


@unittest.skipIf(NavigationApp is None, 'Console tests require the optional Jobs extra')
class ModalRenderIndependenceTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        app = NavigationApp()
        app.sources_view = app.sources_request = app.history_view = None
        app.request_sources = Mock()
        app.collect_progress = Mock()
        app.session_source_for = Mock(return_value=None)
        app.progress_service = SimpleNamespace(release_history=Mock())
        return app

    def inspection(self):
        context = RunContext('synthetic-binding', 'VASP', attempt_id='synthetic-attempt')
        return SourceInspection(('synthetic-attempt',), context,
                                (SourcePlan('outcar', None),), ())

    async def test_doctor_runs_once_without_textarea_and_defers_display_receipt(self):
        app = self.app()
        view = DoctorReport(None, 'Synthetic doctor')
        report = {'schema': 1, 'checks': ()}
        loader, displayed = Mock(return_value=report), Mock()
        async with app.run_test() as pilot:
            await app.push_screen(view)
            textarea = view.query_one(TextArea)
            container = textarea.parent
            await textarea.remove()
            with patch.object(view, 'run_worker') as run, \
                    patch.object(app, 'call_from_thread', side_effect=lambda callback, *args: callback(*args)), \
                    patch('cmw.doctor.service.format_report', return_value='Synthetic retained diagnostic'):
                view.load(loader, on_displayed=displayed)
                run.call_args.args[0]()
                view.load(loader, on_displayed=displayed)
            run.assert_called_once()
            loader.assert_called_once_with()
            self.assertFalse(view.pending)
            self.assertIs(view.report, report)
            self.assertIn('Synthetic retained diagnostic', view.text)
            displayed.assert_not_called()
            replacement = TextArea('Old visible diagnostic', read_only=True, id='copy-text')
            await container.mount(replacement)
            view.render_report()
            view.render_report()
            self.assertIn('Synthetic retained diagnostic', replacement.text)
            displayed.assert_called_once_with()
            await pilot.pause()

    async def test_sources_accept_inspection_without_render_surface_and_restore_fields(self):
        app, inspection = self.app(), self.inspection()
        view = Sources(None, 'J1.1')
        async with app.run_test() as pilot:
            await app.push_screen(view)
            display = view.query_one('#sources-inspection')
            container = display.parent
            await display.remove()
            generation = view.request_generation
            await view.accept_result('inspect', inspection, None, generation)
            self.assertIs(view.inspection, inspection)
            self.assertEqual(view.plans, inspection.plans)
            self.assertFalse(view.busy)
            self.assertFalse(view.query('#source-path-0'))
            replacement = Static('', id='sources-inspection')
            await container.mount(replacement)
            view.render_inspection()
            await view.build_fields()
            view.update_controls()
            self.assertIn('synthetic-attempt', str(replacement.render()))
            self.assertTrue(view.query('#source-path-0'))
            self.assertEqual(view.request_generation, generation)
            await pilot.pause()

    async def test_sources_accept_validation_but_missing_fields_disable_commands(self):
        app, inspection = self.app(), self.inspection()
        view = Sources(None, 'J1.1')
        validation = SourceValidation(inspection.identity, inspection.context, (), ())
        async with app.run_test() as pilot:
            await app.push_screen(view)
            await view.accept_result('inspect', inspection, None, view.request_generation)
            await pilot.pause()
            view.configuring = True
            await view.query_one('#source-path-0').remove()
            status = view.query_one('#sources-validation')
            container = status.parent
            await status.remove()
            view.busy = True
            await view.accept_result('validate', validation, None, view.request_generation)
            self.assertIs(view.validation, validation)
            self.assertIsNone(view.validated_fields)
            self.assertFalse(view.busy)
            for name in ('clear', 'validate', 'use'):
                self.assertTrue(view.query_one('#sources-' + name, Button).disabled)
            before = app.request_sources.call_count
            view.request('activate', (validation, True))
            self.assertEqual(app.request_sources.call_count, before)
            replacement = Static('', id='sources-validation')
            await container.mount(replacement)
            view.update_controls()
            self.assertIn('Validated; not yet active', str(replacement.render()))
            await replacement.remove()
            generation = view.request_generation
            view.invalidate()
            self.assertTrue(view.expired)
            self.assertEqual(view.request_generation, generation + 1)
            self.assertIsNone(view.validation)
            self.assertIn('Selected attempt or declarations changed', view.status_text)

    async def test_history_retains_page_and_releases_binding_without_widgets(self):
        app = self.app()
        context = self.inspection().context
        entry = HistoryEntry('synthetic-entry', 1, native_label='Synthetic observation')
        page = HistoryPage(context, (entry,), status='COMPLETE', source_revision='revision-1',
                           cursor='page-1', boundaries=(('stage-1', 1),))
        view = ProgressHistory(None, 'J1.1')
        async with app.run_test() as pilot:
            await app.push_screen(view)
            table = view.query_one('#history-table')
            container = table.parent
            await table.remove()
            state = view.query_one('#history-state')
            await state.remove()
            view.accept_page(context, page, view.request_generation)
            self.assertIs(view.page, page)
            self.assertIs(view.context, context)
            self.assertEqual(view.entries, {entry.entry_id: entry})
            self.assertEqual(view.boundaries, page.boundaries)
            self.assertEqual(view.cursor, 'page-1')
            replacement = NavigationTable(id='history-table', cursor_type='row')
            await container.mount(replacement, Static('', id='history-state'))
            with app.navigation_update():
                view.render_page()
            self.assertEqual(replacement.row_count, 1)
            self.assertEqual(replacement.ordered_rows[0].key.value, entry.entry_id)
            await view.query_one('#history-state').remove()
            generation = view.request_generation
            view.invalidate()
            self.assertTrue(view.expired)
            self.assertEqual(view.request_generation, generation + 1)
            app.progress_service.release_history.assert_called_once_with(context)
            await pilot.pause()

    async def test_history_source_revision_change_invalidates_navigation_without_widgets(self):
        app = self.app()
        context = self.inspection().context
        first = HistoryPage(context, (HistoryEntry('old-entry', 1),), source_revision='revision-1')
        second = replace(first, entries=(HistoryEntry('new-entry', 2),), source_revision='revision-2')
        view = ProgressHistory(None, 'J1.1')
        async with app.run_test():
            await app.push_screen(view)
            view.accept_page(context, first, view.request_generation)
            view.parent_id, view.cursor = 'old-parent', 'old-cursor'
            view.parents = [('old-parent', None, 'old scope', 'old-entry')]
            await view.query_one('#history-state').remove()
            generation = view.request_generation
            view.accept_page(context, second, generation)
            self.assertEqual(view.request_generation, generation + 1)
            self.assertIsNone(view.parent_id)
            self.assertIsNone(view.cursor)
            self.assertEqual(view.parents, [])
            self.assertEqual(view.entries, {'new-entry': second.entries[0]})
            self.assertEqual(view.page.status, 'LOADING')
            self.assertIn('Source generation changed', view.page.messages[0])
            # An obsolete response must not overwrite the newly accepted state.
            view.accept_page(context, first, generation)
            self.assertEqual(view.page.source_revision, 'revision-2')


if __name__ == '__main__':
    unittest.main()
