"""Offline decision tables; no subprocesses, filesystem state or node clocks."""
import math
import unittest

from cmw.jobs.multihost.logic import (
    CancelAction, NodeEvent, NodeState, Reachability, SkewLevel,
    available_cancel_modes, decide_cancel, estimate_clock_skew, negotiate, transition,
)
from cmw.jobs.multihost.models import CancelMode, ContractError


def capabilities():
    return {'submit': True, 'receive': {'max_bundle_bytes': 67108864},
            'cancel_modes': {'vasp': ['graceful', 'soft', 'hard'], 'orca': ['hard']},
            'engines': {'vasp': {'builds': [
                {'build_id': 'synthetic-production', 'version': '6.6.1',
                 'executables': ['vasp_std', 'vasp_gam'], 'experimental': False, 'default': True}]}}}


class CompatibilityTests(unittest.TestCase):
    def test_same_and_different_minor_intersect_features(self):
        local, remote = capabilities(), capabilities()
        remote['submit'] = False
        remote['receive']['max_bundle_bytes'] = 100
        remote['cancel_modes']['vasp'] = ['hard']
        remote['engines']['vasp']['builds'][0]['build_id'] = 'different-host-build'
        remote['future_feature'] = True
        for version in ('1.0', '1.12'):
            with self.subTest(version=version):
                result = negotiate('1.0', version, local, remote)
                self.assertTrue(result.compatible)
                self.assertEqual(result.features, {'receive', 'cancel:vasp:hard', 'cancel:orca:hard', 'engine:vasp'})
                self.assertEqual(result.max_bundle_bytes, 100)

    def test_major_mismatch_disables_every_feature(self):
        result = negotiate('1.0', '2.0', capabilities(), capabilities())
        self.assertFalse(result.compatible)
        self.assertFalse(result.features)
        self.assertIsNone(result.max_bundle_bytes)

    def test_generic_comparison_uses_declared_local_major_support(self):
        result = negotiate('2.0', '2.1', capabilities(), capabilities())
        self.assertTrue(result.compatible)
        self.assertIn('submit', result.features)

    def test_absent_capabilities_offer_nothing(self):
        result = negotiate('1.0', '1.1', capabilities(), {})
        self.assertTrue(result.compatible)
        self.assertEqual(result.features, frozenset())
        self.assertIsNone(result.max_bundle_bytes)

    def test_negotiation_cannot_enable_unsupported_engine_modes(self):
        caps = capabilities()
        caps['cancel_modes']['orca'] = ['graceful', 'soft', 'hard']
        caps['cancel_modes']['future_engine'] = ['hard']
        result = negotiate('1.0', '1.0', caps, caps)
        self.assertIn('cancel:orca:hard', result.features)
        self.assertNotIn('cancel:orca:graceful', result.features)
        self.assertNotIn('cancel:orca:soft', result.features)
        self.assertNotIn('cancel:future_engine:hard', result.features)

    def test_malformed_versions_are_typed_errors(self):
        for version in ('1', '1.0.0', '01.0', '-1.0', '1.x', '', None, True):
            with self.subTest(version=version), self.assertRaises(ContractError):
                negotiate('1.0', version, {}, {})


class ReachabilityTests(unittest.TestCase):
    def test_every_state_event_pair(self):
        # Each row is an explicit expected transition with zero prior failures.
        events = tuple(NodeEvent)
        expected = {
            None: (None, NodeState.REACHABLE, None, None, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
            NodeState.REACHABLE: (NodeState.REACHABLE, NodeState.REACHABLE, NodeState.REACHABLE,
                                  NodeState.REACHABLE, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
            NodeState.UNREACHABLE: (NodeState.UNREACHABLE, NodeState.REACHABLE, NodeState.UNREACHABLE,
                                    NodeState.UNREACHABLE, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
            NodeState.INCOMPATIBLE: (NodeState.INCOMPATIBLE, NodeState.REACHABLE, NodeState.INCOMPATIBLE,
                                    NodeState.INCOMPATIBLE, NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE),
            NodeState.AUTH_FAILED: (NodeState.AUTH_FAILED,) * 6,
        }
        for state, results in expected.items():
            for event, result in zip(events, results):
                with self.subTest(state=state, event=event):
                    updated = transition(Reachability(state), event, received_at=10)
                    self.assertEqual(updated.state, result)
                    self.assertEqual(updated.operations_enabled, result == NodeState.REACHABLE)
                    self.assertEqual(updated.polling_enabled, result != NodeState.AUTH_FAILED)

    def test_three_consecutive_mixed_transport_failures_and_recovery(self):
        state = transition(Reachability(), 'handshake_success', received_at=5)
        for count, event in enumerate(('connect_error', 'timeout', 'connect_error'), 1):
            state = transition(state, event)
            self.assertEqual(state.consecutive_failures, count)
            self.assertEqual(state.state, NodeState.REACHABLE if count < 3 else NodeState.UNREACHABLE)
            self.assertEqual(state.last_received_at, 5)
        self.assertEqual(state.age(35), 30)
        ordinary = transition(state, 'success', received_at=40)
        self.assertEqual(ordinary.state, NodeState.UNREACHABLE)
        self.assertEqual(ordinary.consecutive_failures, 0)
        recovered = transition(ordinary, 'handshake_success', received_at=41)
        self.assertEqual(recovered.state, NodeState.REACHABLE)

    def test_success_resets_counter_before_third_failure(self):
        initial = Reachability(NodeState.REACHABLE, 2, 1)
        current = transition(initial, 'success', received_at=2)
        self.assertEqual(current.consecutive_failures, 0)
        self.assertEqual(transition(current, 'timeout').state, NodeState.REACHABLE)

    def test_failure_threshold_for_every_state(self):
        for state in (None, *NodeState):
            for event in ('connect_error', 'timeout'):
                for count in (0, 1, 2, 3):
                    with self.subTest(state=state, event=event, count=count):
                        result = transition(Reachability(state, count), event)
                        expected = state
                        if state not in (NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE) and count >= 2:
                            expected = NodeState.UNREACHABLE
                        self.assertEqual(result.state, expected)

    def test_auth_recovery_requires_manual_handshake(self):
        auth = transition(Reachability(NodeState.REACHABLE), 'auth_failure')
        self.assertEqual(transition(auth, 'handshake_success', received_at=1), auth)
        self.assertEqual(transition(auth, 'success', received_at=1, manual_retry=True).state, NodeState.AUTH_FAILED)
        self.assertEqual(transition(auth, 'timeout', manual_retry=True).state, NodeState.AUTH_FAILED)
        self.assertEqual(transition(auth, 'handshake_success', received_at=2, manual_retry=True).state, NodeState.REACHABLE)

    def test_no_initial_reachability_without_handshake(self):
        self.assertIsNone(transition(Reachability(), 'success', received_at=1).state)
        self.assertIsNone(Reachability().age(100))

    def test_portal_times_only_and_bad_events(self):
        state = Reachability(NodeState.REACHABLE, 0, 100)
        with self.assertRaises(ContractError):
            state.age(99)
        for event, received in (('success', 99), ('success', math.nan), ('success', None), ('bogus', 101)):
            with self.subTest(event=event, received=received), self.assertRaises(ContractError):
                transition(state, event, received_at=received)


class ClockTests(unittest.TestCase):
    def test_exact_positive_negative_boundaries(self):
        for skew, level in ((0, SkewLevel.NONE), (4.999, SkewLevel.NONE), (5, SkewLevel.AMBER),
                            (59.999, SkewLevel.AMBER), (60, SkewLevel.RED), (600, SkewLevel.RED)):
            for sign in (-1, 1):
                with self.subTest(skew=skew * sign):
                    result = estimate_clock_skew(100, 104, 102 + sign * skew)
                    self.assertAlmostEqual(result.seconds, skew * sign)
                    self.assertEqual(result.level, level)
                    self.assertEqual(result.round_trip_seconds, 4)
                    self.assertEqual(result.uncertainty_seconds, 2)

    def test_hello_utc_timestamp(self):
        self.assertEqual(estimate_clock_skew(0, 2, '1970-01-01T00:00:06.000Z').level, SkewLevel.AMBER)

    def test_invalid_clock_evidence(self):
        for args in ((2, 1, 1), (math.inf, 2, 1), (0, 1, math.nan),
                     (0, 1, '1970-01-01T00:00:00'), (0, 1, 'bad')):
            with self.subTest(args=args), self.assertRaises(ContractError):
                estimate_clock_skew(*args)


class CancellationTests(unittest.TestCase):
    def test_full_escalation_table(self):
        modes = tuple(CancelMode)
        expected = {
            None: (CancelAction.START, CancelAction.START, CancelAction.START),
            CancelMode.GRACEFUL: (CancelAction.NOOP, CancelAction.ESCALATE, CancelAction.ESCALATE),
            CancelMode.SOFT: (CancelAction.REJECT, CancelAction.NOOP, CancelAction.ESCALATE),
            CancelMode.HARD: (CancelAction.REJECT, CancelAction.REJECT, CancelAction.NOOP),
        }
        for current, actions in expected.items():
            for requested, action in zip(modes, actions):
                with self.subTest(current=current, requested=requested):
                    result = decide_cancel(current, requested)
                    self.assertEqual(result.action, action)
                    self.assertEqual(result.effective_mode, current if action == CancelAction.REJECT else requested)
                    self.assertEqual(result.error_code, 'conflict' if action == CancelAction.REJECT else None)

    def test_engine_modes_and_unsupported_requests(self):
        caps = capabilities()
        self.assertEqual(available_cancel_modes('vasp', caps), tuple(CancelMode))
        self.assertEqual(available_cancel_modes('orca', caps), (CancelMode.HARD,))
        for engine in ('orca', 'unknown'):
            result = decide_cancel(None, 'graceful', engine=engine, capabilities=caps)
            self.assertEqual(result.error_code, 'capability_unsupported')
        self.assertEqual(decide_cancel(None, 'hard', engine='orca', capabilities=caps).action, CancelAction.START)

    def test_no_synonyms_or_implicit_modes(self):
        for mode in ('force', 'now', 'softly', None):
            with self.subTest(mode=mode), self.assertRaises(ContractError):
                decide_cancel(None, mode)
        with self.assertRaises(ContractError):
            decide_cancel(None, 'hard', engine='orca')


if __name__ == '__main__':
    unittest.main()
