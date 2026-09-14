"""Unknown show observes fixture identities and locks without granting authority."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.cli import build_parser
from cmw.jobs import activity, cli, ownership
from cmw.jobs.store import Store


class UnknownDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-unknown-show-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['/bin/echo', 'synthetic'], cwd=self.root, name='fixture')
        self.owner = {'pid': os.getpid(), 'birth': 100.0, 'host': 'fixture-host', 'boot': 1.0}
        self.lock = self.store.root/'attempts'/self.job['attempt_id']/'worker.lock'

    @contextmanager
    def worker_probes(self, *, current=None, status=psutil.STATUS_SLEEPING):
        with patch('socket.gethostname', return_value='fixture-host'), \
             patch.object(psutil, 'boot_time', return_value=1.0), \
             patch.object(ownership, 'identity', return_value=current or self.owner) as lookup, \
             patch.object(psutil, 'Process', return_value=Mock(status=Mock(return_value=status))) as process:
            yield lookup, process

    def show(self, state, operation='show', *, json_output=True):
        command = ['jobs', '--state', str(self.store.root), operation]
        if operation != 'status':
            command.append(self.job['display_id'])
        if json_output:
            command.append('--json')
        args = build_parser().parse_args(command)
        output = io.StringIO()
        with patch.object(activity, 'project', return_value=state), redirect_stdout(output):
            self.assertEqual(args.handler(args), 0)
        return json.loads(output.getvalue())

    def test_stale_pid_show_explains_mismatch_without_persisting_or_creating_attempt(self):
        stale = {**self.owner, 'birth': self.owner['birth']-100}
        with self.store.transaction() as con:
            row = self.store.get(con, self.job['id'])
            row.update(status='Unknown', worker=stale, order=None,
                       reason='Worker ownership/completion cannot be verified; automatic progression stopped')
            self.store.save(con, row)
        state = self.store.snapshot()
        original = deepcopy(state)
        database = self.store.path.read_bytes()
        names = sorted(path.name for path in self.store.root.iterdir())
        mode = self.store.root.stat().st_mode
        with self.worker_probes(), \
             patch.object(Store, 'transaction', side_effect=AssertionError('show cannot write')), \
             patch.object(ownership, 'lock_held', side_effect=AssertionError('missing lock cannot be probed')), \
             patch.object(os, 'kill', side_effect=AssertionError('show cannot signal')), \
             patch.object(os, 'killpg', side_effect=AssertionError('show cannot signal')):
            result = self.show(state)
        self.assertEqual(result['status'], 'Unknown')
        self.assertEqual(result['worker'], stale)
        observed = result['ownership_observation']
        self.assertTrue(observed['advisory'])
        self.assertIsInstance(observed['observed_at'], float)
        self.assertEqual(observed['worker']['verdict'], 'mismatch')
        self.assertEqual(observed['worker']['observed_identity'], self.owner)
        self.assertEqual(observed['worker_lock']['verdict'], 'missing')
        self.assertEqual(state, original)
        self.assertEqual(self.store.path.read_bytes(), database)
        self.assertEqual(sorted(path.name for path in self.store.root.iterdir()), names)
        self.assertEqual(self.store.root.stat().st_mode, mode)
        self.assertNotIn('ownership_observation', self.store.snapshot()['jobs'][0])

    def test_missing_and_invalid_identity_do_not_probe_pid(self):
        with self.worker_probes() as (lookup, process):
            for owner in (None, {}, {'pid': 42}):
                self.assertEqual(cli._unknown_worker_observation(owner)['verdict'], 'missing')
            for pid in (0, -1, True, '42'):
                self.assertEqual(cli._unknown_worker_observation({**self.owner, 'pid': pid})['verdict'], 'unavailable')
            lookup.assert_not_called()
            process.assert_not_called()

    def test_host_and_boot_mismatch_do_not_probe_unrelated_local_pid(self):
        with self.worker_probes() as (lookup, process):
            for field, value in (('host', 'other-host'), ('boot', 2.0)):
                with self.subTest(field=field):
                    result = cli._unknown_worker_observation({**self.owner, field: value})
                    self.assertEqual(result['verdict'], 'mismatch')
                    self.assertIn(field, result['reason'])
            lookup.assert_not_called()
            process.assert_not_called()

    def test_matched_gone_zombie_and_denied_are_distinct(self):
        with self.worker_probes() as (lookup, process):
            self.assertEqual(cli._unknown_worker_observation(self.owner)['verdict'], 'matched')
            process.return_value.status.return_value = psutil.STATUS_ZOMBIE
            self.assertEqual(cli._unknown_worker_observation(self.owner)['verdict'], 'gone')
            for error, expected in ((psutil.NoSuchProcess(self.owner['pid']), 'gone'),
                                    (psutil.AccessDenied(self.owner['pid']), 'unavailable'),
                                    (PermissionError('private path must not appear'), 'unavailable')):
                with self.subTest(error=type(error).__name__):
                    lookup.side_effect = error
                    result = cli._unknown_worker_observation(self.owner)
                    self.assertEqual(result['verdict'], expected)
                    self.assertNotIn('private path', result['reason'])

    def test_identity_change_during_status_observation_is_not_a_match(self):
        with self.worker_probes() as (lookup, _):
            lookup.side_effect = [self.owner, {**self.owner, 'birth': 200.0}]
            self.assertEqual(cli._unknown_worker_observation(self.owner)['verdict'], 'mismatch')

    def test_held_and_unheld_lock_leave_fixture_contents_and_mode_unchanged(self):
        self.lock.parent.mkdir(parents=True)
        self.lock.write_text('fixture lock')
        before = self.lock.stat()
        with ownership.exclusive(self.lock), patch.object(ownership, 'lock_held', wraps=ownership.lock_held) as probe:
            self.assertEqual(cli._unknown_lock_observation(self.store, self.job['attempt_id'])['verdict'], 'held')
            probe.assert_called_once_with(self.lock, strict=True)
        self.assertEqual(cli._unknown_lock_observation(self.store, self.job['attempt_id'])['verdict'], 'unheld')
        after = self.lock.stat()
        self.assertEqual((before.st_mode, before.st_mtime_ns, before.st_ino),
                         (after.st_mode, after.st_mtime_ns, after.st_ino))
        self.assertEqual(self.lock.read_text(), 'fixture lock')

    def test_lock_path_and_probe_errors_are_unavailable_not_unheld(self):
        self.lock.parent.mkdir(parents=True)
        self.lock.touch()
        with patch.object(Path, 'lstat', side_effect=PermissionError('private location')), \
             patch.object(ownership, 'lock_held') as probe:
            result = cli._unknown_lock_observation(self.store, self.job['attempt_id'])
            self.assertEqual(result['verdict'], 'unavailable')
            self.assertNotIn('private location', result['reason'])
            probe.assert_not_called()
        with patch.object(ownership, 'lock_held', side_effect=PermissionError('private location')):
            self.assertEqual(cli._unknown_lock_observation(self.store, self.job['attempt_id'])['verdict'], 'unavailable')
        with patch.object(ownership, 'lock_held', side_effect=FileNotFoundError):
            self.assertEqual(cli._unknown_lock_observation(self.store, self.job['attempt_id'])['verdict'], 'missing')

    def test_invalid_and_redirected_attempt_paths_are_not_probed(self):
        with patch.object(ownership, 'lock_held') as probe:
            for identifier in (None, '', '../escape', '/tmp/escape', 'f'*31):
                self.assertEqual(cli._unknown_lock_observation(self.store, identifier)['verdict'], 'unavailable')
            self.lock.parent.mkdir(parents=True)
            target = self.root/'outside.lock'
            target.write_text('untouched')
            self.lock.symlink_to(target)
            self.assertEqual(cli._unknown_lock_observation(self.store, self.job['attempt_id'])['verdict'], 'unavailable')
            self.lock.unlink()
            self.lock.parent.rmdir()
            self.lock.parent.symlink_to(self.root, target_is_directory=True)
            self.assertEqual(cli._unknown_lock_observation(self.store, self.job['attempt_id'])['verdict'], 'unavailable')
            probe.assert_not_called()
            self.assertEqual(target.read_text(), 'untouched')

    def test_only_unknown_show_gets_observation_in_both_output_formats(self):
        state = self.store.snapshot()
        state['jobs'][0]['status'] = 'Unknown'
        with patch.object(cli, '_unknown_ownership_observation', return_value={'advisory': True}) as observe:
            for json_output in (False, True):
                self.assertEqual(self.show(state, json_output=json_output)['ownership_observation'], {'advisory': True})
            self.assertEqual(observe.call_count, 2)
        with patch.object(cli, '_unknown_ownership_observation', side_effect=AssertionError('unexpected probe')):
            for status in ('Queue', 'Starting', 'Run', 'Cancelling', 'Done', 'Fail', 'Cancelled'):
                state['jobs'][0]['status'] = status
                self.assertNotIn('ownership_observation', self.show(state))
            state['jobs'][0]['status'] = 'Unknown'
            self.assertNotIn('ownership_observation', self.show(state, 'status')['jobs'][0])
            self.assertNotIn('ownership_observation', self.show(state, 'logs'))


if __name__ == '__main__':
    unittest.main()
