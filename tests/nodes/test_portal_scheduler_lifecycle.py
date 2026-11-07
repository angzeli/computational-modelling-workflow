"""Node observation scheduling is independent of a mounted render surface."""
from collections import deque
import unittest
from unittest.mock import Mock, patch

from cmw.nodes.contracts import NodeState
from tests.nodes.test_portal import FakeBackend, NodesApp

if NodesApp is not None:
    from textual.containers import Vertical
    from textual.screen import Screen
    from textual.widgets import Button, DataTable, Static
    from cmw.nodes.portal import row_key
    from cmw.nodes.portal_views import NodeControlConfirm


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class PortalSchedulerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.
        self.backend = FakeBackend('synthetic')
        self.backend.reconnect = Mock()
        self.app = NodesApp({'node': self.backend}, clock=lambda: self.now,
                            wall_clock=lambda: 1000. + self.now)
        self.lane = self.app.lanes['node']
        # These tests drive the status lane, not selected-job progress polling.
        self.lane.next_progress = float('inf')
        self.workers = deque()
        self.enterContext(patch.object(self.app, 'set_interval', return_value=Mock()))
        self.enterContext(patch.object(self.app, 'run_worker', side_effect=self.schedule))
        self.enterContext(patch.object(self.app, 'call_from_thread',
                                      side_effect=lambda callback, *args: callback(*args)))

    def schedule(self, work, **kwargs):
        # Keep the real worker body and accept path, but control delivery without
        # a thread, wall-clock delay, or waiting for a timer to happen to fire.
        self.workers.append(work)

    def deliver(self, operation):
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.lane.active_request[0], operation)
        self.workers.popleft()()
        self.assertEqual(self.backend.calls[-1][0], operation)

    def establish_status(self):
        self.deliver('capabilities')
        self.assertFalse(self.lane.handshake_required)
        self.assertFalse(self.lane.fresh(self.now))
        self.deliver('status')
        self.assertTrue(self.lane.fresh(self.now))
        self.assertFalse(self.workers)
        self.lane.next_progress = float('inf')

    def exercise_observation_lane(self, *, after_expiry=lambda: None):
        self.assertTrue(self.lane.fresh(self.now))
        self.now += 4.999
        self.app.tick()
        self.assertFalse(self.workers)
        self.now = 105.
        self.app.tick()
        self.deliver('status')
        self.assertEqual(self.lane.received, self.now)

        old_receipt = self.lane.receipts['status']
        generation = self.lane.observation_generation
        self.now += 16.
        self.app.tick()
        # Expiry must happen before the outstanding status response arrives.
        self.assertEqual(self.lane.observation_generation, generation + 1)
        self.assertFalse(self.lane.fresh(self.now))
        self.assertFalse(self.app.receipt_current('node', old_receipt))
        after_expiry()
        self.deliver('status')
        self.assertTrue(self.lane.fresh(self.now))
        self.assertFalse(self.app.receipt_current('node', old_receipt))

        retained = self.lane.state
        self.backend.fail = True
        for failure in range(1, 4):
            self.now += 5.
            self.app.tick()
            self.deliver('status' if failure == 1 else 'capabilities')
            self.assertEqual(self.lane.reachability.consecutive_failures, failure)
            self.assertEqual(self.lane.reachability.state,
                             NodeState.REACHABLE if failure < 3 else NodeState.UNREACHABLE)
            self.assertTrue(self.lane.handshake_required)
            self.assertFalse(self.lane.fresh(self.now))
            self.assertIs(self.lane.state, retained)
        self.assertEqual(self.backend.reconnect.call_count, 2)

        self.backend.fail = False
        self.backend.jobs[0]['name'] = 'recovered'
        self.backend.jobs[1]['status'] = 'Run'
        self.now += 5.
        self.app.tick()
        self.deliver('capabilities')
        self.assertEqual(self.backend.reconnect.call_count, 3)
        self.assertEqual(self.lane.reachability.state, NodeState.REACHABLE)
        self.assertEqual(self.lane.reachability.consecutive_failures, 0)
        self.assertFalse(self.lane.handshake_required)
        self.assertFalse(self.lane.fresh(self.now))
        self.deliver('status')
        self.assertTrue(self.lane.fresh(self.now))
        self.assertEqual(self.lane.state['jobs'][0]['name'], 'recovered')
        self.assertFalse(self.workers)

    async def test_missing_surface_keeps_expiry_polling_and_reconnect_alive(self):
        self.assertFalse(self.app.screen_stack)
        self.assertFalse(self.app.render_available())
        with patch.object(self.app, '_running', True):
            self.app.tick()
            self.establish_status()
            self.exercise_observation_lane()
        self.assertFalse(self.app.screen_stack)

    async def test_partial_surface_keeps_lane_and_modal_live_then_renders_latest_data(self):
        async with self.app.run_test(size=(120, 40)):
            self.establish_status()
            origin = self.app.screen
            await origin.query_one('#node-summary').remove()
            self.assertTrue(origin.query('#table'))
            self.assertFalse(self.app.render_available())

            target = self.backend.jobs[0]['target']
            view = NodeControlConfirm('node', target, 'Synthetic job', {'action': 'cancel'})
            await self.app.push_screen(self.app.bind_node_view(
                view, 'node', received=self.lane.receipts['status']))
            self.app.sync_node_view(view)
            confirm = view.query_one('#node-control-confirm', Button)
            self.assertFalse(confirm.disabled)

            def expired_view():
                self.assertTrue(confirm.disabled)
                self.assertIn('Last reported', str(view.node_view_frame.border_title))
                self.assertFalse(self.app.view_commands_allowed(view))

            self.exercise_observation_lane(after_expiry=expired_view)
            # A recovered lane cannot bless data retained by this particular view.
            expired_view()
            self.app.view_received(view, 'node', self.lane.receipts['status'])
            self.assertFalse(confirm.disabled)
            self.assertFalse(view.node_frame_captions)
            await self.app.pop_screen()

            summary = Static(id='node-summary', markup=False)
            await origin.query_one('#portal-frame').mount(summary, before='#table')
            self.app.tick()
            self.assertTrue(self.app.render_available())
            self.assertFalse(self.workers)
            self.assertIn('Online', str(summary.render()))
            self.assertIn('Run 2', str(summary.render()))
            table = origin.query_one('#table', DataTable)
            self.assertEqual(table.row_count, len(self.backend.jobs))
            self.assertEqual(str(table.get_cell(row_key(target), 'name')), 'recovered')

    async def test_shutdown_does_not_dispatch_pending_or_due_requests(self):
        with patch.object(self.app, '_running', True):
            self.app.queue('node', 'capabilities')
            self.app.closed = True
            self.app.tick()
        self.assertFalse(self.workers)
        self.assertFalse(self.backend.calls)
        self.backend.reconnect.assert_not_called()
        self.assertIsNone(self.lane.active_request)
        self.assertFalse(self.lane.busy)

    async def test_replaced_modal_frame_receives_current_freshness(self):
        async with self.app.run_test():
            self.establish_status()
            view = Screen()
            await self.app.push_screen(self.app.bind_node_view(view, 'node'))
            original = Vertical()
            await view.mount(original)
            self.app.sync_node_view(view)
            self.assertIs(view.node_view_frame, original)
            self.assertIn('Last reported', str(original.border_title))
            await original.remove()
            replacement = Vertical()
            await view.mount(replacement)
            self.app.sync_node_view(view)
            self.assertIs(view.node_view_frame, replacement)
            self.assertIn('Last reported', str(replacement.border_title))
            self.app.view_received(view, 'node', self.lane.receipts['status'])
            self.assertIsNone(replacement.border_title)
            self.assertTrue(self.app.view_commands_allowed(view))
