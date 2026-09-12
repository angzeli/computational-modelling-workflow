"""POSIX state privacy without changing permissions on existing user directories."""
import argparse
from contextlib import contextmanager, redirect_stdout
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs import runtime
from cmw.jobs.cli import register
from cmw.jobs.store import JobsError, Store
from tests.jobs.isolated_runtime import install
from tests.jobs.test_jobs import wait_for


@contextmanager
def umask(value):
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


class StatePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-private-state-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.addCleanup(install().close)

    def add(self):
        output = self.root/f'payload-{self.store.root.name}'
        code = f"from pathlib import Path; Path({str(output)!r}).write_text('dummy output')"
        return self.store.add(argv=[sys.executable, '-c', code],
                              cwd=self.root, name='private fixture', env={'DUMMY_SECRET': 'sentinel-not-for-diagnostics'})

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_existing_permissive_root_refuses_mutations_without_creating_files(self):
        self.store.root.mkdir(mode=0o755)
        self.store.root.chmod(0o755)
        for operation in (self.add, lambda: runtime.start(self.store), lambda: runtime.controller(self.store),
                          lambda: self.store.dispatch(True)):
            with self.subTest(operation=operation), patch.object(runtime, 'detached', side_effect=AssertionError('unsafe launch')):
                with self.assertRaises(JobsError) as raised:
                    operation()
                message = str(raised.exception)
                for expected in (str(self.store.root), '0755', 'argv', 'env', 'logs', 'private'):
                    self.assertIn(expected, message)
                self.assertNotIn('sentinel-not-for-diagnostics', message)
                self.assertEqual(list(self.store.root.iterdir()), [])
                self.assertEqual(self.mode(self.store.root), 0o755)

    def test_default_state_is_created_private(self):
        env = {k: v for k, v in os.environ.items() if k != 'CMW_JOBS_STATE'}
        env['XDG_STATE_HOME'] = str(self.root/'xdg')
        with patch.dict(os.environ, env, clear=True), patch.object(Path, 'home', return_value=self.root), umask(0):
            store = Store()
            self.assertTrue(store.root.is_relative_to(self.root))
            with store.transaction():
                pass
        self.assertEqual(self.mode(store.root), 0o700)
        self.assertEqual(self.mode(store.path), 0o600)

    def test_existing_private_root_proceeds_without_chmod(self):
        self.store.root.mkdir(mode=0o700)
        with patch.object(Path, 'chmod', side_effect=AssertionError('no implicit chmod')):
            self.add()
        self.assertEqual(self.mode(self.store.root), 0o700)

    def test_foreign_owner_refused(self):
        self.store.root.mkdir(mode=0o700)
        with patch('os.geteuid', return_value=os.geteuid()+1):
            with self.assertRaisesRegex(JobsError, 'owner'):
                self.add()
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_status_does_not_create_or_chmod_state(self):
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers())
        args = parser.parse_args(['jobs', '--state', str(self.store.root), 'status', '--json'])
        for exists in (False, True):
            if exists:
                self.store.root.mkdir(mode=0o755)
                self.store.root.chmod(0o755)
            with self.subTest(exists=exists), redirect_stdout(io.StringIO()):
                self.assertEqual(args.handler(args), 0)
            self.assertEqual(self.store.root.exists(), exists)
            if exists:
                self.assertEqual(self.mode(self.store.root), 0o755)
                self.assertEqual(list(self.store.root.iterdir()), [])

    def test_existing_database_observation_is_read_only_on_permissive_root(self):
        self.add()
        before = self.store.path.read_bytes()
        names = sorted(path.name for path in self.store.root.iterdir())
        self.store.root.chmod(0o755)
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.store.root.iterdir()), names)
        self.assertEqual(self.mode(self.store.root), 0o755)

    def test_new_state_and_runtime_files_are_private_under_both_umasks(self):
        for mask in (0, 0o077):
            with self.subTest(umask=mask), umask(mask):
                self.store = Store(self.root/f'state-{mask}')
                self.add()
                self.assertEqual(self.mode(self.store.root), 0o700)
                self.assertEqual(self.mode(self.store.path), 0o600)
                with self.store.transaction() as con:
                    self.store.event(con, None, 'dummy journal evidence')
                    self.assertEqual(self.mode(Path(str(self.store.path)+'-journal')), 0o600)
                try:
                    runtime.start(self.store)
                    wait_for(lambda: self.store.snapshot()['jobs'][0]['status'] == 'Done')
                finally:
                    controller = self.store.snapshot()['controller'].get('owner')
                    runtime.stop(self.store)
                    wait_for(lambda: not self.store.snapshot()['controller']['online'])
                    for child in runtime._CHILDREN:
                        if controller and child.pid == controller['pid']:
                            child.wait(timeout=5)
                    runtime.reap_detached()
                self.assertEqual(self.mode(self.root/f'payload-{self.store.root.name}'), 0o666 & ~mask)
                names = {path.name for path in self.store.root.rglob('*') if path.is_file()}
                self.assertTrue({'queue.sqlite3', 'stdout.log', 'stderr.log', 'controller.log',
                                 'payload-exit.json', 'worker.lock', 'start.lock', 'controller.lock'} <= names)
                for path in self.store.root.rglob('*'):
                    if path.is_file():
                        self.assertEqual(self.mode(path), 0o600, str(path))
