"""Narrow regression checks for campaign evidence, never scientific execution."""
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

from cmw.nodes.protocol import NodeError, encode
from cmw.nodes.transport import ProcessTransport
from tests.campaign import acceptance
from tests.campaign.support import MeterTransport, ProcessLedger, protocol_report, summary


class ObservationTests(unittest.TestCase):
    def test_original_rank_quantile_is_preserved(self):
        self.assertEqual(summary(list(range(14)))['p95'], 13)
        self.assertEqual(summary(list(range(40)))['p95'], 38)

    def test_short_direct_child_has_launch_and_original_reaping_evidence(self):
        with ProcessLedger(self.id()) as ledger:
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'pass'])
            self.assertEqual(child.wait(timeout=3), 0)
            sample = ledger.sample()
            record, = [row for row in ledger.records if row['capture_origin'] == 'direct-launch']
            self.assertEqual(record['parent'], os.getpid())
            self.assertTrue(record['host'])
            self.assertIsNotNone(record['boot'])
            observed, = [row for row in sample['observations'] if row['ledger_id'] == record['ledger_id']]
            self.assertEqual(observed['classification'], 'verified_exited')
            self.assertEqual(observed['exit_evidence'], 'original Popen wait/reap')
            self.assertNotIn('sample', observed)
            self.assertFalse(ledger.cleanup(time.monotonic()+1))

    def test_denied_live_and_transient_reads_are_not_harmless_absence(self):
        ledger = ProcessLedger(self.id())
        for error, expected in ((psutil.AccessDenied(os.getpid()), 'access_denied'),
                                (psutil.NoSuchProcess(os.getpid()), 'transient_read_error')):
            with self.subTest(error=expected), patch('tests.campaign.support.resource_sample', side_effect=error):
                observed = ledger.observe(ledger.frontend)
            self.assertEqual(observed['classification'], expected)
            self.assertTrue(observed['required_live'])
            ledger.observations.append(observed)
        self.assertFalse(ledger.report()['coverage_passed'])

    def test_missing_birth_requires_original_popen_reap_not_later_adoption(self):
        ledger = ProcessLedger(self.id())
        row = {**ledger.frontend, 'ledger_id': 1, 'birth': None}
        direct = Mock()
        direct.poll.return_value = None
        ledger.direct[1] = direct
        self.assertEqual(ledger.observe(row)['classification'], 'genuinely_unobservable')
        direct.poll.return_value = 0
        direct.wait.return_value = 0
        observed = ledger.observe(row)
        self.assertEqual(observed['classification'], 'verified_exited')
        direct.wait.assert_called_once_with(timeout=0)
        self.assertIsNone(row['birth'])

    def test_reused_pid_keeps_each_original_direct_launch(self):
        ledger = ProcessLedger(self.id())
        process = Mock(pid=123456)
        process.ppid.return_value = os.getpid()
        process.create_time.side_effect = [1.0, 2.0]
        first, second = Mock(), Mock()
        identity = {'pid': process.pid, 'host': ledger.host, 'boot': ledger.boot}
        with patch('tests.campaign.support.process_identity', side_effect=[
                {**identity, 'birth': 1.0}, {**identity, 'birth': 2.0}]):
            old = ledger._register(process, 'old child', 'direct-launch', first)
            new = ledger._register(process, 'new child', 'direct-launch', second)
        self.assertNotEqual(old['ledger_id'], new['ledger_id'])
        self.assertIs(ledger.direct[old['ledger_id']], first)
        self.assertIs(ledger.direct[new['ledger_id']], second)
        first.poll.return_value = 0
        first.wait.return_value = 0
        self.assertEqual(ledger.observe(old)['classification'], 'verified_exited')
        self.assertNotIn(new['ledger_id'], ledger.terminal)

    def test_partial_identity_assembly_never_adopts_replacement_birth(self):
        ledger = ProcessLedger(self.id())
        process = Mock(pid=123456)
        process.create_time.return_value = 1.0
        process.ppid.side_effect = psutil.NoSuchProcess(process.pid)
        with patch('tests.campaign.support.process_identity', return_value={
                'pid': process.pid, 'host': ledger.host, 'boot': ledger.boot, 'birth': 2.0}):
            row = ledger._register(process, 'descendant', 'first-observed-descendant')
        self.assertIsNone(row['birth'])
        self.assertIn('NoSuchProcess', row['identity_error'])
        self.assertEqual(ledger.observe(row)['classification'], 'genuinely_unobservable')

    def test_registration_disappearance_requires_confirmed_absence(self):
        ledger = ProcessLedger(self.id())
        process = Mock(pid=123456)
        process.create_time.side_effect = psutil.NoSuchProcess(process.pid)
        row = ledger._register(process, 'descendant', 'first-observed-descendant')
        self.assertTrue(row['registration_disappearance'])
        with patch('tests.campaign.support.psutil.Process', side_effect=psutil.NoSuchProcess(process.pid)):
            observed = ledger.observe(row)
        self.assertEqual(observed['classification'], 'verified_exited')
        self.assertFalse(observed['required_live'])
        self.assertNotIn('sample', observed)
        self.assertIsNone(row['birth'])

    def test_registration_disappearance_cannot_adopt_reused_or_denied_pid(self):
        for denied in (False, True):
            with self.subTest(denied=denied):
                ledger = ProcessLedger(self.id())
                process = Mock(pid=123456)
                process.create_time.side_effect = psutil.NoSuchProcess(process.pid)
                row = ledger._register(process, 'descendant', 'first-observed-descendant')
                replacement = Mock()
                replacement.create_time.return_value = 9.0
                with patch('tests.campaign.support.psutil.Process', return_value=replacement,
                           side_effect=psutil.AccessDenied(process.pid) if denied else None):
                    observed = ledger.observe(row)
                    remaining = ledger.cleanup(time.monotonic())
                self.assertEqual(observed['classification'], 'access_denied' if denied else 'genuinely_unobservable')
                self.assertTrue(observed['required_live'])
                self.assertTrue(remaining)
                self.assertIsNone(row['birth'])
                replacement.terminate.assert_not_called()
                ledger.observations.append(observed)
                self.assertFalse(ledger.report()['coverage_passed'])

    def test_failed_start_is_retained(self):
        with ProcessLedger(self.id()) as ledger:
            with self.assertRaises(FileNotFoundError):
                subprocess.Popen(['/nonexistent/cmw-campaign-fixture'])
            self.assertEqual(len(ledger.launch_failures), 1)
            self.assertIn('FileNotFoundError', ledger.launch_failures[0]['error'])

    def test_failed_protocol_reply_keeps_actual_frame_and_latency(self):
        import cmw.nodes.transport as transport_module
        meter = MeterTransport(['/unexecuted'])
        frame = encode({'protocol': 1, 'request_id': '1'*32, 'ok': False,
                        'error': {'code': 'TEST_REFUSAL', 'message': 'expected'}})
        def refuse(*args, **kwargs):
            transport_module.decode(frame)
            raise NodeError('TEST_REFUSAL', 'expected')
        with patch.object(ProcessTransport, 'exchange', side_effect=refuse):
            with self.assertRaises(NodeError):
                meter.exchange('status')
        sample, = meter.samples
        self.assertEqual(sample['response_bytes'], len(frame))
        self.assertEqual(sample['outcome'], 'TEST_REFUSAL')
        self.assertEqual(sample['temperature'], 'cold')
        report = protocol_report(meter.samples)
        self.assertEqual(report['latency_by_operation']['status']['count'], 0)
        self.assertEqual(report['all_attempt_latency_by_operation']['status']['count'], 1)
        self.assertEqual(report['encoded_received_response_bytes'], len(frame))
        meter.expected_refusal_reason = 'deliberate unavailable-owner control'
        with patch.object(ProcessTransport, 'exchange', side_effect=refuse):
            with self.assertRaises(NodeError):
                meter.exchange('status')
        self.assertTrue(meter.samples[-1]['expected_refusal'])
        with patch.object(ProcessTransport, 'exchange', return_value={}):
            meter.exchange('status')
        self.assertFalse(meter.samples[-1]['expected_refusal'])


    def test_pre_binding_refusal_still_writes_cleanup_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'refused'
            with patch.object(acceptance, 'runtime_identity', side_effect=RuntimeError('refused')):
                with self.assertRaisesRegex(RuntimeError, 'refused'):
                    acceptance.run(output, 'deterministic', {'fingerprint': 'unreached'})
            report = json.loads((output/'result.json').read_text())
            self.assertFalse(report['passed'])
            self.assertTrue(report['cleanup']['sampler_stopped'])
            self.assertEqual(report['cleanup']['deadline_seconds'], 30)
            self.assertIsNone(report['workload_seconds'])
            self.assertIn('refused', report['error'])

    def test_top_level_runner_reaps_success_and_retains_failed_command(self):
        from tests.campaign.owned_run import run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = run([sys.executable, '-I', '-B', '-c', 'pass'], root/'good', 3)
            self.assertTrue(good['passed'], good)
            self.assertEqual(good['reaped_returncode'], 0)
            bad = run([sys.executable, '-I', '-B', '-c', 'raise SystemExit(7)'], root/'bad', 3)
            self.assertFalse(bad['passed'])
            self.assertEqual(bad['returncode'], 7)
            self.assertFalse(bad['remaining'])

    def test_top_level_runner_timeout_has_separate_cleanup_and_reaping(self):
        from tests.campaign.owned_run import run
        with tempfile.TemporaryDirectory() as directory:
            result = run([sys.executable, '-I', '-B', '-c', 'import time;time.sleep(5)'],
                         Path(directory)/'timeout', .05)
            self.assertFalse(result['passed'])
            self.assertIn('TimeoutExpired', result['error'])
            self.assertLess(result['cleanup_seconds'], 3)
            self.assertFalse(result['remaining'])
            self.assertIsNotNone(result['reaped_returncode'])
