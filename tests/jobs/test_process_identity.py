import errno
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from cmw.jobs import process_identity as native


class KernelIdentityTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'native kernel')
    def test_current_process_identity_is_repeatable_and_independent_of_psutil(self):
        import psutil
        first = native.capture()
        with patch.object(psutil, 'boot_time', return_value=1793662684.0), \
                patch.object(psutil, 'Process', side_effect=AssertionError('adjusted psutil birth')), \
                patch.object(psutil._psplatform, 'INIT_BOOT_TIME', 1793633892.0, create=True):
            self.assertEqual(first, native.capture())
        self.assertEqual(first['version'], 2)
        self.assertEqual(first['pid'], os.getpid())
        self.assertEqual(str(UUID(first['boot_session'])), first['boot_session'])
        self.assertIs(type(first['start_token']), int)
        self.assertGreater(first['start_token'], 0)

    def test_malformed_kernel_boot_uuid_is_unavailable_on_both_platforms(self):
        def sysctlbyname(name, output, size, new, new_size):
            output.value = b'bad\xff'
            return 0

        for platform in ('linux', 'darwin'):
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform), \
                    patch.object(Path, 'read_bytes', return_value=b'bad\xff\n'), \
                    patch.object(native, '_darwin_sysctls', return_value=Mock(sysctlbyname=sysctlbyname)):
                with self.assertRaises(OSError) as caught:
                    native.boot_session_id()
                self.assertEqual(caught.exception.errno, errno.EIO)

    def test_linux_stat_uses_field_22_after_last_closing_parenthesis(self):
        record = b'42 (thread ) \xff\n(3) ) Z ' + b'0 ' * 18 + b'287916 0 0\n'
        with patch.object(Path, 'read_bytes', return_value=record):
            self.assertEqual(native._linux_process(42), (287916, True))

    def test_missing_proc_entry_with_visible_pid_is_not_evidence_of_absence(self):
        with patch.object(Path, 'read_bytes', side_effect=FileNotFoundError), \
                patch.object(os, 'getpgid', return_value=42):
            with self.assertRaises(PermissionError):
                native._linux_process(42)

    def test_missing_proc_entry_with_kernel_esrch_confirms_absence(self):
        with patch.object(Path, 'read_bytes', side_effect=FileNotFoundError), \
                patch.object(os, 'getpgid', side_effect=ProcessLookupError(errno.ESRCH, 'absent')):
            with self.assertRaises(ProcessLookupError):
                native._linux_process(42)
