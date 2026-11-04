"""Portal receive-time freshness and five-second observation scheduling."""
import unittest
from unittest.mock import Mock, patch

from cmw.nodes.contracts import NodeEvent, transition
from cmw.nodes.protocol import NodeError
from tests.nodes.test_portal import FakeBackend, NodesApp


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class PortalPollingTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 100., 1000.
        self.backend = FakeBackend('synthetic')
        self.app = NodesApp({'node': self.backend}, clock=lambda: self.now, wall_clock=lambda: self.wall)
        self.app.render_state = Mock()
        self.lane = self.app.lanes['node']
        for operation in ('capabilities', 'status'):
            self.lane.request_started, self.lane.request_wall_started = self.now, self.wall
            self.app.accept('node', (operation, {}, None, None, False), self.backend.call(operation), None)

    def test_inclusive_fifteen_seconds_and_no_future_receipt(self):
        for age, fresh in ((0., True), (10.1, True), (15., True), (15.001, False), (-.001, False)):
            with self.subTest(age=age):
                self.assertEqual(self.lane.fresh(100. + age), fresh)

    def test_wall_clock_steps_do_not_change_freshness(self):
        for wall in (-100000., 100000.):
            self.wall = wall
            self.now = 115.
            self.assertTrue(self.lane.fresh(self.now))
            self.now = 115.001
            self.assertFalse(self.lane.fresh(self.now))

    def test_status_cadence_is_five_seconds(self):
        with patch.object(self.app, '_running', True), patch.object(self.app, 'query', return_value=[object()]), \
             patch.object(self.app, 'run_worker') as worker:
            self.app.tick()
            self.assertEqual(self.lane.active_request[0], 'status')
            self.assertEqual(self.lane.next_poll, 105.)
            self.lane.active_request, self.lane.busy = None, False
            self.now = 104.999
            self.app.tick()
            self.assertEqual(worker.call_count, 1)
            self.now = 105.
            self.app.tick()
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(self.lane.next_poll, 110.)

    def test_fresh_receipt_never_overrides_refusal_or_required_handshake(self):
        for code in ('DISCONNECTED', 'AUTH_FAILED', 'INCOMPATIBLE_CAPABILITIES',
                     'SSH_IDENTITY_CHANGED', 'RUNTIME_MISMATCH', 'NODE_NOT_READY'):
            with self.subTest(code=code):
                self.app.refuse_lane('node', NodeError(code, 'refused'), 'status')
                self.lane.received = self.now
                self.assertFalse(self.lane.fresh(self.now))
                self.lane.reachability = transition(self.lane.reachability, NodeEvent.SUCCESS, received_at=self.now)
                self.assertFalse(self.lane.fresh(self.now))
