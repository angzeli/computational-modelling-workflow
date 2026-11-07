"""Queued refreshes belong to the mounted view that requested them."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.store import Store

try:
    from textual.app import App
    from textual.widgets import Static, TextArea
    from cmw.doctor.presentation import DoctorReport
    from cmw.jobs.tui import JobsApp, Inspect, ExternalInspect, SharingInspect
    from cmw.jobs.history_ui import HistoryDetails, ProgressHistory
    from cmw.jobs.catalogue_ui import CatalogueSearch, CatalogueDetails
    from cmw.jobs.study_ui import StudyScreen
    from cmw.jobs.review_ui import ReviewScreen
    from cmw.jobs.sources_ui import Sources
    from cmw.nodes.portal import NodesApp
    from cmw.nodes.portal_views import FetchDialog, NodeFiles, NodeControlConfirm, NodeOrderPrompt
    from cmw.nodes.view_freshness import ViewReceipt
    from cmw.progress.models import HistoryEntry
    from tests.jobs.test_review_ui import review
    from tests.nodes.test_portal import FakeBackend
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require the optional Jobs extra')
class RefreshLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-refresh-lifecycle-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root/'queue')
        self.job = self.store.add(argv=['/never/executed'], cwd=self.root, name='Synthetic retained view', hold=True)
        for method in ('collect_activity', 'collect_progress', 'collect_execution'):
            self.enterContext(patch.object(JobsApp, method))

    async def test_refresh_after_machine_removal_and_screen_removal_does_not_query_or_read(self):
        app = JobsApp(self.store)
        timers = []
        def interval(*args, **kwargs):
            timer = Mock()
            timers.append(timer)
            return timer
        with patch.object(app, 'set_interval', side_effect=interval):
            async with app.run_test() as pilot:
                origin = app.screen
                self.assertTrue(str(origin.query_one('#machine', Static).render()))
                # Capture the real resize callback, then deliver it after the
                # first relevant child disappears but both tables still exist.
                with patch.object(origin, 'call_after_refresh', return_value=True) as queued:
                    app.on_resize()
                callback = queued.call_args.args[0]
                await origin.query_one('#machine').remove()
                self.assertTrue(origin.query('#table'))
                self.assertTrue(origin.query('#external'))
                with patch.object(self.store, 'snapshot', side_effect=AssertionError('late refresh read')):
                    callback()
                    app.accept_progress(None, 0, None, 'late result', database_changed=True)
                    app.accept_execution(None, 0, None, 'late result')
                await pilot.pause()
        self.assertFalse(origin.is_attached)
        with patch.object(self.store, 'snapshot', side_effect=AssertionError('detached refresh read')):
            callback()
        self.assertEqual(len(timers), 4)
        for timer in timers:
            timer.stop.assert_called_once_with()

    async def test_live_refresh_does_not_swallow_unrelated_failure(self):
        app = JobsApp(self.store)
        with patch.object(app, 'set_interval'):
            async with app.run_test() as pilot:
                with patch.object(app, '_refresh_state', side_effect=RuntimeError('render defect')):
                    with self.assertRaisesRegex(RuntimeError, 'render defect'):
                        app.refresh_state()

    async def test_late_activity_reply_after_detach_is_not_accepted(self):
        app = JobsApp(self.store)
        catchup = Mock()
        with patch.object(app, 'set_interval'), patch.object(app, 'set_timer', return_value=catchup):
            async with app.run_test():
                before = deepcopy(app.activity_projection)
                app.schedule_progress()
        with patch.object(app, 'refresh_state') as refresh:
            app.accept_activity({'machine_usage': {'late': True}})
        self.assertEqual(app.activity_projection, before)
        refresh.assert_not_called()
        catchup.stop.assert_called_once_with()

    async def test_periodic_detail_views_render_on_mount_and_stop_after_removal(self):
        entry = HistoryEntry('entry', 1, native_label='synthetic observation')
        history = SimpleNamespace(entries={'entry': entry}, expired=False,
                                  page=SimpleNamespace(source_revision='source'))
        views = (
            (Inspect(self.store, self.job['display_id']), '#body'),
            (ExternalInspect({'id': 'external'}, provider=Mock(return_value=None)), '#external-body'),
            (SharingInspect(self.store.snapshot), '#sharing-body'),
            (HistoryDetails(history, entry), '#history-detail'),
        )
        for view, selector in views:
            with self.subTest(view=type(view).__name__):
                app = App()
                timer = Mock()
                with patch.object(view, 'set_interval', return_value=timer), \
                        patch('cmw.jobs.tui.external_detail', return_value='Synthetic external observation'):
                    async with app.run_test() as pilot:
                        await app.push_screen(view)
                        # External detail polls first; the other views render in Mount.
                        view.refresh_content()
                        self.assertTrue(str(view.query_one(selector, Static).render()))
                        await app.pop_screen()
                        with patch.object(view, 'query_one', side_effect=AssertionError('removed view query')):
                            view.refresh_content()
                        await pilot.pause()
                timer.stop.assert_called_once_with()

    async def test_review_stops_polling_and_ignores_reply_after_unmount(self):
        requests = []
        def request(operation, params, callback):
            requests.append((operation, callback))
        view = ReviewScreen('synthetic', request, cached=replace(review(self.job), status='PENDING'))
        timer = Mock()
        app = App()
        with patch.object(view, 'set_interval', return_value=timer):
            async with app.run_test() as pilot:
                await app.push_screen(view)
                self.assertEqual([operation for operation, _ in requests], ['review.preview'])
                self.assertTrue(str(view.query_one('#review-body', Static).render()))
                await app.pop_screen()
                with patch.object(view, 'query_one', side_effect=AssertionError('removed review query')):
                    view.poll()
                    requests[0][1](review(self.job), None)
                    view.refresh_node_observation()
                self.assertEqual(len(requests), 1)
                await pilot.pause()
        timer.stop.assert_called_once_with()

    async def test_node_view_unsubscribes_before_late_lane_refresh(self):
        app = NodesApp({'LOCAL': FakeBackend('LOCAL')})
        view = ReviewScreen('synthetic', Mock(), cached=review(self.job))
        timer = Mock()
        receipt = ViewReceipt(1., 1., 0)
        with patch.object(app, 'set_interval', return_value=timer), patch.object(app, 'tick'), \
                patch.object(view, 'set_interval'):
            async with app.run_test() as pilot:
                await app.push_screen(app.bind_node_view(view, 'LOCAL', received=receipt))
                self.assertEqual(view.node_view_receipts, {'LOCAL': receipt})
                await app.pop_screen()
                self.assertFalse(view.node_view_active)
                self.assertEqual(view.node_view_receipts, {'LOCAL': receipt})
                self.assertFalse(app.view_commands_allowed(view))
                with patch.object(view, 'query_one', side_effect=AssertionError('detached lane listener')):
                    app.sync_node_view(view)
                    app.view_received(view, 'LOCAL', ViewReceipt(2., 2., 1))
                self.assertEqual(view.node_view_receipts, {'LOCAL': receipt})
                await pilot.pause()
        timer.stop.assert_called_once_with()
        callback = Mock()
        app.accept('LOCAL', ('status', {}, None, callback, False), None, RuntimeError('late reply'))
        callback.assert_not_called()

    async def test_sources_reply_stops_after_field_build_dismisses_the_screen(self):
        app = JobsApp(self.store)
        view = Sources(('fixture', self.job['id'], self.job['attempt_id']), self.job['display_id'])
        context = SimpleNamespace(engine='SYNTHETIC', binding_status='SOURCE_UNRESOLVED',
                                  attempt_id=self.job['attempt_id'], association='fixture',
                                  sources=(), limitations=())
        result = SimpleNamespace(context=context, plans=(), declarations=())
        with patch.object(app, 'set_interval'), patch.object(app, 'request_sources'):
            async with app.run_test() as pilot:
                await app.push_screen(view)
                async def build_and_dismiss():
                    await app.pop_screen()
                with patch.object(view, 'build_fields', side_effect=build_and_dismiss):
                    await view.accept_result('inspect', result, None, view.request_generation)
                self.assertTrue(view.closed)
                self.assertNotIn(view, app.screen_stack)
                self.assertIsNone(app.sources_view)
                await pilot.pause()

    async def test_app_detach_invalidates_queued_navigation_restoration(self):
        app = JobsApp(self.store)
        queued = []
        def defer(callback, *args):
            queued.append((callback, args))
            return True
        with patch.object(app, 'set_interval'):
            async with app.run_test():
                snapshot = app.capture_view()
                with patch.object(app, 'call_after_refresh', side_effect=defer):
                    app.restore_view(snapshot)
                self.assertTrue(app.navigation_pending())
        self.assertFalse(app._navigation_restores)
        self.assertFalse(app._navigation_frames)
        for callback, args in queued:
            callback(*args)

    async def test_doctor_reply_and_deferred_load_ignore_removed_text_area(self):
        view = DoctorReport(None, 'synthetic')
        app = App()
        async with app.run_test():
            await app.push_screen(view)
            await view.query_one(TextArea).remove()
            loader = Mock(side_effect=AssertionError('late inspection'))
            view.load(loader)
            view.accept(None, None, 'late reply')
            self.assertFalse(view.started)
            loader.assert_not_called()

    async def test_sources_and_history_ignore_partially_removed_response_surfaces(self):
        selection = ('fixture', self.job['id'], self.job['attempt_id'])
        app = JobsApp(self.store)
        with patch.object(app, 'set_interval'), patch.object(app, 'request_sources'):
            async with app.run_test():
                sources = Sources(selection, self.job['display_id'])
                await app.push_screen(sources)
                await sources.query_one('#sources-inspection').remove()
                await sources.accept_result('inspect', None, 'late reply', sources.request_generation)
                await sources.build_fields()
                sources.node_commands_changed(False)
                self.assertIsNone(sources.inspection)
                await app.pop_screen()
                history = ProgressHistory(selection, self.job['display_id'])
                await app.push_screen(history)
                await history.query_one('#history-state').remove()
                history.accept_page(None, None, history.request_generation)
                self.assertIsNone(history.page)
                history.invalidate()

    async def test_catalogue_and_study_replies_ignore_removed_response_surfaces(self):
        for view, selector, error_args in (
                (CatalogueDetails('synthetic', Mock()), '#catalogue-body', (None, None, 'late reply')),
                (StudyScreen('synthetic', Mock()), '#study-body', (None, 'late reply'))):
            with self.subTest(view=type(view).__name__):
                app = App()
                async with app.run_test():
                    await app.push_screen(view)
                    callback = view.request.call_args.args[0]
                    await view.query_one(selector).remove()
                    callback(*error_args)
                    self.assertIsNone(view.record)

    async def test_lane_callbacks_ignore_partially_removed_dialog_controls(self):
        target = {'node_id': 'node', 'queue_id': 'queue', 'attempt_id': 'attempt'}
        search = CatalogueSearch(None, {}, 'synthetic')
        search.node_view_receipts = {}
        cases = (
            (search, '#catalogue-owner'),
            (CatalogueDetails('synthetic', Mock()), '#catalogue-members'),
            (FetchDialog('LOCAL', target, 'synthetic'), '#fetch-confirm'),
            (NodeFiles('LOCAL', target, 'synthetic', {'locations': []}, local=True), '#node-files-open'),
            (NodeControlConfirm('LOCAL', target, 'synthetic', {'action': 'cancel'}), '#node-control-check'),
            (NodeOrderPrompt('synthetic'), '#answer'),
        )
        for view, selector in cases:
            with self.subTest(view=type(view).__name__):
                app = NodesApp({'LOCAL': FakeBackend('LOCAL')})
                with patch.object(app, 'set_interval'), patch.object(app, 'tick'):
                    async with app.run_test():
                        await app.push_screen(view)
                        await view.query_one(selector).remove()
                        view.node_commands_changed(False)
                        if view is search:
                            await view.query_one('#catalogue-other-receipts').remove()
                            view.show_other_receipts(('late receipt',))
