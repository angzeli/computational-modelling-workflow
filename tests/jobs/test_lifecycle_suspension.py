"""P1 observations preserve the frozen P2 authority and suspension contract."""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from unittest.mock import patch

import psutil

from cmw.jobs import lifecycle, ownership, suspension
from cmw.jobs.store import Store
from tests.jobs.test_lifecycle_observation import LifecycleFixture, NOW, owner, processes


class LifecycleSuspensionTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.computational = owner(82001)
        with self.store.transaction() as con:
            control = self.store.control(con)
            control.update(schema=3, runtime_protocol=3)
            self.store.set_control(con, control)
            row = self.store.get(con, self.job['id'])
            row.update(worker_protocol=3)
            row['env']['CMW_JOBS_OWN_SESSION'] = '1'
            row['suspension'] = suspension.initial_state('local-command-v1')
            row['suspension'].update(verified_at=NOW-10, evidence={'checked_clock': 1.0})
            row['suspension']['capability'] = {
                'protocol': 1, 'backend': 'local-command-v1',
                'runner': owner(82002), 'strategy': 'serial',
                'state_identity': {'device': self.store.path.stat().st_dev,
                                   'inode': self.store.path.stat().st_ino},
                'computational_scope': [self.computational],
                'protected_scope': [self.anchor, owner(82002)]}
            self.store.save(con, row)
        self.job, _ = lifecycle.read_job(self.store, self.job['id'])

    def change_suspension(self, **changes):
        with self.store.transaction() as con:
            row = self.store.get(con, self.job['id'])
            row['suspension'].update(changes)
            self.store.save(con, row)
        self.job, _ = lifecycle.read_job(self.store, self.job['id'])

    def observe_live(self):
        with self.hold(self.lock), self.live():
            return self.observe()

    def test_schema_three_is_read_without_migration_or_control_operations(self):
        before = self.store.path.read_bytes()
        with ExitStack() as guards:
            guards.enter_context(patch.object(Store, 'transaction', side_effect=AssertionError('no write')))
            guards.enter_context(patch.object(suspension, 'submit', side_effect=AssertionError('no request')))
            guards.enter_context(patch.object(suspension, 'readiness', side_effect=AssertionError('no protocol adoption')))
            guards.enter_context(patch.object(ownership, 'snapshot_owned_session', side_effect=AssertionError('no global PID census')))
            result = self.observe_live()
        self.assertEqual(result['evidence']['source']['schema'], 3)
        self.assertEqual(result['persisted_status'], 'Run')
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(result['evidence']['domain']['membership_complete'])
        self.assertFalse(result['reconciliation_required'])

    def test_persisted_suspended_phase_never_becomes_a_fresh_whole_domain_verdict(self):
        self.change_suspension(phase='Suspended', desired='Suspended', inhibited=True,
                               verified_at=NOW-60, evidence={'stopped': [self.computational]})
        before = self.store.path.read_bytes()
        result = self.observe_live()
        self.assertEqual(result['persisted_status'], 'Run')
        self.assertEqual(result['evidence']['suspension']['phase'], 'Suspended')
        self.assertTrue(result['evidence']['suspension']['inhibited'])
        self.assertNotIn(result['observed_execution'], ('VERIFIED_ACTIVE', 'VERIFIED_STOPPED', 'VERIFIED_WORKER'))
        self.assertFalse(result['evidence']['domain']['membership_complete'])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unverified_inhibited_or_transition_phase_cannot_be_overridden_by_live_worker(self):
        for phase in ('Unverified', 'Suspending', 'Resuming'):
            with self.subTest(phase=phase):
                self.change_suspension(phase=phase, inhibited=True, reason='Persisted protocol uncertainty')
                result = self.observe_live()
                self.assertEqual(result['evidence']['worker']['verdict'], 'matched')
                self.assertEqual(result['evidence']['suspension']['phase'], phase)
                self.assertNotIn(result['observed_execution'], ('VERIFIED_WORKER', 'VERIFIED_STOPPED', 'VERIFIED_ACTIVE'))
                self.assertEqual(result['reconciliation_required'], phase == 'Unverified')

    def test_inhibited_run_is_not_presented_as_unqualified_verified_worker(self):
        self.change_suspension(phase='Run', inhibited=True, desired='Terminated')
        result = self.observe_live()
        self.assertTrue(result['evidence']['suspension']['inhibited'])
        self.assertNotIn(result['observed_execution'], ('VERIFIED_WORKER', 'VERIFIED_STOPPED', 'VERIFIED_ACTIVE'))
        self.assertFalse(result['reconciliation_required'])

    def test_suspension_and_worker_protocols_are_distinct_supported_versions(self):
        result = self.observe_live()
        self.assertEqual(result['evidence']['source']['schema'], 3)
        self.assertEqual(result['evidence']['suspension']['protocol'], 1)
        self.assertEqual(self.job['worker_protocol'], 3)
        self.assertEqual(result['observed_execution'], 'VERIFIED_WORKER')

    def test_future_suspension_protocol_does_not_gain_verified_observation(self):
        self.change_suspension(protocol=2)
        result = self.observe_live()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertTrue(result['reconciliation_required'])

    def test_future_worker_protocol_does_not_gain_verified_observation(self):
        self.update(worker_protocol=4)
        result = self.observe_live()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertTrue(result['reconciliation_required'])

    def test_future_capability_protocol_does_not_gain_verified_observation(self):
        capability = {**self.job['suspension']['capability'], 'protocol': 2}
        self.change_suspension(capability=capability)
        result = self.observe_live()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')

    def test_registered_rank_identity_is_not_process_adoption_or_complete_membership(self):
        records = {self.worker['pid']: (self.worker, psutil.STATUS_SLEEPING),
                   self.anchor['pid']: (self.anchor, psutil.STATUS_SLEEPING)}
        with self.hold(self.lock), processes(records) as lookup, \
             patch.object(os, 'getsid', return_value=self.anchor['pid']), \
             patch.object(os, 'getpgid', return_value=self.anchor['pid']):
            result = self.observe()
        inspected = {call.args[0] for call in lookup.call_args_list if call.args}
        self.assertNotIn(self.computational['pid'], inspected)
        self.assertNotIn(82002, inspected)
        self.assertFalse(result['evidence']['domain']['membership_complete'])
        self.assertEqual(result['evidence']['suspension']['phase'], 'Run')

    def test_enrolled_payload_receipt_and_persisted_ack_do_not_grant_terminal_authority(self):
        (self.attempt/'payload-exit.json').write_text(json.dumps({'exit_code': 0, 'recorded_at': NOW-1}))
        channel = self.attempt/'suspension'
        channel.mkdir(mode=0o700)
        for name in ('context.json', 'registration.json', 'ack.json'):
            (channel/name).write_text(json.dumps({'protocol': 1, 'status': 'cancel_prepared',
                                                 'claim': self.job['claim'], 'attempt_id': self.job['attempt_id']}))
        before = {path.name: path.read_bytes() for path in channel.iterdir()}
        self.change_suspension(phase='Unverified', inhibited=True)
        with self.live(worker=False, anchor=False):
            result = self.observe()
        self.assertEqual(result['persisted_status'], 'Run')
        self.assertFalse(result['evidence']['receipt']['terminal_authority'])
        self.assertEqual(result['evidence']['receipt']['verdict'], 'unbound')
        self.assertFalse(result['evidence']['domain']['membership_complete'])
        self.assertEqual({path.name: path.read_bytes() for path in channel.iterdir()}, before)
        self.assertTrue(result['reconciliation_required'])

    def test_key_binds_persisted_suspension_semantics(self):
        original = lifecycle.observation_key(self.store, self.job)
        changes = {'protocol': 2, 'backend': 'future-backend', 'phase': 'Unverified',
                   'desired': 'Suspended', 'revision': 2, 'request': {'id': 'new-request'},
                   'reason': 'new protocol reason', 'inhibited': True,
                   'capability': {**self.job['suspension']['capability'], 'runner': owner(82999)}}
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = deepcopy(self.job)
                changed['suspension'][field] = value
                self.assertNotEqual(lifecycle.observation_key(self.store, changed), original)
        for field, value in (('worker_protocol', 4), ('termination_requested', 'hard deadline')):
            with self.subTest(field=field):
                self.assertNotEqual(lifecycle.observation_key(self.store, {**self.job, field: value}), original)

    def test_key_ignores_timing_and_freshness_projections(self):
        original = lifecycle.observation_key(self.store, self.job)
        projected = deepcopy(self.job)
        projected['suspension'].update(evidence={'observed_at': NOW, 'checked_clock': 99},
            verified_at=NOW, verification_fresh=True, current_suspended_seconds=20,
            suspended_seconds=20, unsuspended_elapsed=30, intervals=[{'start': 10, 'end': 30}],
            clock_started=10, clock_finished=None, accounting_uncertain=False)
        projected.update(elapsed=999, note='concurrent note', note_revision=4, note_updated_at=NOW)
        self.assertEqual(lifecycle.observation_key(self.store, projected), original)

    def test_concurrent_persisted_phase_revision_or_termination_invalidates_observation(self):
        original = lifecycle.observe_process
        for changes in ({'revision': 1}, {'phase': 'Suspended', 'inhibited': True},
                        {'termination_requested': 'hard deadline'}):
            with self.subTest(changes=changes):
                changed = False
                def update(record):
                    nonlocal changed
                    if not changed:
                        changed = True
                        with self.store.transaction() as con:
                            row = self.store.get(con, self.job['id'])
                            if 'termination_requested' in changes:
                                row.update(changes)
                            else:
                                row['suspension'].update(changes)
                            self.store.save(con, row)
                    return original(record)
                with self.hold(self.lock), self.live(), patch.object(lifecycle, 'observe_process', side_effect=update):
                    result = self.observe()
                self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
                self.assertEqual(result['evidence']['source']['verdict'], 'unavailable')
                self.job, _ = lifecycle.read_job(self.store, self.job['id'])

    def test_concurrent_timing_evidence_or_note_update_does_not_invalidate_observation(self):
        original = lifecycle.observe_process
        changed = False
        def update(record):
            nonlocal changed
            if not changed:
                changed = True
                with self.store.transaction() as con:
                    row = self.store.get(con, self.job['id'])
                    row['suspension'].update(verified_at=NOW, evidence={'checked_clock': 100},
                                             suspended_seconds=5, unsuspended_elapsed=6)
                    self.store.save(con, row)
                self.store.annotate(self.job['id'], 'valid concurrent note')
            return original(record)
        with self.hold(self.lock), self.live(), patch.object(lifecycle, 'observe_process', side_effect=update):
            result = self.observe()
        self.assertEqual(result['observed_execution'], 'VERIFIED_WORKER')
        self.assertEqual(result['evidence']['source']['verdict'], 'matched')
        latest, _ = lifecycle.read_job(self.store, self.job['id'])
        self.assertEqual(latest['note'], 'valid concurrent note')
        self.assertEqual(latest['suspension']['suspended_seconds'], 5)

    def test_legacy_unenrolled_attempt_in_schema_three_remains_scoped_read_only(self):
        with self.store.transaction() as con:
            row = self.store.get(con, self.job['id'])
            row.pop('suspension')
            self.store.save(con, row)
        self.job, _ = lifecycle.read_job(self.store, self.job['id'])
        before = self.store.path.read_bytes()
        result = self.observe_live()
        self.assertEqual(result['observed_execution'], 'VERIFIED_WORKER')
        self.assertFalse(result['reconciliation_required'])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_key_detects_cleared_verification_and_changed_recorded_domain(self):
        stable = deepcopy(self.job)
        stable['suspension']['evidence'] = {
            'computational': [self.computational], 'protected': [self.anchor],
            'launchers': [], 'stopped': [], 'checked_clock': 10, 'observed_at': NOW-1}
        key = lifecycle.observation_key(self.store, stable)
        timed = deepcopy(stable)
        timed['suspension']['evidence'].update(checked_clock=20, observed_at=NOW)
        timed['suspension']['verified_at'] = NOW
        self.assertEqual(lifecycle.observation_key(self.store, timed), key)
        changed = deepcopy(stable)
        changed['suspension']['verified_at'] = None
        self.assertNotEqual(lifecycle.observation_key(self.store, changed), key)
        changed = deepcopy(stable)
        changed['suspension']['evidence']['computational'] = [owner(82999)]
        self.assertNotEqual(lifecycle.observation_key(self.store, changed), key)
        changed = deepcopy(stable)
        changed['suspension']['evidence']['stopped'] = [self.computational]
        self.assertNotEqual(lifecycle.observation_key(self.store, changed), key)

    def test_invalidated_verification_during_probe_rejects_stale_positive_result(self):
        observe = lifecycle.observe_process
        changed = False
        def invalidate(record):
            nonlocal changed
            if not changed:
                changed = True
                with self.store.transaction() as con:
                    row = self.store.get(con, self.job['id'])
                    row['suspension']['verified_at'] = None
                    self.store.save(con, row)
            return observe(record)
        with self.hold(self.lock), self.live(), patch.object(lifecycle, 'observe_process', side_effect=invalidate):
            result = self.observe()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(result['evidence']['source']['verdict'], 'unavailable')

    def test_suspended_unknown_keeps_resource_commitments_and_dispatch_unchanged(self):
        self.change_suspension(phase='Suspended', inhibited=True, desired='Suspended')
        self.update(status='Unknown')
        before = self.store.path.read_bytes()
        result = self.observe_live()
        self.assertEqual(result['persisted_status'], 'Unknown')
        self.assertTrue(result['reconciliation_required'])
        self.assertEqual(result['evidence']['suspension']['phase'], 'Suspended')
        self.assertEqual(self.store.path.read_bytes(), before)
        current, control = lifecycle.read_job(self.store, self.job['id'])
        self.assertEqual(current['resources'], self.job['resources'])
        self.assertFalse(control['dispatch'])

    def test_noninteger_and_future_protocol_versions_are_rejected(self):
        for version in (True, 1.0, '1', 2):
            with self.subTest(capability_protocol=version):
                capability = {**self.job['suspension']['capability'], 'protocol': version}
                self.change_suspension(capability=capability)
                self.assertEqual(self.observe_live()['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.change_suspension(capability={**self.job['suspension']['capability'], 'protocol': 1})
        for version in (True, 3.0, '3', 4):
            with self.subTest(worker_protocol=version):
                self.update(worker_protocol=version)
                self.assertEqual(self.observe_live()['observed_execution'], 'OBSERVATION_UNAVAILABLE')

    def test_unenrolled_schema_three_still_rejects_unknown_worker_protocol(self):
        with self.store.transaction() as con:
            row = self.store.get(con, self.job['id'])
            row.pop('suspension')
            row['worker_protocol'] = 4
            self.store.save(con, row)
        self.job, _ = lifecycle.read_job(self.store, self.job['id'])
        self.assertEqual(self.observe_live()['observed_execution'], 'OBSERVATION_UNAVAILABLE')

    def test_schema_three_controller_protocol_is_not_silently_reinterpreted(self):
        for version in (True, 3.0, '3', 4):
            with self.subTest(controller_protocol=version):
                with self.store.transaction() as con:
                    control = self.store.control(con)
                    control['runtime_protocol'] = version
                    self.store.set_control(con, control)
                before = self.store.path.read_bytes()
                with processes({}) as lookup:
                    result = self.observe()
                self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
                self.assertEqual(self.store.path.read_bytes(), before)
                lookup.assert_not_called()

    def test_stopped_protected_control_is_not_ordinary_computational_suspension(self):
        self.change_suspension(phase='Suspended', desired='Suspended', inhibited=True)
        for status in (psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP):
            for stopped in (self.worker, self.anchor):
                with self.subTest(stopped_pid=stopped['pid'], status=status):
                    records = {item['pid']: (item, status if item == stopped else psutil.STATUS_SLEEPING)
                               for item in (self.worker, self.anchor)}
                    with self.hold(self.lock), processes(records), \
                         patch.object(os, 'getsid', return_value=self.anchor['pid']), \
                         patch.object(os, 'getpgid', return_value=self.anchor['pid']):
                        result = self.observe()
                    self.assertEqual(result['persisted_status'], 'Run')
                    self.assertEqual(result['evidence']['suspension']['phase'], 'Suspended')
                    self.assertTrue(result['reconciliation_required'])
                    self.assertNotIn(result['observed_execution'], ('VERIFIED_WORKER', 'VERIFIED_STOPPED', 'CONTROLLED_PHASE'))
