"""Offline decision tables; no endpoint, process, filesystem or network calls."""
import math
import unittest

from cmw.nodes.contracts import (
    CancelAction, CancelMode, NodeEvent, NodeState, POLL_INTERVAL_SECONDS,
    Reachability, SkewLevel, STALE_AFTER_SECONDS, Staleness,
    available_cancel_modes, classify_clock_skew, classify_staleness,
    decide_cancel, estimate_clock_skew, transition,
)
from cmw.nodes.protocol import NodeError


class CancelContractTests(unittest.TestCase):
    def test_complete_escalation_table(self):
        expected = {
            None: (CancelAction.START, CancelAction.START, CancelAction.START),
            CancelMode.GRACEFUL: (CancelAction.NOOP, CancelAction.ESCALATE, CancelAction.ESCALATE),
            CancelMode.SOFT: (CancelAction.REJECT, CancelAction.NOOP, CancelAction.ESCALATE),
            CancelMode.HARD: (CancelAction.REJECT, CancelAction.REJECT, CancelAction.NOOP),
        }
        for current, actions in expected.items():
            for requested, action in zip(CancelMode, actions):
                with self.subTest(current=current, requested=requested):
                    result = decide_cancel('VASP', current, requested)
                    self.assertEqual(result.action, action)
                    self.assertEqual(result.effective_mode, current if action == CancelAction.REJECT else requested)
                    self.assertEqual(result.error_code, 'REQUEST_CONFLICT' if action == CancelAction.REJECT else None)

    def test_engine_availability_and_unsupported_modes(self):
        self.assertEqual(available_cancel_modes('vasp'), tuple(CancelMode))
        self.assertEqual(available_cancel_modes('ORCA'), (CancelMode.HARD,))
        self.assertEqual(available_cancel_modes('Command'), ())
        for engine in ('ORCA', 'unrecognized'):
            for mode in ('graceful', 'soft'):
                result = decide_cancel(engine, None, mode)
                self.assertEqual(result.error_code, 'UNSUPPORTED_OPERATION')
                self.assertIsNone(result.effective_mode)
        self.assertEqual(decide_cancel('ORCA', None, 'hard').action, CancelAction.START)

    def test_no_default_or_synonyms_in_pure_explicit_request(self):
        for mode in (None, 'force', 'now', 'SOFT', True):
            with self.subTest(mode=mode), self.assertRaises(NodeError) as caught:
                decide_cancel('VASP', None, mode)
            self.assertEqual(caught.exception.code, 'INVALID_PARAMS')


class ReachabilityContractTests(unittest.TestCase):
    def test_every_state_event_pair(self):
        expected = {
            None: (None, NodeState.REACHABLE, None, None, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
            NodeState.REACHABLE: (NodeState.REACHABLE, NodeState.REACHABLE, NodeState.REACHABLE,
                                  NodeState.REACHABLE, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
            NodeState.UNREACHABLE: (NodeState.UNREACHABLE, NodeState.REACHABLE, NodeState.UNREACHABLE,
                                    NodeState.UNREACHABLE, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
            NodeState.AUTH_FAILED: (NodeState.AUTH_FAILED, NodeState.REACHABLE, NodeState.AUTH_FAILED,
                                   NodeState.AUTH_FAILED, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
            NodeState.INCOMPATIBLE: (NodeState.INCOMPATIBLE, NodeState.REACHABLE, NodeState.INCOMPATIBLE,
                                    NodeState.INCOMPATIBLE, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
        }
        for state, row in expected.items():
            for event, wanted in zip(NodeEvent, row):
                with self.subTest(state=state, event=event):
                    result = transition(Reachability(state), event, received_at=10)
                    self.assertEqual(result.state, wanted)
                    self.assertEqual(result.requires_handshake, wanted != NodeState.REACHABLE)

    def test_failure_threshold_for_every_state_and_transport_event(self):
        for state in (None, *NodeState):
            for event in (NodeEvent.TRANSPORT_FAILURE, NodeEvent.TIMEOUT):
                for count in range(4):
                    with self.subTest(state=state, event=event, count=count):
                        result = transition(Reachability(state, count, 5), event)
                        wanted = state
                        if state not in (NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE) and count >= 2:
                            wanted = NodeState.UNREACHABLE
                        self.assertEqual(result.state, wanted)
                        self.assertEqual(result.consecutive_failures, count + 1)
                        self.assertEqual(result.received_at, 5)

    def test_mixed_failures_success_reset_and_handshake_recovery(self):
        state = transition(Reachability(), NodeEvent.HANDSHAKE_SUCCESS, received_at=1)
        for event in (NodeEvent.TIMEOUT, NodeEvent.TRANSPORT_FAILURE):
            state = transition(state, event)
        state = transition(state, NodeEvent.SUCCESS, received_at=2)
        self.assertEqual(state.consecutive_failures, 0)
        for event in (NodeEvent.TIMEOUT, NodeEvent.TRANSPORT_FAILURE, NodeEvent.TIMEOUT):
            state = transition(state, event)
        self.assertEqual(state.state, NodeState.UNREACHABLE)
        state = transition(state, NodeEvent.SUCCESS, received_at=3)
        self.assertEqual(state.state, NodeState.UNREACHABLE)
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(transition(state, NodeEvent.HANDSHAKE_SUCCESS, received_at=4).state, NodeState.REACHABLE)

    def test_invalid_event_and_portal_clock(self):
        state = Reachability(NodeState.REACHABLE, 0, 100)
        for event, received in (('unknown', 101), ('SUCCESS', None), ('SUCCESS', 99), ('SUCCESS', math.nan)):
            with self.subTest(event=event, received=received), self.assertRaises(NodeError):
                transition(state, event, received_at=received)
        for count in (-1, True, 1.5):
            with self.subTest(count=count), self.assertRaises(NodeError):
                Reachability(consecutive_failures=count)

    def test_poll_and_staleness_boundary(self):
        self.assertEqual(POLL_INTERVAL_SECONDS, 5)
        self.assertEqual(STALE_AFTER_SECONDS, 15)
        self.assertEqual(classify_staleness(None, 100), Staleness.UNAVAILABLE)
        for age, expected in ((0, Staleness.FRESH), (14.999, Staleness.FRESH),
                              (15, Staleness.FRESH), (15.001, Staleness.STALE)):
            with self.subTest(age=age):
                self.assertEqual(classify_staleness(100, 100 + age), expected)
        for received, now in ((100, 99), (math.inf, 100), (100, math.nan), (True, 100)):
            with self.subTest(received=received, now=now), self.assertRaises(NodeError):
                classify_staleness(received, now)

    def test_staleness_and_reachability_remain_independent(self):
        state = Reachability(NodeState.REACHABLE, received_at=10)
        self.assertEqual(classify_staleness(state.received_at, 30), Staleness.STALE)
        self.assertEqual(state.state, NodeState.REACHABLE)


class ClockContractTests(unittest.TestCase):
    def test_positive_negative_exact_skew_boundaries(self):
        for skew, expected in ((0, SkewLevel.NONE), (4.999, SkewLevel.NONE),
                               (5, SkewLevel.AMBER), (59.999, SkewLevel.AMBER), (60, SkewLevel.RED)):
            for sign in (-1, 1):
                with self.subTest(skew=skew * sign):
                    self.assertEqual(classify_clock_skew(sign * skew), expected)

    def test_midpoint_and_uncertainty_are_display_only(self):
        result = estimate_clock_skew(100, 104, 107)
        self.assertEqual(result.seconds, 5)
        self.assertEqual(result.level, SkewLevel.AMBER)
        self.assertEqual(result.uncertainty_seconds, 2)
        self.assertEqual(estimate_clock_skew(100, 104, 42).level, SkewLevel.RED)

    def test_invalid_clock_evidence(self):
        for values in ((2, 1, 0), (math.nan, 1, 0), (0, 1, math.inf), (0, True, 0)):
            with self.subTest(values=values), self.assertRaises(NodeError):
                estimate_clock_skew(*values)


if __name__ == '__main__':
    unittest.main()
