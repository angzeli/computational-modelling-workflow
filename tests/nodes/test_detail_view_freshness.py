"""A frozen node view keeps the receipt of its own displayed payload."""
import unittest
from unittest.mock import patch

from cmw.nodes.protocol import NodeError
from tests.jobs.test_doctor_ui import DoctorBackend
from tests import waiting

try:
    from textual.widgets import TextArea
    from cmw.doctor.presentation import DoctorReport
    from cmw.nodes.portal import NodesApp
except ImportError:
    NodesApp = None


@unittest.skipIf(NodesApp is None, 'Optional Jobs console unavailable')
class FrozenNodeViewTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, predicate):
        await waiting.until(predicate, timeout=5, interval=.01, what='Detail view condition did not become true')

    async def check_view(self, action):
        now = [100.]
        backend = DoctorBackend('OWNER', remote=True)
        app = NodesApp({'OWNER': backend}, clock=lambda: now[0], wall_clock=lambda: 1700000000.)
        lane = app.lanes['OWNER']
        caller = {'runtime': {'identity': {'fingerprint': 'caller'}},
                  'capabilities': [], 'findings': [], 'observation': {}}
        with patch('cmw.doctor.service.inspect_local', return_value=caller):
            async with app.run_test(size=(100, 30)) as pilot:
                await self.until(lambda: bool(app.rows) and not lane.pending and not lane.busy)
                if action == 'doctor':
                    await pilot.press('enter', 'd')
                    await self.until(lambda: isinstance(app.screen, DoctorReport) and not app.screen.pending)
                    self.assertIsNotNone(app.screen.report, app.screen.query_one(TextArea).text)
                else:
                    getattr(app, 'action_' + action)()
                    await pilot.pause()
                view = app.screen
                self.assertIsNot(view, app.screen_stack[0])
                retained = view.query_one(TextArea).text
                self.assertTrue(app.view_commands_allowed(view))
                now[0] += 3
                app.accept('OWNER', ('status', {}, None, None), None, NodeError('DISCONNECTED', 'stream lost'))
                frame = view.query_one('.node-view-frame')
                self.assertTrue(frame.border_title)
                self.assertIn('last reported', frame.border_title.lower())
                self.assertIn('22:13:20 UTC · 3s ago', frame.border_title)
                self.assertEqual(view.query_one(TextArea).text, retained)
                if action == 'details':
                    self.assertFalse(view.check_action('doctor', ()))
                    with patch.object(app, 'queue') as queue:
                        view.action_doctor()
                        queue.assert_not_called()
                now[0] = lane.next_poll
                app.tick()
                await self.until(lambda: lane.fresh(now[0]))
                self.assertTrue(frame.border_title, 'A new status cannot refresh the frozen display')
                self.assertEqual(view.query_one(TextArea).text, retained)
                if action == 'copy_snapshot':
                    target = backend.jobs[0]['target']
                    app.collect_progress()
                    app.queue('OWNER', 'observe', target=target)
                    await self.until(lambda: all(
                        (receipt := app.view_receipt('OWNER', op, target)) is not None
                        and receipt.generation == app.view_receipt('OWNER', 'status').generation
                        for op in ('progress', 'observe')))
                    await pilot.click('#copy-refresh')
                    self.assertFalse(frame.border_title)
                    self.assertTrue(app.view_commands_allowed(view))

    async def test_details_disables_doctor_until_reopened(self):
        await self.check_view('details')

    async def test_doctor_report_remains_last_reported(self):
        await self.check_view('doctor')

    async def test_scheduling_remains_last_reported(self):
        await self.check_view('scheduling')

    async def test_copy_refresh_requires_new_component_receipts(self):
        await self.check_view('copy_snapshot')
