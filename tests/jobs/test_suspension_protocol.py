from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.ownership import identity
from cmw.jobs.suspension_protocol import (BINDING_FIELDS, RunnerControl, SuspensionProtocolError,
                                          clock_descriptor, now_clock)


class RunnerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw runner protocol ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.storage = self.root / 'storage'
        self.storage.mkdir()
        owner = identity()
        info = self.root.stat()
        self.context = {'protocol': 1, 'backend': 'fictional-v1', 'job_id': 'job-1',
                        'attempt_id': 'attempt-1', 'claim': 'claim-1', 'nonce': 'nonce-1',
                        'worker': owner, 'anchor': owner,
                        'state_identity': {'device': 1, 'inode': 2},
                        'directory_identity': {'device': info.st_dev, 'inode': info.st_ino},
                        'clock': clock_descriptor()}
        self.write('context.json', self.context)
        self.sid = patch('cmw.jobs.suspension_protocol.os.getsid', return_value=os.getpid())
        self.pgid = patch('cmw.jobs.suspension_protocol.os.getpgid', return_value=os.getpid())
        self.sid.start()
        self.pgid.start()
        self.addCleanup(self.sid.stop)
        self.addCleanup(self.pgid.stop)

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def make(self):
        control = RunnerControl(self.root / 'context.json', backend='fictional-v1',
                                deadline={'known': True, 'hard_timeout_seconds': None},
                                storage=[str(self.storage)], runtime={'local_only': True})
        self.addCleanup(control.close)
        control.clock_started = now_clock() - 10
        return control

    def command(self, revision, operation, *, request=None, **extra):
        value = {key: self.context[key] for key in BINDING_FIELDS}
        value.update(revision=revision, operation=operation, request_id=request or f'request-{revision}', **extra)
        self.write('command.json', value)
        return value

    def test_absent_enrollment_is_not_a_capability(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(RunnerControl.from_environment('fictional-v1', deadline={}, storage=[], runtime={}))

    def test_present_invalid_enrollment_refuses_before_launch(self):
        with patch.dict(os.environ, {'CMW_SUSPENSION_CONTEXT': str(self.root / 'foreign.json')}):
            with self.assertRaisesRegex(SuspensionProtocolError, 'context path'):
                RunnerControl.from_environment('fictional-v1', deadline={}, storage=[], runtime={})

    def test_unknown_backend_and_clock_refused(self):
        with self.assertRaisesRegex(SuspensionProtocolError, 'backend/protocol'):
            RunnerControl(self.root / 'context.json', backend='other-v1', deadline={}, storage=[], runtime={})
        self.context['clock']['boot'] -= 100
        self.write('context.json', self.context)
        with self.assertRaisesRegex(SuspensionProtocolError, 'clock/boot'):
            self.make()

    def test_duplicate_runner_cannot_register(self):
        self.make()
        with self.assertRaisesRegex(SuspensionProtocolError, 'already owns'):
            self.make()

    def test_nonprivate_and_redirected_context_refuse(self):
        (self.root / 'context.json').chmod(0o644)
        with self.assertRaisesRegex(SuspensionProtocolError, 'private'):
            self.make()
        (self.root / 'context.json').chmod(0o600)
        (self.root / 'redirect.json').symlink_to(self.root / 'context.json')
        with self.assertRaisesRegex(SuspensionProtocolError, 'context path'):
            RunnerControl(self.root / 'redirect.json', backend='fictional-v1', deadline={}, storage=[], runtime={})

    def test_suspend_continue_boundaries_are_idempotent(self):
        control = self.make()
        self.command(1, 'prepare_suspend')
        self.assertEqual(control.poll(), 'hold')
        self.assertEqual(control.intervals, [])
        boundary = now_clock() - 2
        self.command(1, 'suspended', boundary=boundary)
        self.assertEqual(control.poll(), 'hold')
        self.assertEqual(control.poll(), 'hold')
        self.assertEqual(len(control.intervals), 1)
        self.command(2, 'prepare_continue', boundary=boundary + 1)
        self.assertEqual(control.poll(), 'hold')
        self.assertAlmostEqual(control.suspended_seconds, 1)
        self.command(2, 'run')
        self.assertEqual(control.poll(), 'run')
        self.assertEqual(control.poll(), 'run')
        ack = json.loads((self.root / 'ack.json').read_text())
        self.assertEqual(ack['status'], 'running')
        self.assertEqual(ack['intervals'][0]['end'], boundary + 1)
        self.assertAlmostEqual(control.suspended_seconds, 1)

    def test_forged_and_stale_commands_hold_without_acknowledgement(self):
        control = self.make()
        value = self.command(1, 'prepare_suspend')
        value['claim'] = 'foreign'
        self.write('command.json', value)
        self.assertEqual(control.poll(), 'hold')
        self.assertIn('authority/attempt', control.last_error)
        self.assertFalse((self.root / 'ack.json').exists())
        self.command(2, 'prepare_suspend')
        control.poll()
        ack = (self.root / 'ack.json').read_bytes()
        self.command(1, 'run')
        self.assertEqual(control.poll(), 'hold')
        self.assertIn('Stale', control.last_error)
        self.assertEqual((self.root / 'ack.json').read_bytes(), ack)

    def test_changed_request_identity_cannot_release_hold(self):
        control = self.make()
        self.command(1, 'prepare_suspend')
        control.poll()
        self.command(1, 'run', request='another-user-action')
        self.assertEqual(control.poll(), 'hold')
        self.assertIn('Stale', control.last_error)

    def test_unprepared_confirmed_boundary_is_refused(self):
        control = self.make()
        self.command(1, 'suspended', boundary=now_clock())
        self.assertEqual(control.poll(), 'hold')
        self.assertIn('lacks preparation', control.last_error)
        self.assertEqual(control.intervals, [])

    def test_storage_replacement_and_worker_loss_fail_closed(self):
        control = self.make()
        self.storage.rename(self.root / 'old-storage')
        self.storage.mkdir()
        self.assertEqual(control.poll(), 'hold')
        self.assertIn('storage identity changed', control.last_error)
        with patch('cmw.jobs.suspension_protocol.owner_alive', return_value=False):
            self.assertEqual(control.poll(), 'hold')
            self.assertIn('authority', control.last_error.replace('worker or ownership anchor', 'authority'))

    def test_clock_reversal_and_boot_change_do_not_invent_duration(self):
        control = self.make()
        with patch('cmw.jobs.suspension_protocol.now_clock', return_value=control._last_clock - 1):
            self.assertEqual(control.poll(), 'hold')
            self.assertIn('backwards', control.last_error)
        with patch('cmw.jobs.suspension_protocol.clock_descriptor', return_value={'kind': 'changed', 'boot': 0}):
            self.assertEqual(control.poll(), 'hold')
            self.assertIn('clock/boot', control.last_error)

    def test_budget_excludes_only_confirmed_interval(self):
        control = self.make()
        start = control.clock_started
        with patch('cmw.jobs.suspension_protocol.now_clock', return_value=start + 20):
            self.assertTrue(control.execution_budget_exhausted(15))
            control.intervals = [{'start': start + 2, 'end': start + 12, 'request_id': 'one', 'revision': 1}]
            self.assertFalse(control.execution_budget_exhausted(15))
            self.assertEqual(control.unsuspended_elapsed, 10)
            control.intervals.append({'start': start + 15, 'end': None, 'request_id': 'two', 'revision': 2})
            self.assertEqual(control.suspended_seconds, 15)
            self.assertEqual(control.unsuspended_elapsed, 5)

    def test_cancel_and_hard_deadline_record_preserve_bound_authority(self):
        control = self.make()
        control.intervals = [{'start': now_clock() - 2, 'end': None, 'request_id': 'prior', 'revision': 1}]
        self.command(2, 'cancel')
        self.assertEqual(control.poll(), 'cancel')
        self.assertIsNotNone(control.intervals[-1]['end'])
        self.assertEqual(json.loads((self.root / 'ack.json').read_text())['status'], 'cancel_prepared')
        control.request_termination('hard wall deadline expired', exit_code=124)
        value = json.loads((self.root / 'deadline_expired.json').read_text())
        self.assertEqual(value['claim'], self.context['claim'])
        self.assertEqual(value['runner'], identity())
        self.assertEqual(value['exit_code'], 124)

    def test_missing_storage_still_allows_authenticated_termination(self):
        control = self.make()
        self.storage.rmdir()
        self.assertEqual(control.poll(), 'hold')
        control.request_termination('fixed hard deadline expired', exit_code=124)
        self.assertTrue((self.root / 'deadline_expired.json').exists())
        self.command(1, 'cancel')
        self.assertEqual(control.poll(), 'cancel')
        self.assertEqual(json.loads((self.root / 'ack.json').read_text())['status'], 'cancel_prepared')

    def test_invalidation_closes_only_confirmed_time_and_never_releases(self):
        control = self.make()
        self.command(1, 'prepare_suspend'); control.poll()
        start = now_clock() - 2
        self.command(1, 'suspended', boundary=start); control.poll()
        self.storage.rmdir()
        self.command(1, 'invalidate', boundary=start + 1)
        self.assertEqual(control.poll(), 'hold')
        self.assertEqual(control.intervals[-1]['end'], start + 1)
        self.assertTrue(control.accounting_uncertain)
        ack = json.loads((self.root / 'ack.json').read_text())
        self.assertEqual(ack['status'], 'invalidated')
        self.assertTrue(ack['accounting_uncertain'])
        self.assertEqual(control.poll(), 'hold')
        self.assertEqual(control.suspended_seconds, 1)

    def test_authority_loss_does_not_keep_pausing_execution_budget(self):
        control = self.make()
        self.command(1, 'prepare_suspend'); control.poll()
        start = now_clock() - 2
        self.command(1, 'suspended', boundary=start); control.poll()
        last_ack = control._last_ack_clock
        with patch('cmw.jobs.suspension_protocol.owner_alive', return_value=False):
            self.assertEqual(control.poll(), 'hold')
        self.assertTrue(control.accounting_uncertain)
        self.assertEqual(control.intervals[-1]['end'], last_ack)
        with patch('cmw.jobs.suspension_protocol.now_clock', return_value=last_ack + 100):
            self.assertLess(control.suspended_seconds, 3)
            self.assertGreater(control.unsuspended_elapsed, 100)


class MpiRuntimeIdentityTests(unittest.TestCase):
    def test_mpi4_logical_and_resolved_banner_bind_same_executable(self):
        from types import SimpleNamespace
        from cmw.jobs.suspension_protocol import mpi_runtime_identity
        with tempfile.TemporaryDirectory(prefix='cmw MPI identity ') as temporary:
            root = Path(temporary).resolve()
            for name in ('orterun', 'orted', 'ompi_info'):
                (root / name).write_text('fixture executable identity')
                (root / name).chmod(0o700)
            (root / 'mpirun').symlink_to(root / 'orterun')
            def version(command, **kwargs):
                if '--path' in command:
                    return SimpleNamespace(stdout='path:sysconfdir:' + str(root / 'etc'))
                name = Path(command[0]).name
                return SimpleNamespace(stdout=('mpirun (Open MPI) 4.1.6' if name == 'mpirun'
                                               else 'orterun (OpenRTE) 4.1.6') + '\n')
            with patch.dict(os.environ, {'HOME': str(root / 'home')}, clear=True), patch('cmw.jobs.suspension_protocol.subprocess.run', side_effect=version):
                logical = mpi_runtime_identity(root / 'mpirun', 2)
                resolved = mpi_runtime_identity(root / 'orterun', 2)
            self.assertEqual(logical['mpi_launcher']['path'], str(root / 'orterun'))
            self.assertEqual(logical['mpi_launcher']['version'], resolved['mpi_launcher']['version'])
            self.assertEqual(resolved['mpi_launcher']['observed_version'], 'orterun (OpenRTE) 4.1.6')

    def test_mpi5_records_verified_prrte_exec_alias(self):
        from types import SimpleNamespace
        from cmw.jobs.suspension_protocol import mpi_runtime_identity
        with tempfile.TemporaryDirectory(prefix='cmw PRRTE identity ') as temporary:
            root = Path(temporary).resolve()
            for name in ('mpirun', 'prte', 'prted', 'ompi_info', 'prte-info'):
                (root / name).write_text('fixture executable identity')
                (root / name).chmod(0o700)
            (root / 'prterun').symlink_to(root / 'prte')
            def version(command, **kwargs):
                if '--path' in command:
                    return SimpleNamespace(stdout='path:sysconfdir:' + str(root / 'etc'))
                return SimpleNamespace(stdout=('mpirun (Open MPI) 5.0.9' if Path(command[0]).name == 'mpirun'
                                               else 'prterun (PRRTE) 4.1.0') + '\n')
            with patch.dict(os.environ, {'PATH': str(root), 'HOME': str(root / 'home')}, clear=True), patch('cmw.jobs.suspension_protocol.subprocess.run', side_effect=version):
                value = mpi_runtime_identity(root / 'mpirun', 2)
            self.assertEqual(value['launcher_executables'][1]['path'], str(root / 'prte'))
            self.assertEqual(value['control_runtime']['version'], 'prterun (PRRTE) 4.1.0')


    def test_scheduler_allocation_cannot_claim_local_only_mpi(self):
        from cmw.jobs.suspension_protocol import mpi_runtime_identity
        for key in ('SLURM_JOB_ID', 'PBS_NODEFILE', 'SGE_ROOT', 'LSB_HOSTS'):
            with self.subTest(key=key), patch.dict(os.environ, {key: 'fixture'}, clear=True):
                with self.assertRaisesRegex(SuspensionProtocolError, 'Scheduler allocation'):
                    mpi_runtime_identity('/no-tool-should-be-opened', 2)

    def test_active_mca_configuration_is_refused_without_exposing_contents(self):
        from types import SimpleNamespace
        from cmw.jobs.suspension_protocol import _mpi_configuration_files
        with tempfile.TemporaryDirectory(prefix='cmw MPI config ') as temporary:
            root = Path(temporary).resolve()
            (root / 'ompi_info').write_text('fixture inspector'); (root / 'ompi_info').chmod(0o700)
            config = root / 'etc'; config.mkdir()
            path = config / 'openmpi-mca-params.conf'
            path.write_text('# comment only\n')
            output = SimpleNamespace(stdout='path:sysconfdir:' + str(config))
            with patch.dict(os.environ, {'HOME': str(root / 'home')}, clear=True), \
                 patch('cmw.jobs.suspension_protocol.subprocess.run', return_value=output):
                self.assertIn(str(path), _mpi_configuration_files(root / 'orterun', None))
                path.write_text('orte_default_hostfile=foreign-hosts\n')
                with self.assertRaisesRegex(SuspensionProtocolError, 'Unqualified MPI configuration') as caught:
                    _mpi_configuration_files(root / 'orterun', None)
                self.assertNotIn('foreign-hosts', str(caught.exception))


class ControlledDriverTests(unittest.TestCase):
    def test_deadline_authority_loss_does_not_exit_while_child_lives(self):
        import sys
        from unittest.mock import Mock
        from cmw.jobs.suspension_protocol import controlled_driver
        control = Mock()
        control.poll.return_value = 'hold'
        control.elapsed = 10
        control.request_termination.side_effect = SuspensionProtocolError('Worker authority lost')
        child = Mock(pid=123)
        child.poll.side_effect = [None, None, 0]
        with patch('cmw.jobs.suspension_protocol.RunnerControl.from_environment', return_value=control), \
             patch('cmw.jobs.suspension_protocol.subprocess.Popen', return_value=child) as launch, \
             patch('cmw.jobs.suspension_protocol.time.sleep'):
            self.assertEqual(controlled_driver([sys.executable, '-c', 'pass'], backend='local-command-v1',
                                               hard_timeout=1), 0)
        self.assertEqual(control.request_termination.call_count, 2)
        self.assertEqual(child.poll.call_count, 3)
        launch.assert_called_once()
        control.close.assert_called_once()

    def test_root_exit_waits_for_owned_modules_without_relaunch(self):
        import sys
        from unittest.mock import Mock
        from cmw.jobs.suspension_protocol import controlled_driver
        control = Mock()
        control.poll.return_value = 'hold'
        control.elapsed = 1
        control.execution_budget_exhausted.return_value = False
        control.domain_drained.side_effect = [False, False, True]
        child = Mock(pid=123)
        child.poll.return_value = 0
        with patch('cmw.jobs.suspension_protocol.RunnerControl.from_environment', return_value=control), \
             patch('cmw.jobs.suspension_protocol.subprocess.Popen', return_value=child) as launch, \
             patch('cmw.jobs.suspension_protocol.time.sleep'):
            self.assertEqual(controlled_driver([sys.executable, '-c', 'pass'], backend='local-command-v1'), 0)
        self.assertEqual(control.domain_drained.call_count, 3)
        launch.assert_called_once()
        control.request_termination.assert_not_called()
