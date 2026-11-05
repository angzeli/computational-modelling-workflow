"""A retained file location or control preview cannot authorize a new command."""
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

from cmw.nodes.protocol import NodeError
from tests.nodes import test_portal
from tests.nodes.test_portal import FakeBackend, NodesApp

if NodesApp is not None:
    from cmw.jobs.copy_ui import FrozenText
    from cmw.nodes.portal import row_key
    from cmw.nodes.portal_views import FetchDialog, NodeControlConfirm, NodeFiles, NodeOrderPrompt
    from textual.widgets import Input


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class FileControlViewFreshnessTests(unittest.IsolatedAsyncioTestCase):
    until = test_portal.PortalTests.until

    @asynccontextmanager
    async def opened(self, action, screen_type):
        now = [100.]
        backend = FakeBackend('owner')
        app = NodesApp({'owner': backend}, clock=lambda: now[0])
        async with app.run_test(size=(120, 40)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 5)
            app.select(row_key(backend.jobs[0]['target']))
            getattr(app, 'action_' + action)()
            await self.until(pilot, lambda: isinstance(app.screen, screen_type)
                             and app.screen.is_mounted and bool(app.screen.query('.node-view-frame')))
            self.assertTrue(app.view_commands_allowed(app.screen))
            yield app, pilot, now

    def assert_retained(self, app, view):
        frame = view.query_one('.node-view-frame')
        self.assertTrue(frame.border_title)
        self.assertIn('last reported', frame.border_title.lower())
        self.assertFalse(app.view_commands_allowed(view))

    def disconnect(self, app):
        app.refuse_lane('owner', NodeError('DISCONNECTED', 'synthetic read failure'), 'observe')
        self.assert_retained(app, app.screen)

    async def test_files_disable_fetch_and_local_revalidation_until_reopened(self):
        async with self.opened('files', NodeFiles) as (app, pilot, now):
            files = app.screen
            self.disconnect(app)
            self.assertTrue(files.query_one('#node-files-open').disabled)
            self.assertTrue(files.query_one('#node-files-fetch').disabled)
            self.assertFalse(files.query_one('#node-files-copy').disabled)
            with patch.object(app, 'queue') as send:
                app.open_local_location(files, files.selected_location())
                send.assert_not_called()
            now[0] += 5
            app.tick()
            await self.until(pilot, lambda: app.lanes['owner'].fresh(now[0]))
            self.assert_retained(app, files)
            await pilot.press('escape')
            app.action_files()
            await self.until(pilot, lambda: isinstance(app.screen, NodeFiles) and app.screen is not files
                             and app.screen.is_mounted and app.view_commands_allowed(app.screen))
            self.assertFalse(app.screen.query_one('#node-files-fetch').disabled)

    async def test_fetch_disables_submission_and_keeps_underlying_files_stale(self):
        async with self.opened('files', NodeFiles) as (app, pilot, _):
            files = app.screen
            await pilot.click('#node-files-fetch')
            await self.until(pilot, lambda: isinstance(app.screen, FetchDialog) and app.screen.is_mounted
                             and bool(app.screen.query('.node-view-frame')))
            # Failure on the independent transfer connection reaches every owner view.
            app.finish_fetch(app.screen, 'fetch acknowledgment lost', NodeError('DISCONNECTED', 'acknowledgment lost'))
            self.assert_retained(app, app.screen)
            self.assert_retained(app, files)
            self.assertTrue(app.screen.query_one('#fetch-confirm').disabled)
            with patch.object(app, 'run_worker') as start:
                app.start_fetch(app.screen, '/explicit/spec', '/new/destination')
                start.assert_not_called()

    async def test_control_confirmation_disables_preview_and_request_lookup(self):
        async with self.opened('dispatch', NodeControlConfirm) as (app, pilot, _):
            view = app.screen
            self.assertFalse(view.query_one('#node-control-confirm').disabled)
            # A submitted preview would have an original ID to check.
            view.request_id = 'previous-request'
            self.disconnect(app)
            self.assertTrue(view.query_one('#node-control-confirm').disabled)
            self.assertTrue(view.query_one('#node-control-check').disabled)
            with patch.object(app, 'run_worker') as start:
                app.start_control(view, 'stale intent')
                start.assert_not_called()

    async def test_order_disables_apply_and_keyboard_submission(self):
        async with self.opened('order', NodeOrderPrompt) as (app, pilot, _):
            view = app.screen
            self.disconnect(app)
            self.assertTrue(view.query_one('#apply').disabled)
            self.assertTrue(view.query_one('#answer').disabled)
            with patch.object(view, 'dismiss') as submit:
                view.on_input_submitted(Input.Submitted(view.query_one(Input), '2'))
                submit.assert_not_called()

    async def test_frozen_logs_keep_their_contents_and_local_copy(self):
        async with self.opened('logs', FrozenText) as (app, pilot, _):
            self.disconnect(app)
            self.assertIn('bounded stdout of owner', app.screen.query_one('#copy-text').text)
            self.assertFalse(app.screen.query_one('#copy-all').disabled)
