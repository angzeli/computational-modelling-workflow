"""An owner read failure qualifies every mounted scientific and catalogue view."""

from dataclasses import replace
import unittest

from cmw.nodes.protocol import NodeError

try:
    from textual.widgets import Button, Select
    from cmw.jobs.catalogue_models import CatalogueQuery
    from cmw.jobs.study_ui import StudyScreen
    from cmw.nodes.portal import NodesApp
    from tests.jobs.test_catalogue_ui import CatalogueBackend
    from tests.jobs.test_study_ui import StudyBackend
except ImportError:
    NodesApp = None


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class RetainedViewsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = StudyBackend()
        self.app = NodesApp({'TEST': self.backend}, clock=lambda: 100., wall_clock=lambda: 1800000000.)

    async def until(self, pilot, condition):
        for _ in range(100):
            if condition():
                return
            await pilot.pause(.02)
        self.fail('View did not receive its requested observation')

    def disconnect(self):
        self.app.accept('TEST', ('status', {}, None, None, True), None,
                        NodeError('UNREACHABLE', 'synthetic background failure'))

    def recover_lane(self):
        for op in ('capabilities', 'status'):
            self.app.accept('TEST', (op, {}, None, None, True), self.backend.call(op), None)
        self.assertTrue(self.app.lanes['TEST'].fresh(self.app.clock()))

    def assert_retained(self, screen):
        banner = screen.query_one('.node-receipt-banner')
        self.assertTrue(banner.display)
        self.assertIn('last reported', str(banner.render()).lower())
        self.assertFalse(self.app.view_commands_allowed(screen))

    async def test_history_and_nested_entry_keep_their_original_page_receipt(self):
        app = self.app
        async with app.run_test() as pilot:
            await self.until(pilot, lambda: bool(app.rows))
            app.action_history()
            await self.until(pilot, lambda: app.history_view and app.history_view.page)
            history = app.history_view
            history.action_details()
            await self.until(pilot, lambda: app.screen is not history and app.screen.is_mounted)
            entry = app.screen
            self.disconnect()
            self.assert_retained(history)
            self.assert_retained(entry)
            self.assertFalse(history.check_action('next', ()))
            self.assertIn(self.backend.alias,
                          str(entry.query_one('#history-detail').render()))
            self.recover_lane()
            self.assert_retained(history)
            self.assert_retained(entry)

    async def test_sources_keeps_inspection_and_refuses_session_commands(self):
        app = self.app
        async with app.run_test() as pilot:
            await self.until(pilot, lambda: bool(app.rows))
            app.action_sources()
            await self.until(pilot, lambda: app.sources_view and app.sources_view.inspection)
            view = app.sources_view
            inspection = view.inspection
            self.disconnect()
            self.assert_retained(view)
            self.assertIs(view.inspection, inspection)
            for command in ('clear', 'validate', 'use'):
                self.assertTrue(view.query_one('#sources-' + command, Button).disabled)
            self.recover_lane()
            self.assert_retained(view)
            app.clear_session_source(view)
            self.assertFalse(any(request[0] == 'sources.clear' for request in app.lanes['TEST'].pending))

    async def test_search_and_filtered_rows_keep_page_but_withhold_authority(self):
        app = self.app
        async with app.run_test() as pilot:
            await self.until(pilot, lambda: bool(app.rows))
            app.catalogue_changed(('apply', CatalogueQuery(text='literal.*'), None))
            await self.until(pilot, lambda: bool(app.catalogue_pages))
            before = set(app.rows)
            self.assertTrue(before)
            app.action_search()
            await self.until(pilot, lambda: bool(app.screen.query('#catalogue-apply')))
            view = app.screen
            self.disconnect()
            self.assert_retained(view)
            self.assertTrue(view.query_one('#catalogue-apply', Button).disabled)
            self.assertTrue(view.query_one('#catalogue-next', Button).disabled)
            self.recover_lane()
            self.assertEqual(set(app.rows), before)
            self.assertTrue(all(job['_catalogue_stale'] for _, job in app.rows.values()))
            self.assertIn('last reported', app.catalogue_summary().lower())
            self.assert_retained(view)
            app.tick()
            await self.until(pilot, lambda: app.view_commands_allowed(view))
            self.assertFalse(view.query_one('.node-receipt-banner').display)
            self.assertTrue(all(not job.get('_catalogue_stale') for _, job in app.rows.values()))

    async def test_search_commands_can_still_target_the_healthy_owner(self):
        peer = CatalogueBackend('PEER')
        app = self.app = NodesApp({'TEST': self.backend, 'PEER': peer}, clock=lambda: 100.)
        async with app.run_test() as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            app.catalogue_changed(('apply', CatalogueQuery(text='literal.*'), None))
            await self.until(pilot, lambda: len(app.catalogue_pages) == 2)
            for owner, page in app.catalogue_pages.items():
                app.catalogue_pages[owner] = replace(page, next_cursor='synthetic-next')
            app.action_search()
            await self.until(pilot, lambda: bool(app.screen.query('#catalogue-owner')))
            view = app.screen
            view.query_one('#catalogue-owner', Select).value = 'PEER'
            await self.until(pilot, lambda: not view.query_one('#catalogue-next', Button).disabled)
            self.disconnect()
            self.assertFalse(view.query_one('#catalogue-next', Button).disabled)
            self.assertFalse(view.query_one('#catalogue-apply', Button).disabled)
            self.assertEqual(view.eligible_owners(), ('PEER',))
            view.query_one('#catalogue-owner', Select).value = 'TEST'
            await self.until(pilot, lambda: view.query_one('#catalogue-next', Button).disabled)
            before = len(self.backend.queries)
            view.action_apply_query()
            await self.until(pilot, lambda: len(peer.queries) == 2)
            self.assertEqual(len(self.backend.queries), before)

    async def test_catalogue_details_disables_navigation_that_requests_more_data(self):
        app = self.app
        async with app.run_test() as pilot:
            await self.until(pilot, lambda: bool(app.rows))
            app.action_catalogue()
            await self.until(pilot, lambda: getattr(app.screen, 'record', None))
            view = app.screen
            record = view.record
            self.disconnect()
            self.assert_retained(view)
            for button in ('catalogue-study', 'catalogue-members'):
                self.assertTrue(view.query_one('#' + button, Button).disabled)
            view.action_study()
            self.assertIs(app.screen, view)
            self.assertIs(view.record, record)
            self.recover_lane()
            self.assert_retained(view)

    async def test_study_keeps_its_own_receipt_and_record(self):
        app = self.app
        async with app.run_test() as pilot:
            await self.until(pilot, lambda: bool(app.rows))
            app.action_catalogue()
            await self.until(pilot, lambda: getattr(app.screen, 'record', None))
            details = app.screen
            details.action_study()
            await self.until(pilot, lambda: isinstance(app.screen, StudyScreen) and app.screen.record)
            study = app.screen
            record = study.record
            self.disconnect()
            self.assert_retained(study)
            self.assert_retained(details)
            self.recover_lane()
            self.assert_retained(study)
            self.assertIs(study.record, record)
            self.assertIn('NOT ASSESSED', str(study.query_one('#study-body').render()))
