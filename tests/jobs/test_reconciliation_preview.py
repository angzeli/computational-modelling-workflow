"""Recovery previews bind lifecycle evidence without acquiring mutation authority."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs import reconciliation
from cmw.jobs.store import JobsError, Store
from tests.jobs.test_lifecycle_cli import observed


class ReconciliationPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-reconcile-preview-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root/'state')
        job = self.store.add(argv=['/unexecuted/fixture'], cwd=self.root, name='fixture', cpus=8, memory_gib=12)
        with self.store.transaction() as con:
            row = self.store.get(con, job['id'])
            row.update(status='Run', order=None, started_at=1.0, claim='fixture-claim')
            self.store.save(con, row)
        self.state = self.store.snapshot()
        self.job = self.state['jobs'][0]

    def observation(self, **verdicts):
        value = observed(self.job).to_dict()
        for field, verdict in verdicts.items():
            value['evidence'][field]['verdict'] = verdict
        return observed(self.job, **value)

    def preview(self, **verdicts):
        with patch.object(reconciliation, 'observe_execution', return_value=self.observation(**verdicts)):
            return reconciliation.preview_reconciliation(self.store, self.job, state=self.state).to_dict()

    def test_absent_worker_proposes_only_conditional_unknown_and_retains_commitments(self):
        before = self.store.path.read_bytes()
        with patch.object(Store, 'transaction', side_effect=AssertionError('preview cannot write')), \
             patch.object(Store, 'snapshot', side_effect=AssertionError('preview cannot project all jobs')), \
             patch('psutil.pids', side_effect=AssertionError('preview cannot enumerate processes')), \
             patch('psutil.process_iter', side_effect=AssertionError('preview cannot discover processes')), \
             patch.object(os, 'kill', side_effect=AssertionError('preview cannot signal')), \
             patch.object(os, 'killpg', side_effect=AssertionError('preview cannot signal')):
            result = self.preview()
        self.assertEqual(result['proposed_action'], 'conditional_mark_unknown')
        self.assertEqual(result['proposed_outcome'], 'Unknown')
        self.assertFalse(result['mutation_permitted'])
        self.assertFalse(result['apply_supported'])
        self.assertEqual(result['conditional_field_changes']['controller.dispatch'], False)
        self.assertEqual(result['resource_commitments'], 'retained')
        self.assertFalse(result['dispatch_enablement_permitted'])
        self.assertEqual(self.store.path.read_bytes(), before)
        after = self.store.snapshot()
        self.assertEqual(after['jobs'][0]['status'], 'Run')
        self.assertEqual(after['jobs'][0]['resources'], self.job['resources'])
        self.assertEqual(after['jobs'][0]['attempt_id'], self.job['attempt_id'])

    def test_existing_authority_is_not_overridden_even_with_incomplete_domain(self):
        for changes in ({'worker': 'matched'}, {'worker_lock': 'held'},
                        {'controller': 'online'}, {'controller': 'stale'}, {'controller': 'unavailable'}):
            with self.subTest(changes=changes):
                result = self.preview(**changes)
                self.assertEqual(result['proposed_action'], 'none')
                self.assertIsNone(result['proposed_outcome'])
                self.assertEqual(result['conditional_field_changes'], {})

    def test_unbound_malformed_and_contradictory_receipts_never_grant_terminal_authority(self):
        for verdict in ('missing', 'malformed', 'unbound', 'contradictory', 'present', 'bound'):
            with self.subTest(verdict=verdict):
                observation = self.observation(receipt=verdict).to_dict()
                observation['evidence']['receipt']['exit_code'] = 0
                with patch.object(reconciliation, 'observe_execution', return_value=observed(self.job, **observation)):
                    result = reconciliation.preview_reconciliation(self.store, self.job, state=self.state).to_dict()
                self.assertEqual(result['proposed_outcome'], 'Unknown')
                self.assertFalse(result['mutation_permitted'])
                self.assertNotIn('job.finished_at', result['conditional_field_changes'])
                self.assertNotIn('job.exit_code', result['conditional_field_changes'])

    def test_unavailable_source_vetoes_a_transition_even_if_partial_probes_look_absent(self):
        result = self.preview(source='unavailable')
        self.assertEqual(result['proposed_action'], 'none')
        self.assertEqual(result['conditional_field_changes'], {})

    def test_missing_or_unreadable_identity_or_lock_does_not_propose_transition(self):
        for changes in ({'worker': 'missing'}, {'worker': 'mismatch'}, {'worker': 'unavailable'},
                        {'worker_lock': 'missing'}, {'worker_lock': 'unavailable'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.preview(**changes)['proposed_action'], 'none')

    def test_changed_attempt_or_worker_completion_invalidates_preview(self):
        for changes in ({'attempt_id': 'f'*32}, {'status': 'Done', 'finished_at': 20.0},
                        {'claim': 'replacement-claim'}):
            with self.subTest(changes=changes):
                fresh = deepcopy(self.state)
                fresh['jobs'][0].update(changes)
                with patch.object(reconciliation, 'read_job', return_value=(fresh['jobs'][0], fresh['controller'])), \
                     patch.object(reconciliation, 'observe_execution', return_value=self.observation()):
                    result = reconciliation.preview_reconciliation(self.store, self.job, state=self.state).to_dict()
                self.assertTrue(result['stale_preview'])
                self.assertEqual(result['proposed_action'], 'none')
                self.assertIn('STALE_PREVIEW', result['rationale'][0])

    def test_new_controller_authority_invalidates_conditional_proposal(self):
        fresh = deepcopy(self.state)
        fresh['controller'].update(online=True, owner={'pid': 123, 'birth': 20, 'host': 'fixture', 'boot': 1})
        with patch.object(reconciliation, 'read_job', return_value=(fresh['jobs'][0], fresh['controller'])), \
             patch.object(reconciliation, 'observe_execution', return_value=self.observation()):
            result = reconciliation.preview_reconciliation(self.store, self.job, state=self.state).to_dict()
        self.assertTrue(result['stale_preview'])
        self.assertEqual(result['conditional_field_changes'], {})

    def test_note_concurrency_does_not_invalidate_lifecycle_binding(self):
        fresh = deepcopy(self.state)
        fresh['jobs'][0].update(note='legitimate concurrent annotation', note_revision=1, note_updated_at=21.0)
        with patch.object(reconciliation, 'read_job', return_value=(fresh['jobs'][0], fresh['controller'])), \
             patch.object(reconciliation, 'observe_execution', return_value=self.observation()):
            result = reconciliation.preview_reconciliation(self.store, self.job, state=self.state).to_dict()
        self.assertFalse(result['stale_preview'])
        self.assertEqual(result['proposed_outcome'], 'Unknown')
        self.assertEqual(fresh['jobs'][0]['note'], 'legitimate concurrent annotation')

    def test_unknown_remains_unresolved_and_terminal_jobs_are_not_reopened(self):
        for status in ('Unknown', 'Done', 'Fail', 'Cancelled', 'Hold'):
            with self.subTest(status=status):
                state = deepcopy(self.state)
                state['jobs'][0]['status'] = status
                with patch.object(reconciliation, 'read_job', return_value=(state['jobs'][0], state['controller'])), \
                     patch.object(reconciliation, 'observe_execution', return_value=observed(state['jobs'][0])):
                    result = reconciliation.preview_reconciliation(self.store, state['jobs'][0], state=state).to_dict()
                self.assertEqual(result['proposed_action'], 'none')
                self.assertEqual(result['conditional_field_changes'], {})

    def test_unsupported_schema_rejected_before_observation(self):
        state = {**self.state, 'schema': 3}
        with patch.object(reconciliation, 'observe_execution', side_effect=AssertionError('must not observe')):
            with self.assertRaisesRegex(JobsError, 'Unsupported Jobs state schema'):
                reconciliation.preview_reconciliation(self.store, self.job, state=state)

    def test_cli_preview_default_and_explicit_are_non_mutating(self):
        before = self.store.path.read_bytes()
        for extra in ([], ['--preview']):
            out = io.StringIO()
            with redirect_stdout(out), \
                 patch.object(reconciliation, 'observe_execution', return_value=self.observation()):
                code = main(['jobs', '--state', str(self.store.root), 'reconcile', self.job['display_id'], *extra, '--json'])
            self.assertEqual(code, 0)
            result = json.loads(out.getvalue())
            self.assertFalse(result['mutation_permitted'])
            self.assertFalse(result['apply_supported'])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_cli_apply_is_explicitly_rejected_before_any_state_or_process_probe(self):
        out = io.StringIO()
        with redirect_stdout(out), \
             patch.object(Store, 'snapshot', side_effect=AssertionError('apply must not read state')), \
             patch.object(Store, 'transaction', side_effect=AssertionError('apply must not write')), \
             patch.object(reconciliation, 'observe_execution', side_effect=AssertionError('apply must not observe')):
            code = main(['jobs', '--state', str(self.root/'missing'), 'reconcile', self.job['display_id'], '--apply', '--json'])
        self.assertEqual(code, 2)
        self.assertIn('deferred until serial P2 integration', json.loads(out.getvalue())['reason'])
        self.assertFalse((self.root/'missing').exists())


if __name__ == '__main__':
    unittest.main()
