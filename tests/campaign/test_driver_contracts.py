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
        with patch('tests.campaign.qualification_v2.psutil.Process', return_value=process):
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
        with patch('tests.campaign.qualification_v2.psutil.Process', return_value=process):
            values, fields = field_sample(process, 123.)
        self.assertEqual(fields['rss_bytes']['classification'], 'transient_read_uncertainty')
        self.assertEqual(fields['rss_bytes']['post_identity']['birth'], 123.)
        self.assertEqual(fields['rss_bytes']['post_identity']['status'], psutil.STATUS_RUNNING)
        self.assertEqual(values['fds'], 9)
        self.assertNotIn('rss_bytes', values)

    def test_wrong_birth_never_publishes_a_resource_value(self):
        process = self.process()
        with patch('tests.campaign.qualification_v2.psutil.Process', return_value=process):
            values, fields = field_sample(process, 124.)
        self.assertEqual(set(values), {'pid', 'birth'})
        self.assertTrue(all(value['classification'] == 'identity_mismatch' for value in fields.values()))
        process.memory_info.assert_not_called()

    def test_each_field_identity_bracket_uses_fresh_handles_not_cached_birth(self):
        process = self.process()
        identities = [self.process() for _ in range(2*len(FIELDS))]
        # The original resource handle would keep returning its cached birth.
        # Replacement occurs during its first resource call and must invalidate it.
        for identity in identities[1:]:
            identity.create_time.return_value = 124.
        with patch('tests.campaign.qualification_v2.psutil.Process', side_effect=identities) as factory:
            values, fields = field_sample(process, 123.)
        self.assertEqual(factory.call_count, 2*len(FIELDS))
        self.assertTrue(all(call.args == (process.pid,) for call in factory.call_args_list))
        self.assertEqual(len({id(identity) for identity in identities}), 2*len(FIELDS))
        process.create_time.assert_not_called()
        for identity in identities:
            identity.create_time.assert_called_once_with()
        self.assertEqual(fields['rss_bytes']['pre_identity']['birth'], 123.)
        self.assertEqual(fields['rss_bytes']['post_identity']['birth'], 124.)
        self.assertEqual(fields['rss_bytes']['classification'], 'identity_mismatch')
        self.assertEqual(set(values), {'pid', 'birth'})
        process.memory_info.assert_called_once_with()
        process.num_fds.assert_not_called()
        process.cpu_times.assert_not_called()

    def test_fresh_identity_brackets_preserve_rss_and_fd_when_cpu_is_denied(self):
        process = self.process()
        process.cpu_times.side_effect = psutil.AccessDenied(process.pid)
        identities = [self.process() for _ in range(2*len(FIELDS))]
        with patch('tests.campaign.qualification_v2.psutil.Process', side_effect=identities):
            values, fields = field_sample(process, 123.)
        self.assertEqual(values['rss_bytes'], 1234)
        self.assertEqual(values['fds'], 9)
        self.assertNotIn('cpu_seconds', values)
        self.assertEqual(fields['cpu_seconds']['classification'], 'access_denied')
        self.assertEqual(fields['cpu_seconds']['method'], 'psutil.Process.cpu_times')
        self.assertEqual(fields['cpu_seconds']['pre_identity']['birth'], 123.)
        self.assertEqual(fields['cpu_seconds']['post_identity']['birth'], 123.)
        process.create_time.assert_not_called()

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

    def test_zombie_tail_is_separate_from_live_blindness_and_actual_gaps_remain(self):
        record = {'ledger_id': 64, 'birth': 123., 'recorded_at': 0., 'role': 'owned descendant',
                  'capture_origin': 'first-observed-descendant'}
        observations = [{'ledger_id': 64, 'birth': 123., 'at': value, 'required_live': True,
            'classification': 'sampled_live',
            'fields': {field: {'ended_at': value, 'classification': 'sampled_live'} for field in FIELDS}}
            for value in (.01, .13, .25, .37, .49, .62, 1.40)]
        observations.extend([{'ledger_id': 64, 'birth': 123., 'at': 1.52,
            'required_live': False, 'classification': 'zombie_reaping_pending'},
            {'ledger_id': 64, 'birth': 123., 'at': 65.3, 'required_live': False, 'classification': 'verified_exited'}])
        result = coverage([record], observations, [], ended_at=70.)
        self.assertEqual(result['resource_observability'], 'RESOURCE_QUALIFICATION_INCOMPLETE')
        for row in result['field_coverage']:
            self.assertEqual(row['lifetime_seconds'], 65.3)
            self.assertEqual(row['resource_window_seconds'], 1.52)
            self.assertAlmostEqual(row['zombie_pending_reap_seconds'], 63.78)
            self.assertAlmostEqual(row['longest_blind_seconds'], .78)
            self.assertNotIn('exit', record)
            if row['required']:
                self.assertIn('required_blind_interval', row['failures'])
        observations[-2]['birth'] = 999.
        result = coverage([record], observations, [], ended_at=70.)
        self.assertGreater(result['field_coverage'][0]['longest_blind_seconds'], 60.)

    @unittest.skipUnless(sys.platform == 'darwin', 'Native zombie lifecycle fixture is macOS qualification')
    def test_native_zombie_retains_reap_responsibility_without_resource_blindness(self):
        import select
        code = """import os, sys
read_gate, write_gate = os.pipe()
child = os.fork()
if child == 0:
    os.close(write_gate)
    os.read(read_gate, 1)
    os._exit(0)
os.close(read_gate)
print(child, flush=True)
sys.stdin.buffer.read(1)
os.write(write_gate, b'X')
os.close(write_gate)
sys.stdin.buffer.read(1)
_, status = os.waitpid(child, 0)
assert os.waitstatus_to_exitcode(status) == 0
"""
        evidence = {'scope': 'native benign grandchild live-to-zombie-to-parent-reap', 'samples': []}
        with ProcessLedger(self.id()) as ledger:
            parent = subprocess.Popen([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
            try:
                ready, _, _ = select.select([parent.stdout], [], [], 3.)
                self.assertTrue(ready)
                child_pid = int(parent.stdout.readline())
                evidence['samples'].append(ledger.sample())
                child_row, = [row for row in ledger.records if row['pid'] == child_pid]
                parent.stdin.write(b'X'); parent.stdin.flush()
                deadline = time.monotonic()+3.
                while psutil.Process(child_pid).status() != psutil.STATUS_ZOMBIE:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                evidence['samples'].append(ledger.sample())
                zombie, = [row for row in evidence['samples'][-1]['observations'] if row['pid'] == child_pid]
                self.assertEqual(zombie['classification'], 'zombie_reaping_pending')
                self.assertFalse(zombie['required_live'])
                self.assertNotIn(child_row['ledger_id'], ledger.terminal)
                self.assertNotIn('exit', child_row)
                parent.stdin.write(b'Y'); parent.stdin.flush()
                self.assertEqual(parent.wait(timeout=3.), 0)
                evidence['samples'].append(ledger.sample())
                self.assertIn(child_row['ledger_id'], ledger.terminal)
                report = ledger.report()
                fields = [row for row in report['field_coverage'] if row['ledger_id'] == child_row['ledger_id']]
                self.assertTrue(all(row['resource_end_basis'].startswith('same-birth zombie') for row in fields))
                self.assertTrue(all(row['resource_end_upper_bound'] <= row['lifecycle_end_upper_bound'] for row in fields))
                evidence['child_field_coverage'] = fields
                evidence['actual_parent_returncode'] = parent.returncode
            finally:
                try:
                    if parent.poll() is None:
                        parent.stdin.write(b'XY'); parent.stdin.flush()
                        parent.wait(timeout=3.)
                finally:
                    parent.stdin.close(); parent.stdout.close(); parent.stderr.close()
                    evidence['remaining'] = ledger.cleanup(time.monotonic()+3.)
                    evidence['records'] = ledger.records
                    if destination := os.environ.get('CMW_TEST_EVIDENCE_DIR'):
                        closure_workflows.save(Path(destination)/'zombie-lifecycle.json', evidence)
            self.assertFalse(evidence['remaining'])

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

    def test_descendant_discovery_skips_only_roots_already_in_verified_tree(self):
        for parent_sees_child in (True, False):
            with self.subTest(parent_sees_child=parent_sees_child):
                ledger = ProcessLedger(self.id())
                child_row = {**ledger.frontend, 'pid': 123456, 'birth': 123., 'ledger_id': 1,
                             'capture_origin': 'direct-launch', 'role': 'controller'}
                ledger.records.append(child_row)
                parent, child = Mock(pid=ledger.frontend['pid']), Mock(pid=child_row['pid'])
                parent.create_time.return_value = ledger.frontend['birth']
                child.create_time.return_value = child_row['birth']
                parent.children.return_value = [child] if parent_sees_child else []
                child.children.return_value = []
                def observation(row):
                    return {'ledger_id': row['ledger_id'], 'pid': row['pid'], 'birth': row['birth'],
                            'classification': 'sampled_live', 'required_live': True}
                with patch('tests.campaign.support.psutil.Process', side_effect=lambda pid: parent if pid == parent.pid else child), \
                     patch.object(ledger, '_register', return_value=child_row), \
                     patch.object(ledger, 'observe', side_effect=observation):
                    ledger.sample()
                parent.children.assert_called_once_with(recursive=True)
                if parent_sees_child:
                    child.children.assert_not_called()
                else:
                    child.children.assert_called_once_with(recursive=True)

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

    def test_streamed_report_keeps_owned_shape_encoding_and_newline(self):
        from tests.campaign.active_writer_pty import save as writer_save
        from tests.campaign.closure_pty import write as pty_write
        value = {'samples': [{'at': 1.25, 'observations': [{'ledger_id': 1, 'role': 'endpoint',
            'fields': {'fds': {'classification': 'access_denied', 'value': None,
                              'message': 'fixture denied', 'errno': 13}}}], 'processes': []}],
            'outcomes': {'functional_result': 'PASS', 'resource_observability': 'RESOURCE_QUALIFICATION_INCOMPLETE'},
            'remaining': [], 'label': '研究'}
        expected = json.dumps(value, indent=2)+'\n'
        with tempfile.TemporaryDirectory() as directory:
            for index, writer in enumerate((closure_workflows.save, writer_save, pty_write)):
                target = Path(directory)/f'owned-{index}.json'
                writer(target, value)
                self.assertEqual(target.read_text(), expected)
                self.assertEqual(json.loads(target.read_text()), value)

    def test_readiness_diagnostic_retains_original_refusal_without_retry(self):
        from cmw.nodes import identity
        config = {'node_id': 'original', 'runtime': {'fingerprint': 'original'},
                  'runtime_observation': {'original': True}}
        refusal = {'ready': False, 'errors': ['exact refusal'],
                   'runtime_diagnostic': {'category': 'in_flight_change', 'after': {'epoch': 42}}}
        report = {}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(identity, 'check_node', return_value=refusal) as original:
                with closure_workflows.readiness_diagnostics(directory, report):
                    self.assertIs(identity.check_node(config, operation='execution'), refusal)
                original.assert_called_once_with(config, operation='execution')
            record, = report['readiness_refusals']
            observed = json.loads(Path(record['path']).read_text())
            self.assertEqual(observed['readiness'], refusal)
            self.assertEqual(observed['original_observation'], config['runtime_observation'])
            self.assertEqual(record['category'], 'in_flight_change')
            self.assertTrue(observed['call_stack'])

    def test_readiness_capture_failure_does_not_replace_actual_refusal(self):
        from cmw.nodes import identity
        config = {'node_id': 'original', 'runtime': {}, 'runtime_observation': {}}
        refusal = {'ready': False, 'errors': ['original refusal']}
        report = {}
        with patch.object(identity, 'check_node', return_value=refusal) as original:
            with patch.object(closure_workflows, 'save', side_effect=OSError('capture denied')):
                with closure_workflows.readiness_diagnostics('/not-written', report):
                    self.assertIs(identity.check_node(config), refusal)
            original.assert_called_once_with(config)
        self.assertEqual(report['readiness_capture_errors'],
                         [{'type': 'OSError', 'message': 'capture denied'}])

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
            self.assertEqual(child_record['argv'], child.args)

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
