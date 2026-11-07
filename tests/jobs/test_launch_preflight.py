"""PATH lookup preserves candidate search without hiding explicit read failures."""
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.runtime import launch_blocker


class LaunchPreflightTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='cmw-preflight-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.denied = self.root / 'denied' / 'command'
        self.executable = self.root / 'command'
        self.executable.write_text('#!/bin/sh\nexit 0\n')
        self.executable.chmod(0o700)
        self.job = {'cwd': str(self.root), 'argv': ['command'],
                    'env': {'PATH': f'{self.denied.parent}:{self.root}'}}
        original_stat = Path.stat

        def probe(path, *args, **kwargs):
            if path == self.denied:
                raise PermissionError(errno.EACCES, 'inaccessible search directory', str(path))
            return original_stat(path, *args, **kwargs)

        self.enterContext(patch.object(Path, 'stat', probe))

    def test_inaccessible_path_entry_does_not_mask_later_executable(self):
        self.assertEqual(launch_blocker(self.job), '')

    def test_inaccessible_path_entry_without_match_reports_not_found(self):
        self.job['env']['PATH'] = str(self.denied.parent)
        self.assertEqual(launch_blocker(self.job), 'Executable not found: command')

    def test_explicit_path_read_denial_remains_unavailable(self):
        self.job['argv'] = [str(self.denied)]
        with self.assertRaises(PermissionError):
            launch_blocker(self.job)
