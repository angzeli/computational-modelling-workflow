"""Fixture failure is not permission to leave synthetic processes behind."""
from __future__ import annotations

import errno
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cmw.jobs import runtime
from cmw.jobs.ownership import OwnershipStatus, observe_owner
from cmw.jobs.store import Store
from tests.jobs.isolated_runtime import install
from tests.runtime.owned_process_cleanup import OwnedProcessCleanup
from tests.runtime import owned_process_cleanup


def until(predicate, seconds=8):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError('Synthetic cleanup fixture did not start')


class OwnedProcessCleanupTests(unittest.TestCase):
    def failed_case(self, failure):
        class Fixture(unittest.TestCase):
            def setUp(self):
                isolation = install()
                self.addCleanup(isolation.close)
                self.temporary = tempfile.TemporaryDirectory(prefix='cmw-cleanup-regression-')
                self.addCleanup(self.remove_directory)
                self.root = Path(self.temporary.name)
                self.store = Store(self.root/'state')
                self.cleanup = OwnedProcessCleanup(self.store, self.root)
                self.addCleanup(self.cleanup.close)
                detached = runtime.detached
                launch = patch.object(runtime, 'detached',
                    side_effect=lambda *args, **kwargs: self.cleanup.acquired(detached(*args, **kwargs)))
                launch.start()
                self.addCleanup(launch.stop)
                script = self.root/'payload.py'
                script.write_text('''import json,pathlib,subprocess,sys,time
from cmw.jobs.ownership import identity
root=pathlib.Path(sys.argv[1])
(root/'driver.json').write_text(json.dumps({'owner':identity()}))
child=subprocess.Popen([sys.executable,'-c',
    "import json,pathlib,sys,time; from cmw.jobs.ownership import identity; "
    "pathlib.Path(sys.argv[1]).write_text(json.dumps({'owner':identity()})); time.sleep(60)",
    str(root/'module-0.json')])
child.wait()
''')
                self.job = self.store.add(argv=[sys.executable, str(script), str(self.root)],
                    cwd=self.root, name='private cleanup regression', cpus=1)
                if failure == 'setup':
                    self.store.change(self.job['id'], 'hold')
                runtime.start(self.store)
                if failure == 'setup':
                    raise AssertionError('forced setup failure after controller acquisition')
                until((self.root/'module-0.json').exists)
                self.cleanup.collect()

            def remove_directory(self):
                if self.cleanup.complete:
                    self.temporary.cleanup()
                else:
                    self.temporary._finalizer.detach()

            def runTest(self):
                with self.store.transaction() as con:
                    job = self.store.get(con, self.job['id'])
                    job.update(status='Cancelling' if failure == 'cancel-race' else 'Unknown',
                               cancel_requested=failure == 'cancel-race')
                    self.store.save(con, job)
                if failure == 'cancel-race':
                    original = self.store.change
                    def changed(*args, **kwargs):
                        with self.store.transaction() as con:
                            job = self.store.get(con, self.job['id'])
                            job['status'] = 'Unknown'
                            self.store.save(con, job)
                        return original(*args, **kwargs)
                    # Keep this failure injection active until after cleanup.
                    self.store.change = changed
                raise AssertionError('forced body failure with live fixture processes')

        fixture = Fixture()
        result = unittest.TestResult()
        fixture.run(result)
        self.assertEqual(len(result.failures), 1, result.errors)
        self.assertIn('forced setup failure' if failure == 'setup' else 'forced body failure', result.failures[0][1])
        self.assertEqual(result.errors, [])
        self.assertTrue(fixture.cleanup.complete)
        with self.assertRaises(FileNotFoundError):
            fixture.root.stat()
        self.assertGreaterEqual(len(fixture.cleanup.records), 1 if failure == 'setup' else 5)
        self.assertTrue(all(process.returncode is not None for process in fixture.cleanup.handles))
        for owner in fixture.cleanup.records.values():
            self.assertEqual(observe_owner(owner).status, OwnershipStatus.GONE, owner)
        if failure != 'setup':
            self.assertTrue(any('Cannot cancel Unknown' in reason for reason in fixture.cleanup.refusals))

    def test_body_failure_with_unknown_still_reaps_every_owned_process(self):
        self.failed_case('unknown')

    def test_cancelling_to_unknown_during_cleanup_still_reaps_every_owned_process(self):
        self.failed_case('cancel-race')

    def test_setup_failure_after_controller_acquisition_still_reaps_it(self):
        self.failed_case('setup')

    def test_unverifiable_or_reused_identity_never_authorizes_cleanup_signals(self):
        cleanup = OwnedProcessCleanup(Mock(), Path('/unused'))
        record = {'pid': 12345, 'version': 2, 'start_token': 1}
        cleanup.remember(record)
        for status in (OwnershipStatus.UNVERIFIABLE, OwnershipStatus.GONE):
            with self.subTest(status=status), \
                    patch.object(owned_process_cleanup, 'observe_owner', return_value=SimpleNamespace(status=status)), \
                    patch.object(owned_process_cleanup.os, 'kill') as kill:
                for signum in (signal.SIGTERM, signal.SIGCONT, signal.SIGKILL):
                    cleanup.send(signum, controller=True)
                kill.assert_not_called()

    def test_failed_queue_read_does_not_skip_independent_fixture_identity(self):
        with tempfile.TemporaryDirectory(prefix='cmw-cleanup-evidence-') as temporary:
            root = Path(temporary)
            store = Store(root/'state')
            store.add(argv=['/bin/true'], cwd=root, name='evidence fixture', cpus=1)
            record = {'pid': 12345, 'version': 2, 'start_token': 1}
            (root/'driver.json').write_text(json.dumps({'owner': record}))
            cleanup = OwnedProcessCleanup(store, root)
            with patch.object(store, 'transaction', side_effect=PermissionError('queue denied')), \
                    patch.object(owned_process_cleanup, 'observe_owner', return_value=SimpleNamespace(status=OwnershipStatus.GONE)):
                with self.assertRaisesRegex(AssertionError, 'queue denied'):
                    cleanup.collect()
            self.assertIn(record, cleanup.records.values())

    def test_unreadable_queue_keeps_evidence_and_runs_independent_cleanup(self):
        from tests.runtime.test_orca_suspend_lifecycle import OrcaSuspendLifecycleTests
        for failed_read in ('stat', 'transaction'):
            with self.subTest(read=failed_read), \
                    tempfile.TemporaryDirectory(prefix='cmw-cleanup-unreadable-') as directory:
                root = Path(directory)
                store = Store(root/'state')
                store.add(argv=['/bin/true'], cwd=root, name='unreadable fixture inventory', cpus=1)
                record = {'pid': 12345, 'version': 2, 'start_token': 1}
                evidence = root/'driver.json'
                evidence.write_text(json.dumps({'owner': record}))
                cleanup = OwnedProcessCleanup(store, root)
                original_stat = Path.stat

                def stat(path, *args, **kwargs):
                    if path == store.path:
                        raise PermissionError(errno.EACCES, 'queue stat denied')
                    return original_stat(path, *args, **kwargs)

                failure = (patch.object(Path, 'stat', stat) if failed_read == 'stat' else
                           patch.object(store, 'transaction',
                                        side_effect=PermissionError(errno.EACCES, 'queue read denied')))
                with failure, \
                        patch.object(owned_process_cleanup, 'observe_owner',
                                     return_value=SimpleNamespace(status=OwnershipStatus.GONE)), \
                        patch.object(cleanup, 'send') as send, \
                        patch.object(cleanup, 'wait_gone', return_value=True) as wait, \
                        patch.object(cleanup, 'reap') as reap:
                    with self.assertRaisesRegex(AssertionError, 'queue (stat|read) denied'):
                        cleanup.close()
                self.assertFalse(cleanup.complete)
                self.assertIn(record, cleanup.records.values())
                self.assertEqual(send.call_count, 5)
                self.assertEqual(wait.call_count, 5)
                self.assertTrue(reap.called)
                # Exercise the actual lifecycle fixture's deletion gate.
                temporary = SimpleNamespace(cleanup=Mock(), _finalizer=SimpleNamespace(detach=Mock()))
                fixture = SimpleNamespace(process_cleanup=cleanup, temporary=temporary)
                OrcaSuspendLifecycleTests.cleanup_directory(fixture)
                temporary.cleanup.assert_not_called()
                temporary._finalizer.detach.assert_called_once_with()
                self.assertEqual(json.loads(evidence.read_text()), {'owner': record})
