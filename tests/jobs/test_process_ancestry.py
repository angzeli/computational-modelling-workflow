"""Suspension membership from raw start-token and parent-edge evidence."""
import errno
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import psutil

from cmw.jobs import process_identity as native


class ProcessAncestryTests(unittest.TestCase):
    def test_native_tree_survives_changed_wall_birth_and_boot_readings(self):
        with subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import sys; sys.stdin.read()'],
                              stdin=subprocess.PIPE) as child:
            try:
                with patch.object(psutil, 'boot_time', side_effect=[1793633892., 1793662684.]) as boot, \
                     patch.object(psutil.Process, 'create_time', side_effect=[1793654971.378596, 1793683763.378596]) as birth:
                    self.assertIn(child.pid, native.descendant_pids(os.getpid()))
                    self.assertEqual(native.ancestor_pids(child.pid, os.getpid()), (os.getpid(),))
                    boot.assert_not_called()
                    birth.assert_not_called()
            finally:
                child.communicate(input=b'', timeout=10)

    def test_reused_parent_or_changed_edge_refuses_the_walk(self):
        for parents, observations in [([10, 11], [(20, False), (10, False)]),
                                     ([10, 10], [(20, False), (10, False), (20, False), (11, False)])]:
            with self.subTest(parents=parents), patch.object(native, '_parent', side_effect=parents), \
                 patch.object(native, 'process_evidence', side_effect=observations):
                with self.assertRaises(OSError) as error:
                    native.ancestor_pids(20, 10)
                self.assertEqual(error.exception.errno, errno.EAGAIN)

    def test_raw_child_census_denial_cannot_be_returned_as_empty(self):
        with patch.object(native, 'process_evidence', return_value=(10, False)), \
             patch.object(native, '_children', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                native.descendant_pids(10)
