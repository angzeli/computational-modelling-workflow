"""Per-node failure authority and handshake recovery without real transports."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from cmw.nodes.contracts import NodeState, NodeEvent, SkewLevel
from cmw.nodes.protocol import NodeError
from cmw.nodes.transport_events import TransportFailure, FailureKind
from tests.nodes.test_portal import FakeBackend, NodesApp


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class ReachabilityPortalTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 100., 1000.
        self.backend = FakeBackend('synthetic')
        self.app = NodesApp({'node': self.backend}, clock=lambda: self.now, wall_clock=lambda: self.wall)
        self.app.render_state = Mock()
        self.lane = self.app.lanes['node']

    def reply(self, operation='status', error=None, envelope=None):
        self.lane.request_started = self.now
        self.lane.request_wall_started = self.wall
        self.app.accept('node', (operation, {}, None, None, False),
                        None if error else envelope or self.backend.call(operation), error)

    def ready(self):
        self.reply('capabilities')
        self.reply()
        self.assertTrue(self.lane.fresh(self.now))

    def test_initial_state_needs_compatible_capabilities_and_fresh_status(self):
        self.assertIsNone(self.lane.reachability.state)
        self.reply()
        self.assertFalse(self.lane.fresh(self.now))
        self.reply('capabilities')
        self.assertEqual(self.lane.reachability.state, NodeState.REACHABLE)
        self.assertFalse(self.lane.fresh(self.now))
        self.reply()
        self.assertTrue(self.lane.fresh(self.now))

    def test_three_disconnects_retain_rows_but_disable_controls_from_first(self):
        self.ready()
        previous = deepcopy(self.lane.state)
        for count in range(1, 4):
            self.reply(error=NodeError('DISCONNECTED', 'literal IPv6 address is no longer reachable'))
            self.assertEqual(self.lane.reachability.consecutive_failures, count)
            self.assertEqual(self.lane.reachability.state,
                             NodeState.REACHABLE if count < 3 else NodeState.UNREACHABLE)
            self.assertFalse(self.lane.fresh(self.now))
            self.assertEqual(self.lane.state, previous)
            self.assertTrue(self.lane.handshake_required)
            self.assertEqual(self.lane.next_poll, self.now + 5)

    def test_data_received_after_expiry_cannot_become_current_on_next_status(self):
        self.ready()
        target = self.backend.jobs[0]['target']
        self.now += 16
        self.app.accept('node', ('observe', {}, target, None), self.backend.call('observe', {}, target), None)
        receipt = self.app.view_receipt('node', 'observe', target)
        self.assertFalse(self.app.receipt_current('node', receipt))
        self.reply()
        self.assertFalse(self.app.receipt_current('node', receipt))
        self.app.accept('node', ('observe', {}, target, None), self.backend.call('observe', {}, target), None)
        self.assertTrue(self.app.receipt_current('node', self.app.view_receipt('node', 'observe', target)))

    def test_timeout_uses_same_counted_transition(self):
        self.ready()
        for _ in range(3):
            self.reply(error=TransportFailure('UNREACHABLE', 'timeout', event=NodeEvent.TIMEOUT))
        self.assertEqual(self.lane.reachability.state, NodeState.UNREACHABLE)

    def test_auth_and_incompatible_are_immediate_and_require_handshake(self):
        for code, state in [('AUTH_FAILED', NodeState.AUTH_FAILED),
                            ('INCOMPATIBLE_CAPABILITIES', NodeState.INCOMPATIBLE)]:
            with self.subTest(code=code):
                self.ready()
                self.reply('capabilities', NodeError(code, 'explicit refusal'))
                self.assertEqual(self.lane.reachability.state, state)
                self.assertEqual(self.lane.next_poll, self.now + 30)
                self.reply()
                self.assertEqual(self.lane.reachability.state, state)
                self.assertFalse(self.lane.fresh(self.now))
                self.ready()

    def test_security_and_readiness_refusals_block_before_failure_threshold(self):
        for code, kind in [('SSH_IDENTITY_CHANGED', FailureKind.SECURITY),
                           ('IDENTITY_CHANGED', FailureKind.SECURITY),
                           ('RUNTIME_MISMATCH', FailureKind.SECURITY),
                           ('NODE_NOT_READY', FailureKind.READINESS),
                           ('OWNERSHIP_UNVERIFIABLE', FailureKind.READINESS)]:
            with self.subTest(code=code):
                self.ready()
                self.reply(error=NodeError(code, 'explicit refusal'))
                self.assertEqual(self.lane.refusal, kind)
                self.assertEqual(self.lane.reachability.consecutive_failures, 0)
                self.assertFalse(self.lane.fresh(self.now))

    def test_queued_mutation_is_abandoned_never_replayed_on_recovery(self):
        self.ready()
        callback = Mock()
        self.app.queue('node', 'control', {'action': 'cancel'}, callback=callback)
        self.reply(error=NodeError('OUTCOME_UNKNOWN', 'lost acknowledgement'))
        callback.assert_called_once()
        self.assertEqual(len(self.lane.pending), 0)
        self.ready()
        self.assertFalse(any(op == 'control' for op, *_ in self.backend.calls))

    def test_capability_payload_and_explicit_readiness_are_enforced(self):
        for change, code in [({'operations': []}, FailureKind.INCOMPATIBLE),
                             ({'readiness': {'identity_ready': False}}, FailureKind.SECURITY),
                             ({'readiness': {'identity_ready': True, 'runtime_ready': True,
                                             'ready': False}}, FailureKind.READINESS)]:
            with self.subTest(change=change):
                envelope = self.backend.call('capabilities')
                envelope['result'].update(change)
                self.reply('capabilities', envelope=envelope)
                self.assertEqual(self.lane.refusal, code)
                self.assertTrue(self.lane.handshake_required)

    def test_changed_session_reply_cannot_authorize_controls_or_callback(self):
        self.ready()
        self.backend.session = 'new-session'
        callback = Mock()
        envelope = self.backend.call('control.preview', {'action': 'hold'})
        self.app.accept('node', ('control.preview', {}, None, callback, False), envelope, None)
        self.assertEqual(callback.call_args.args[1].code, 'HANDSHAKE_REQUIRED')
        self.assertFalse(self.lane.fresh(self.now))
        self.ready()

    def test_operation_errors_do_not_invent_transport_failure(self):
        self.ready()
        self.reply('logs', NodeError('INVALID_PARAMS', 'Permission denied; host key changed'))
        self.assertTrue(self.lane.fresh(self.now))
        self.assertEqual(self.lane.reachability.consecutive_failures, 0)

    def test_older_handshake_cannot_clear_newer_mutation_refusal(self):
        self.ready()
        request = ('capabilities', {}, None, None, True, self.lane.refusal_generation)
        response = self.backend.call('capabilities')
        failure = self.app.refuse_lane('node', NodeError('RUNTIME_MISMATCH', 'post-control refusal'), 'control')
        self.assertTrue(failure.outcome_unknown)
        self.app.accept('node', request, response, None)
        self.assertEqual(self.lane.refusal, FailureKind.SECURITY)
        self.assertTrue(self.lane.handshake_required)
        self.assertFalse(self.lane.fresh(self.now))
        self.ready()

    def test_clock_skew_signed_with_uncertainty_display_only(self):
        self.ready()
        self.lane.request_wall_started = 1000.
        self.wall = 1002.
        envelope = self.backend.call('status')
        envelope['observed_at'] = 1061.
        self.app.accept('node', ('status', {}, None, None, False), envelope, None)
        self.assertEqual(self.lane.clock_skew.seconds, 60.)
        self.assertEqual(self.lane.clock_skew.uncertainty_seconds, 1.)
        self.assertEqual(self.lane.clock_skew.level, SkewLevel.RED)
        self.assertTrue(self.lane.fresh(self.now))
        self.assertIn('+60.0s ±1.0s (red)', self.app.connection_text('node'))
        self.lane.request_wall_started = 1003.
        self.app.accept('node', ('status', {}, None, None, False), envelope, None)
        self.assertIsNone(self.lane.clock_skew)
        self.assertTrue(self.lane.fresh(self.now))

    def test_recovery_worker_reconnects_before_capabilities_only(self):
        self.ready()
        self.reply(error=NodeError('DISCONNECTED', 'address changed'))
        self.now += 5
        self.backend.reconnect = Mock()
        # Run the scheduled worker synchronously; no process or UI loop is needed.
        with patch.object(self.app, '_running', True), patch.object(self.app, 'query', return_value=[object()]), \
             patch.object(self.app, 'render_state'), patch.object(self.app, 'run_worker') as worker:
            self.app.tick()
            self.assertEqual(self.lane.active_request[0], 'capabilities')
            with patch.object(self.app, 'call_from_thread') as delivered:
                worker.call_args.args[0]()
            self.backend.reconnect.assert_called_once_with()
            self.assertIsNone(delivered.call_args.args[-1])
