"""The ORCA shell consumes explicit drained outcomes, never exit-code guesses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.suspension_protocol import BINDING_FIELDS, SuspensionProtocolError, controlled_driver
from cmw.molecular.orca.suspension import verify_completion, write_completion


class OrcaCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-orca-completion-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.control = self.root / 'control'; self.control.mkdir(mode=0o700)
        self.scratch = self.root / 'scratch'; self.scratch.mkdir(mode=0o700)
        self.context = {key: key + '-fixture' for key in BINDING_FIELDS}
        self.context.update(protocol=1, backend='orca-local-v1', job_id=1,
            worker={'pid': 12340, 'birth': 110.0, 'host': 'fixture', 'boot': 100.0},
            anchor={'pid': 12341, 'birth': 111.0, 'host': 'fixture', 'boot': 100.0},
            directory_identity={'device': self.control.stat().st_dev,
                                'inode': self.control.stat().st_ino}, clock={'fixture': True})
        self.runner = {'pid': 12345, 'birth': 123.0, 'host': 'fixture', 'boot': 100.0}
        self.computation = {**self.runner, 'pid': 12346}
        self.outcome = {**self.context, 'runner': self.runner, 'root': self.computation, 'exit_code': 0,
                        'source': 'cmw-controlled-driver-drained-v1', 'disposition': 'completed'}
        self.record(self.control/'context.json', self.context)
        self.record(self.control/'registration.json', {**self.context, 'runner': self.runner, 'root': self.computation})
        self.path = self.scratch / 'controlled-completion.json'
        self.environ = patch.dict(os.environ, {'TMPDIR': str(self.scratch),
                                               'CMW_SUSPENSION_CONTEXT': str(self.control/'context.json')})
        self.environ.start(); self.addCleanup(self.environ.stop)
        clock = patch('cmw.molecular.orca.suspension.clock_descriptor', return_value={'fixture': True})
        clock.start(); self.addCleanup(clock.stop)
        for name, value in (('owner_alive', True), ('os.getsid', 12341), ('os.getpgid', 12341)):
            mocked = patch('cmw.molecular.orca.suspension.'+name, return_value=value)
            mocked.start(); self.addCleanup(mocked.stop)

    def record(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def verify(self, status=0):
        return verify_completion(self.path, process_status=status, runner_pid=self.runner['pid'])

    def test_normal_and_signal_shaped_failures_are_not_cancellation(self):
        for code in (0, 137, 143):
            with self.subTest(code=code):
                self.record(self.path, {**self.outcome, 'exit_code': code})
                self.assertEqual(self.verify(code), 'completed')

    def test_authenticated_cancelled_result_is_explicit(self):
        write_completion(self.path, {**self.outcome, 'disposition': 'cancelled', 'exit_code': 143})
        self.assertEqual(self.verify(143), 'cancelled')

    def test_cancel_after_outcome_before_shell_gate_prevents_finalization(self):
        write_completion(self.path, self.outcome)
        self.record(self.control/'command.json', {**self.context, 'operation': 'cancel',
                                                'revision': 1, 'request_id': 'cancel-fixture'})
        self.assertEqual(self.verify(), 'cancelled')

    def test_mismatched_late_cancel_does_not_authorize_a_disposition(self):
        write_completion(self.path, self.outcome)
        self.record(self.control/'command.json', {**self.context, 'attempt_id': 'other',
            'operation': 'cancel', 'revision': 1, 'request_id': 'cancel-fixture'})
        with self.assertRaises(SuspensionProtocolError): self.verify()

    def test_missing_or_unsafe_record_refuses(self):
        with self.assertRaises(FileNotFoundError): self.verify()
        self.record(self.path, self.outcome); self.path.chmod(0o644)
        with self.assertRaises(SuspensionProtocolError): self.verify()
        self.path.unlink(); self.path.symlink_to(self.control/'registration.json')
        with self.assertRaises(OSError): self.verify()

    def test_attempt_runner_source_and_status_mismatch_refuse(self):
        for change in ({'attempt_id': 'other'}, {'worker': 'other'}, {'anchor': 'other'},
                       {'runner': {**self.runner, 'birth': 999.0}}, {'root': {**self.computation, 'birth': 999.0}},
                       {'source': 'untrusted'},
                       {'exit_code': 137}, {'exit_code': False}):
            with self.subTest(change=change):
                self.record(self.path, {**self.outcome, **change})
                with self.assertRaises(SuspensionProtocolError): self.verify()


    def test_lost_authority_or_changed_session_refuses_finalization(self):
        write_completion(self.path, self.outcome)
        with patch('cmw.molecular.orca.suspension.owner_alive', return_value=False):
            with self.assertRaises(SuspensionProtocolError): self.verify()
        with patch('cmw.molecular.orca.suspension.os.getsid', return_value=99999):
            with self.assertRaises(SuspensionProtocolError): self.verify()

    def test_context_replacement_at_gate_refuses(self):
        from cmw.molecular.orca import suspension
        write_completion(self.path, self.outcome)
        original = suspension._read_private_record
        reads = 0
        def read(path):
            nonlocal reads
            result = original(path)
            if Path(path).name == 'context.json':
                reads += 1
                if reads == 2: return {**result, 'claim': 'changed'}
            return result
        with patch.object(suspension, '_read_private_record', side_effect=read):
            with self.assertRaises(SuspensionProtocolError): self.verify()

    def test_atomic_publication_does_not_clobber(self):
        write_completion(self.path, self.outcome)
        with self.assertRaises(FileExistsError): write_completion(self.path, {'changed': True})
        self.assertEqual(json.loads(self.path.read_text()), self.outcome)
        self.assertEqual(list(self.scratch.iterdir()), [self.path])

    def test_structured_result_waits_for_drain_and_keeps_cancellation(self):
        control = Mock(binding=self.context, runner=self.runner, cancel_requested=True,
                       _drain_known={12346: self.computation})
        control.poll.return_value = 'hold'  # A later hold cannot erase accepted cancellation.
        control.domain_drained.side_effect = [False, True]
        control.execution_budget_exhausted.return_value = False
        child = Mock(pid=12346); child.poll.return_value = -15
        with patch('cmw.jobs.suspension_protocol.RunnerControl.from_environment', return_value=control), \
             patch('cmw.jobs.suspension_protocol.subprocess.Popen', return_value=child), \
             patch('cmw.jobs.suspension_protocol.time.sleep'):
            result = controlled_driver([sys.executable, '-c', 'pass'], backend='orca-local-v1',
                                       completion_details=True)
        self.assertEqual(result, {**self.outcome, 'exit_code': 143, 'disposition': 'cancelled'})
        self.assertEqual(control.domain_drained.call_count, 2)
        control.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
