"""Presentation-only guard fixtures; no process or scientific observation."""
from copy import deepcopy
import time
import unittest
from unittest.mock import patch

from cmw.jobs.activity import admission
from cmw.jobs.cli import guard_text
from cmw.jobs.status_presentation import admission_presentation, scheduling_detail


def state_fixture(*, running=True, queued=False):
    jobs = []
    if running:
        jobs.append({'id': 1, 'display_id': 'J1.1', 'status': 'Run', 'order': None,
                     'scheduling': {'role': 'primary'}})
    if queued:
        jobs.append({'id': 2, 'display_id': 'J2.1', 'status': 'Queue', 'order': 1})
    state = {'mode': 'Sequential', 'scheduler': {'mode': 'sequential'}, 'jobs': jobs,
             'controller': {'online': True, 'stale': False, 'dispatch': True, 'stop': False},
             'external_activity': {
                 'state': 'NO_MATCH', 'reason': 'No recognized external computation observed',
                 'source': 'client', 'scope': 'Synthetic recognized-executable scope',
                 'observed_at': time.time(), 'age_seconds': 0, 'stale': False,
                 'observations': [], 'coverage': {'complete': True, 'uncertainties': 0,
                                                'warnings': [], 'limitations': []}}}
    state['admission'] = admission(state, state['external_activity'])
    return state


class AdmissionPresentationTests(unittest.TestCase):
    def test_sequential_occupancy_is_normal_and_preserves_canonical_admission(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                state = state_fixture(queued=queued)
                before = deepcopy(state)
                view = admission_presentation(state)
                self.assertEqual((view.category, view.tone), ('NORMAL_CAPACITY', 'neutral'))
                self.assertIn('Slot occupied by J1.1', view.summary)
                self.assertEqual('1 queued' in view.summary, queued)
                self.assertLessEqual(len(view.summary), 75)
                self.assertFalse(state['admission']['permitted'])
                self.assertIn('Admission: BLOCKED — Managed execution occupies slot', guard_text(state))
                self.assertEqual(state, before)

    def test_classification_does_not_interpret_reason_prose(self):
        state = state_fixture()
        state['admission']['reason'] = 'Opaque unchanged diagnostic'
        self.assertEqual(admission_presentation(state).category, 'NORMAL_CAPACITY')
        self.assertIn('Opaque unchanged diagnostic', scheduling_detail(state))
        state['jobs'][0]['status'] = 'Unknown'
        state['admission']['reason'] = 'Managed execution occupies slot'
        self.assertEqual(admission_presentation(state).category, 'WARNING')

    def test_no_pending_is_idle_only_with_usable_observation_and_online_controller(self):
        state = state_fixture(running=False)
        self.assertEqual(admission_presentation(state).category, 'IDLE')
        self.assertIn('No pending jobs', admission_presentation(state).summary)
        state['controller']['online'] = False
        self.assertEqual(admission_presentation(state).category, 'WARNING')

    def test_paused_and_held_work_are_waiting_not_admission_permission(self):
        state = state_fixture(running=False)
        state['controller']['dispatch'] = False
        self.assertEqual(admission_presentation(state).category, 'WAITING')
        state['controller']['dispatch'] = True
        state['jobs'] = [{'id': 1, 'status': 'Hold', 'order': 1}]
        self.assertIn('Queue head on Hold', admission_presentation(state).summary)
        self.assertFalse(state['admission']['permitted'])

    def test_offline_and_stale_controller_remain_visible(self):
        for updates, category, text in (
                ({'online': False}, 'WARNING', 'Controller offline'),
                ({'online': False, 'dispatch': False}, 'CAUTION', 'Controller offline'),
                ({'stale': True}, 'WARNING', 'Controller stale')):
            with self.subTest(updates=updates):
                state = state_fixture()
                state['controller'].update(updates)
                view = admission_presentation(state)
                self.assertEqual(view.category, category)
                self.assertIn(text, view.summary)

    def test_missing_freshness_evidence_is_not_normal_capacity(self):
        for field in ('controller', 'external_activity'):
            with self.subTest(field=field):
                state = state_fixture()
                state[field].pop('stale')
                self.assertIn(admission_presentation(state).category, {'CAUTION', 'WARNING'})

    def test_unknown_cancelling_and_non_primary_are_not_normal_capacity(self):
        for updates in ({'status': 'Unknown'}, {'status': 'Cancelling'}, {'status': 'Starting'},
                        {'scheduling': {'role': 'auxiliary'}}):
            with self.subTest(updates=updates):
                state = state_fixture()
                state['jobs'][0].update(updates)
                self.assertIn(admission_presentation(state).category, {'CAUTION', 'WARNING'})
        state = state_fixture()
        state['jobs'].append(deepcopy(state['jobs'][0]))
        self.assertEqual(admission_presentation(state).category, 'CAUTION')

    def test_clear_complete_scope_is_observation_not_machine_idle_claim(self):
        view = admission_presentation(state_fixture())
        self.assertEqual(view.external_tone, 'neutral')
        self.assertIn('0 observed', view.external_summary)
        self.assertIn('B details', view.external_summary)
        self.assertNotIn('No external processes', view.external_summary)
        self.assertNotIn('machine idle', view.external_summary)

    def test_known_scope_limitations_keep_capacity_normal_but_external_caution(self):
        state = state_fixture()
        coverage = state['external_activity']['coverage']
        coverage.update(warnings=['Unavailable executable for an unrecognized name'],
                        limitations=['Unavailable executable for an unrecognized name'])
        view = admission_presentation(state)
        self.assertEqual((view.category, view.external_tone), ('NORMAL_CAPACITY', 'caution'))
        self.assertIn('1 coverage warnings', view.external_summary)
        self.assertIn(coverage['warnings'][0], scheduling_detail(state))

    def test_unclassified_or_material_coverage_remains_cautious(self):
        for updates in ({'warnings': ['Unclassified warning']}, {'uncertainties': 1},
                        {'complete': False}):
            with self.subTest(updates=updates):
                state = state_fixture()
                state['external_activity']['coverage'].update(updates)
                view = admission_presentation(state)
                self.assertEqual(view.category, 'CAUTION')
                self.assertIn('UNCERTAIN', view.external_summary)

    def test_busy_uncertain_unavailable_and_missing_scan_remain_visible(self):
        for guard_state, category in (('BUSY', 'CAUTION'), ('UNCERTAIN', 'CAUTION'),
                                      ('UNAVAILABLE', 'WARNING')):
            with self.subTest(guard_state=guard_state):
                state = state_fixture(running=False)
                state['external_activity']['state'] = guard_state
                state['admission']['reason'] = 'Synthetic guard diagnostic'
                if guard_state == 'BUSY':
                    state['external_activity']['observations'] = [{'id': 'Efixture'}]
                view = admission_presentation(state)
                self.assertEqual(view.category, category)
                if guard_state == 'UNAVAILABLE':
                    self.assertIn('UNAVAILABLE', view.external_summary)
                    self.assertIn('0 last observed', view.external_summary)
        state = state_fixture()
        state.pop('external_activity')
        self.assertEqual(admission_presentation(state).category, 'WARNING')

    def test_stale_and_unusable_scan_ages_never_become_healthy_zero(self):
        for updates in ({'stale': True}, {'age_seconds': 3}, {'age_seconds': None},
                        {'age_seconds': float('nan')}, {'age_seconds': -1},
                        {'observed_at': None}):
            with self.subTest(updates=updates):
                state = state_fixture()
                state['external_activity'].update(updates)
                view = admission_presentation(state)
                self.assertEqual((view.category, view.external_tone), ('WARNING', 'warning'))
                self.assertIn('last observed', view.external_summary)

    def test_managed_external_conflict_has_priority_over_normal_occupancy(self):
        for projected_conflict in (False, True):
            state = state_fixture()
            state['admission']['conflict'] = projected_conflict
            state['external_activity']['observations'] = [{'id': 'Efixture'}]
            view = admission_presentation(state)
            self.assertEqual(view.category, 'WARNING')
            self.assertIn('conflict', view.summary)

    def test_bounded_refusals_are_not_reclassified_from_reason_words(self):
        for reason in ('CPU commitment exceeds budget', 'External reservation identity expired',
                       'Managed execution occupies slot'):
            state = state_fixture()
            state['mode'] = 'Bounded Sharing'
            state['scheduler']['mode'] = 'bounded-sharing'
            state['admission']['reason'] = reason
            view = admission_presentation(state)
            self.assertEqual(view.category, 'CAUTION')
            self.assertIn(reason, view.summary)

    def test_sharing_warning_and_controller_stop_are_not_ordinary_occupancy(self):
        state = state_fixture()
        state['admission']['warnings'] = ['Resource overrun fixture']
        self.assertEqual(admission_presentation(state).category, 'CAUTION')
        state['admission'].pop('warnings')
        state['controller']['stop'] = True
        self.assertEqual(admission_presentation(state).category, 'CAUTION')

    def test_permission_still_awaits_controller_and_external_reservation_stays_cautious(self):
        state = state_fixture(running=False)
        state['admission']['permitted'] = True
        self.assertIn('Awaiting controller check', admission_presentation(state).summary)
        state['scheduler']['mode'] = 'bounded-sharing'
        state['external_activity'].update(state='BUSY', observations=[{'id': 'Efixture'}])
        state['admission']['reason'] = 'Reserved coexistence; controller rechecks'
        view = admission_presentation(state)
        self.assertEqual(view.category, 'CAUTION')
        self.assertNotIn('BLOCKED', view.summary)

    def test_full_scheduling_details_keep_all_diagnostics_and_projection(self):
        state = state_fixture()
        state['controller_guard'] = {'state': 'UNCERTAIN', 'observed_at': 123,
                                     'reason': 'Older independent controller observation'}
        state['external_activity']['coverage']['warnings'] = ['Warning one', 'Warning two']
        state['admission']['conflict'] = True
        before = deepcopy(state)
        with patch('cmw.jobs.activity.DEFAULT_OBSERVER.scan', side_effect=AssertionError('no scan')):
            admission_presentation(state)
            detail = scheduling_detail(state)
        for expected in ('Warning one', 'Warning two', 'CONFLICT', 'Controller last evaluation',
                         'Older independent controller observation', 'Synthetic recognized-executable scope',
                         '"controller"', '"external_activity"', '"controller_guard"', '"permitted": false'):
            self.assertIn(expected, detail)
        self.assertEqual(state, before)


if __name__ == '__main__':
    unittest.main()
