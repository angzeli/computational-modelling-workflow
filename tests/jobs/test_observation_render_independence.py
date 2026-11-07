"""Live observation work is independent of the base screen's render surface."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.store import Store

try:
    from cmw.jobs.history_ui import ProgressHistory
    from cmw.jobs.sources_ui import Sources
    from cmw.jobs.tui import JobsApp
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require the optional Jobs extra')
class ObservationRenderIndependenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-observation-render-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'queue')
        self.job = self.store.add(argv=['/never/executed'], cwd=self.root,
                                  name='Synthetic observation', hold=True)
        self.collect_progress = JobsApp.collect_progress
        for method in ('collect_activity', 'collect_progress', 'collect_execution'):
            self.enterContext(patch.object(JobsApp, method))

    def source_selection(self, generation=1):
        return SimpleNamespace(identity=(str(self.store.root), 'database', 'fixture',
                                         self.job['id'], self.job['attempt_id']),
                               generation=generation)

    async def test_current_execution_and_progress_evidence_survive_missing_base_widgets(self):
        service = Mock()
        app = JobsApp(self.store, progress_service=service)
        with patch.object(app, 'set_interval'):
            async with app.run_test():
                # No process is launched: this is the displayed synthetic row.
                app.selected()['status'] = 'Run'
                execution_selection = app.execution_identity()
                self.assertIsNotNone(execution_selection)
                app.execution_selection = execution_selection
                selection = app.progress_identity()
                app.progress_selection = selection
                await app.screen.query_one('#machine').remove()
                await app.screen.query_one('#execution').remove()
                await app.screen.query_one('#progress').remove()
                observation = object()
                snapshot = SimpleNamespace(completeness='COMPLETE', sources=())
                app.accept_execution(execution_selection, app.execution_generation, observation, 'qualified execution')
                app.accept_progress(selection, app.progress_generation, snapshot, 'qualified progress')
                self.assertIs(app.execution_observation, observation)
                self.assertEqual(app.execution_error, 'qualified execution')
                self.assertIs(app.progress_snapshot, snapshot)
                self.assertEqual(app.progress_error, 'qualified progress')
                service.retain.assert_not_called()

    async def test_source_activation_completes_while_the_base_machine_widget_is_absent(self):
        app = JobsApp(self.store)
        with patch.object(app, 'set_interval'), patch.object(app, 'request_sources'):
            async with app.run_test():
                origin = app.screen
                selection = app.progress_identity()
                app.progress_selection = selection
                view = Sources(selection, self.job['display_id'])
                await app.push_screen(view)
                await origin.query_one('#machine').remove()
                result = self.source_selection(generation=7)
                request = (view, 'activate', None, view.request_generation)
                await app.accept_sources(request, selection, app.progress_generation, result, None)
                self.assertIs(app.session_sources[result.identity], result)
                self.assertEqual(app.source_binding_generation, 7)
                self.assertTrue(view.closed)
                self.assertNotIn(view, app.screen_stack)
                self.assertEqual(app.progress_selection, app.progress_identity())
                self.assertEqual(app.progress_selection[-1], 7)

    async def test_database_and_source_invalidation_survive_missing_message_widgets(self):
        for changed_database in (True, False):
            with self.subTest(database_changed=changed_database):
                app = JobsApp(self.store)
                with patch.object(app, 'set_interval'), patch.object(app, 'request_sources'):
                    async with app.run_test():
                        origin = app.screen
                        session = self.source_selection()
                        app.session_sources[session.identity] = session
                        selection = app.progress_identity()
                        app.progress_selection = selection
                        sources = Sources(selection, self.job['display_id'])
                        history = ProgressHistory(selection, self.job['display_id'])
                        await app.push_screen(sources)
                        await app.push_screen(history)
                        for screen, selector in ((origin, '#machine'), (origin, '#message'),
                                                 (sources, '#sources-validation'), (history, '#history-state')):
                            await screen.query_one(selector).remove()
                        app.progress_snapshot = object()
                        app.accept_progress(selection, app.progress_generation, None, 'changed evidence',
                                            database_changed=changed_database,
                                            invalid_session=None if changed_database else session)
                        self.assertEqual(app.session_sources, {})
                        self.assertIsNone(app.progress_snapshot)
                        self.assertTrue(sources.expired)
                        self.assertTrue(history.expired)
                        self.assertEqual(app.progress_selection, app.progress_identity())

    async def test_collection_and_catchup_continue_without_base_render_widgets(self):
        context = object()
        snapshot = SimpleNamespace(completeness='CATCHING_UP', sources=())
        service = Mock()
        service.poll.return_value = snapshot
        resolver = Mock(return_value=context)
        app = JobsApp(self.store, progress_service=service, progress_resolver=resolver)
        with patch.object(app, 'set_interval'), patch.object(app, 'set_timer') as timer:
            async with app.run_test():
                selection = app.progress_identity()
                app.progress_selection = selection
                await app.screen.query_one('#machine').remove()
                timer.reset_mock()
                with patch.object(app, 'run_worker') as worker:
                    self.collect_progress(app)
                    worker.assert_called_once()
                    self.assertTrue(app.progress_busy)
                    # Execute the captured read synchronously; no thread, daemon,
                    # external process or real observation provider is started.
                    with patch.object(app, 'call_from_thread', side_effect=lambda callback, *args: callback(*args)):
                        worker.call_args.args[0]()
                resolver.assert_called_once()
                service.poll.assert_called_once_with(context)
                service.retain.assert_called_once_with((context,))
                self.assertIs(app.progress_snapshot, snapshot)
                self.assertFalse(app.progress_busy)
                timer.assert_called_once_with(.05, app.collect_progress)

    async def test_shutdown_still_rejects_late_evidence_and_does_not_restart_collection(self):
        app = JobsApp(self.store)
        with patch.object(app, 'set_interval'):
            async with app.run_test():
                selection = app.progress_identity()
                generation = app.progress_generation
        app.progress_service = Mock()
        with patch.object(app, 'run_worker') as worker, patch.object(app, 'set_timer') as timer:
            app.accept_execution(None, app.execution_generation, object(), None)
            app.accept_progress(selection, generation, object(), None)
            await app.accept_sources(None, selection, generation, object(), None)
            self.collect_progress(app)
        self.assertIsNone(app.execution_observation)
        self.assertIsNone(app.progress_snapshot)
        self.assertEqual(app.session_sources, {})
        app.progress_service.retain.assert_not_called()
        worker.assert_not_called()
        timer.assert_not_called()
