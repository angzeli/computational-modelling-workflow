"""Cheap tests of the actual monitor/driver branches before qualification."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs.store import Store
from tests.campaign import closure_workflows
from tests.campaign.active_writer_pty import admission_diagnostic
from tests.campaign.qualification_v2 import FIELDS, coverage, field_sample, outcomes
from tests.campaign.support import ProcessLedger


class DriverContractTests(unittest.TestCase):
    def process(self):
        process = Mock(pid=os.getpid())
        process.create_time.return_value = 123.
        process.status.return_value = psutil.STATUS_RUNNING
        process.memory_info.return_value = Mock(rss=1234)
        process.num_fds.return_value = 9
        process.cpu_times.return_value = Mock(user=1., system=.5)
        return process

    def test_denied_fd_preserves_rss_cpu_and_exact_method(self):
        process = self.process()
        process.num_fds.side_effect = psutil.AccessDenied(process.pid)
        values, fields = field_sample(process, 123.)
        self.assertEqual(values['rss_bytes'], 1234)
        self.assertEqual(values['cpu_seconds'], 1.5)
        self.assertNotIn('fds', values)
        self.assertIsNone(fields['fds']['value'])
        self.assertEqual(fields['fds']['method'], 'psutil.Process.num_fds')
        self.assertEqual(fields['fds']['classification'], 'access_denied')
        self.assertEqual(fields['fds']['exception'], 'AccessDenied')
        self.assertGreaterEqual(fields['fds']['ended_at'], fields['fds']['started_at'])

    def test_no_such_process_one_method_same_live_birth_is_field_uncertainty(self):
        process = self.process()
        process.memory_info.side_effect = psutil.NoSuchProcess(process.pid)
        values, fields = field_sample(process, 123.)
        self.assertEqual(fields['rss_bytes']['classification'], 'transient_read_uncertainty')
        self.assertEqual(fields['rss_bytes']['post_identity']['birth'], 123.)
        self.assertEqual(fields['rss_bytes']['post_identity']['status'], psutil.STATUS_RUNNING)
        self.assertEqual(values['fds'], 9)
        self.assertNotIn('rss_bytes', values)

    def test_wrong_birth_never_publishes_a_resource_value(self):
        process = self.process()
        values, fields = field_sample(process, 124.)
        self.assertEqual(set(values), {'pid', 'birth'})
        self.assertTrue(all(value['classification'] == 'identity_mismatch' for value in fields.values()))
        process.memory_info.assert_not_called()

    def test_required_owner_blindness_is_not_hidden_by_other_owners(self):
        records = [{'ledger_id': i, 'recorded_at': 0., 'role': role, 'capture_origin': 'direct-launch'}
                   for i, role in enumerate(('controller', 'endpoint'))]
        observations = []
        for index in range(20):
            for record in records:
                observations.append({'ledger_id': record['ledger_id'], 'classification': 'partially_sampled_live',
                    'required_live': True, 'fields': {field: {'ended_at': (index+1)*.1,
                        'classification': 'access_denied' if record['ledger_id'] == 1 and field == 'fds' else 'sampled_live'}
                        for field in FIELDS}})
        result = coverage(records, observations, [], ended_at=2.)
        self.assertEqual(result['resource_observability'], 'RESOURCE_QUALIFICATION_INCOMPLETE')
        bad, = [row for row in result['field_coverage'] if row['failures']]
        self.assertEqual((bad['role'], bad['field']), ('endpoint', 'fds'))
        self.assertEqual(bad['longest_blind_seconds'], 2.)
        result = outcomes(functional_passed=True, safety_passed=True, cleanup_passed=True, observation=result)
        self.assertEqual(result['functional_result'], 'PASS')
        self.assertTrue(result['execution_passed'])
        self.assertFalse(result['qualified'])

    def test_observed_descendant_exit_caps_lifetime_without_inventing_death_time(self):
        record = {'ledger_id': 2, 'recorded_at': 0., 'role': 'owned descendant',
                  'capture_origin': 'first-observed-descendant'}
        observations = [{'ledger_id': 2, 'required_live': True, 'classification': 'sampled_live',
            'fields': {field: {'ended_at': value, 'classification': 'sampled_live'} for field in FIELDS}}
            for value in (.01, .13, .25, .37, .49, .62)]
        observations.append({'ledger_id': 2, 'required_live': False, 'classification': 'verified_exited',
                             'at': .74, 'exit_evidence': 'recorded identity absent on follow-up observation'})
        result = coverage([record], observations, [], ended_at=61.84)
        self.assertEqual(result['resource_observability'], 'PASS')
        self.assertTrue(all(row['first_observed_limit'] for row in result['field_coverage']))
        for row in result['field_coverage']:
            self.assertEqual(row['lifetime_seconds'], .74)
            self.assertEqual(row['lifecycle_end_upper_bound'], .74)
            self.assertEqual(row['end_basis'], 'observed verified exit')
            self.assertLess(row['longest_blind_seconds'], .2)
        self.assertNotIn('exit', record)

    def test_isolated_field_gap_is_disclosed_within_predeclared_window(self):
        record = {'ledger_id': 0, 'recorded_at': 0., 'role': 'controller', 'capture_origin': 'direct-launch'}
        observations = [{'ledger_id': 0, 'classification': 'sampled_live', 'required_live': True,
            'fields': {field: {'ended_at': (index+1)*.1,
                'classification': 'access_denied' if index == 5 and field == 'fds' else 'sampled_live'}
                for field in FIELDS}} for index in range(20)]
        result = coverage([record], observations, [], ended_at=2.)
        self.assertEqual(result['resource_observability'], 'PASS')
        fds, = [row for row in result['field_coverage'] if row['field'] == 'fds']
        self.assertEqual(fds['classifications']['access_denied'], 1)
        self.assertAlmostEqual(fds['longest_blind_seconds'], .2)

    def test_sampler_lag_has_classified_scheduled_opportunities(self):
        ledger = ProcessLedger(self.id())
        ledger.next_due -= .35
        ledger.sample()
        counts = ledger.report()['opportunity_counts']
        self.assertGreaterEqual(counts['skipped_lag'], 3)
        self.assertEqual(counts['sampled'], 1)
        skipped = [row for row in ledger.observations if row['classification'] == 'skipped_lag']
        self.assertTrue(all(set(row['fields']) == set(FIELDS) for row in skipped))

    def test_held_unknown_branch_executes_real_store_add_and_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'state')
            result = closure_workflows.held_unknown_review(store, root)
            job, = store.snapshot()['jobs']
            self.assertEqual(job['name'], 'held UNKNOWN review control')
            self.assertEqual(job['status'], 'Hold')
            self.assertFalse((store.root/'attempts'/job['attempt_id']/'payload-exit.json').exists())
            self.assertEqual(next(row.status for row in result.domains if row.name == 'operational'), 'UNKNOWN')

    def test_active_writer_uses_supported_head_order_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'state')
            held = store.add(argv=['/unexecuted'], cwd=root, name='Synthetic', hold=True)
            writer = store.add(argv=['/unexecuted'], cwd=root, name='benign writer', hold=True)
            before = admission_diagnostic(store, writer['id'])
            self.assertEqual(before['queue'][0]['id'], held['id'])
            store.change(writer['id'], 'move', position=1)
            store.change(writer['id'], 'release')
            after = admission_diagnostic(store, writer['id'])
            self.assertEqual(after['queue'][0]['id'], writer['id'])
            self.assertEqual(after['queue'][0]['status'], 'Queue')
            self.assertFalse(after['controller']['dispatch'])
            self.assertTrue(after['queue'][0]['reason'])

    def test_fault_driver_selectors_resolve_without_loader_failures(self):
        loader = unittest.TestLoader()
        with redirect_stderr(io.StringIO()):
            suite = loader.loadTestsFromNames(closure_workflows.FAULT_TESTS)
        self.assertFalse(loader.errors, loader.errors)
        self.assertGreaterEqual(suite.countTestCases(), len(closure_workflows.FAULT_TESTS))

    def test_nested_denied_field_cannot_change_actual_payload_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/'owned.json'
            with patch('tests.campaign.qualification_v2.psutil.Process.num_fds', side_effect=psutil.AccessDenied(os.getpid())):
                with closure_workflows.monitored(self.id(), destination):
                    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'pass'])
                    self.assertEqual(child.wait(timeout=3), 0)
            report = json.loads(destination.read_text())
            self.assertEqual(report['outcomes']['functional_result'], 'PASS')
            self.assertEqual(report['outcomes']['resource_observability'], 'RESOURCE_QUALIFICATION_INCOMPLETE')
            self.assertTrue(report['outcomes']['execution_passed'])
            child_record, = [row for row in report['records'] if row['capture_origin'] == 'direct-launch']
            self.assertEqual(child_record['exit']['returncode'], 0)

    def test_sampler_crash_is_qualification_failure_with_cleanup_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/'owned.json'
            with patch.object(ProcessLedger, 'sample', side_effect=RuntimeError('injected sampler crash')):
                with closure_workflows.monitored(self.id(), destination):
                    pass
            report = json.loads(destination.read_text())
            self.assertEqual(report['outcomes']['functional_result'], 'PASS')
            self.assertEqual(report['outcomes']['observation_accounting'], 'FAIL')
            self.assertFalse(report['outcomes']['qualified'])
            self.assertFalse(report['remaining'])

    def test_identity_read_uncertainty_is_not_unsafe_signal_authority(self):
        record = {'ledger_id': 0, 'recorded_at': 0., 'role': 'controller', 'capture_origin': 'direct-launch',
                  'birth': None, 'exit': {'at': 2., 'returncode': 0, 'exit_evidence': 'original Popen wait/reap'}}
        observations = [{'ledger_id': 0, 'required_live': True, 'classification': 'genuinely_unobservable'}]
        report = coverage([record], observations, [], ended_at=2.)
        result = outcomes(functional_passed=True, safety_passed=report['control_identity_safety'] == 'PASS',
                          cleanup_passed=True, observation=report)
        self.assertEqual(result['functional_result'], 'PASS')
        self.assertEqual(result['control_identity_safety'], 'PASS')
        self.assertEqual(result['resource_observability'], 'RESOURCE_QUALIFICATION_INCOMPLETE')
        self.assertTrue(result['execution_passed'])
        self.assertFalse(result['qualified'])

    def test_cleanup_exception_is_saved_before_it_blocks_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/'owned.json'
            with patch.object(ProcessLedger, 'cleanup', side_effect=RuntimeError('injected cleanup failure')):
                with self.assertRaises(AssertionError):
                    with closure_workflows.monitored(self.id(), destination):
                        pass
            report = json.loads(destination.read_text())
            self.assertIn('injected cleanup failure', report['cleanup_error'])
            self.assertEqual(report['outcomes']['functional_result'], 'PASS')
            self.assertEqual(report['outcomes']['cleanup_result'], 'FAIL')

    def test_initial_sampler_crash_writes_top_level_failure(self):
        from tests.campaign.owned_run import run
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ProcessLedger, 'sample', side_effect=RuntimeError('initial sampler crash')):
                report = run(['/unexecuted'], Path(directory)/'owned', 1)
            self.assertFalse(report['passed'])
            self.assertIn('initial sampler crash', report['error'])
            self.assertIsNone(report['process_exits']['command_returncode'])
            self.assertTrue((Path(directory)/'owned'/'result.json').is_file())

    def test_cleanup_failure_remains_a_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ProcessLedger, 'cleanup', return_value=[{'unresolved': True}]):
                with self.assertRaises(AssertionError):
                    with closure_workflows.monitored(self.id(), Path(directory)/'owned.json'):
                        pass
            report = json.loads((Path(directory)/'owned.json').read_text())
            self.assertEqual(report['outcomes']['cleanup_result'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
