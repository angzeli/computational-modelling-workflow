"""Native ACL presence, positive absence and refusal evidence on disposable objects."""
import ctypes
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.nodes.native_security import _api, acl_evidence, SecurityReadError
from cmw.nodes.runtime_observation import file_epoch, inventory


@unittest.skipUnless(sys.platform == 'darwin', 'Darwin native filesec contract')
class NativeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-native-acl-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.file = self.root / 'member.py'
        self.file.write_text('value = 1\n')
        self.fd = os.open(self.file, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, self.fd)

    def test_absence_is_positive_descriptor_property_for_file_and_directory(self):
        self.assertEqual(acl_evidence(self.fd)['state'], 'absent')
        fd = os.open(self.root, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self.assertEqual(acl_evidence(fd)['state'], 'absent')
        finally:
            os.close(fd)
        root, members, dirs = inventory(self.root)
        self.assertTrue(root[-1])
        self.assertTrue(all(epoch[-1] for _, epoch in members))

    def test_shared_bindings_recollect_xattrs_on_each_observation(self):
        with patch('ctypes.CDLL', side_effect=AssertionError('unexpected rebinding')):
            before = file_epoch(self.file)
            subprocess.run(['/usr/bin/xattr', '-w', 'cmw.test', 'changed', str(self.file)],
                           check=True, capture_output=True)
            after = file_epoch(self.file)
        self.assertNotEqual(before[-2], after[-2])
        self.assertTrue(after[-1])

    def test_explicit_file_and_directory_acl_are_present_and_changes_differ(self):
        before = file_epoch(self.file)
        for path, rule in ((self.file, 'everyone allow read'),
                           (self.root, 'everyone allow list,search,file_inherit,directory_inherit')):
            subprocess.run(['/bin/chmod', '+a', rule, str(path)], check=True, capture_output=True)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                evidence = acl_evidence(fd)
                self.assertEqual(evidence['state'], 'present')
                self.assertEqual(len(evidence['sha256']), 64)
            finally:
                os.close(fd)
        after = file_epoch(self.file)
        self.assertNotEqual(before[-2], after[-2])
        self.assertTrue(after[-1])

    def test_native_api_error_is_not_absence_and_allocations_are_freed(self):
        real = _api()
        class Broken:
            freed = 0
            def __getattr__(self, name):
                return getattr(real, name)
            def filesec_free(self, obj):
                self.freed += 1
                real.filesec_free(obj)
        for api in ('fstatx_np', 'filesec_query_property', 'filesec_get_property'):
            for code, state in ((errno.EACCES, 'denied'), (errno.ENOTSUP, 'unsupported'),
                                (errno.EIO, 'error'), (errno.ENOENT, 'error')):
                with self.subTest(api=api, code=code):
                    if api == 'filesec_get_property':
                        subprocess.run(['/bin/chmod', '+a', 'everyone allow read', str(self.file)],
                                       check=True, capture_output=True)
                    broken = Broken()
                    def fail(*args):
                        ctypes.set_errno(code)
                        return -1
                    setattr(broken, api, fail)
                    with self.assertRaises(SecurityReadError) as caught:
                        acl_evidence(self.fd, libc=broken)
                    self.assertEqual((caught.exception.api, caught.exception.errno,
                                      caught.exception.state), (api, code, state))
                    self.assertEqual(broken.freed, 1)

    def test_changed_object_during_property_read_refuses(self):
        real = _api()
        class Racing:
            def __getattr__(self, name):
                return getattr(real, name)
            def filesec_query_property(self, *args):
                result = real.filesec_query_property(*args)
                self_file.write_text('value = 2\n')
                return result
        self_file = self.file
        with self.assertRaises(SecurityReadError) as caught:
            acl_evidence(self.fd, libc=Racing())
        self.assertEqual(caught.exception.state, 'changed-object')

    def test_denied_descriptor_open_retains_native_error_context(self):
        from cmw.nodes.runtime_observation import RuntimeRefusal
        self.file.chmod(0)
        try:
            with self.assertRaises(RuntimeRefusal) as caught:
                file_epoch(self.file)
            self.assertEqual(caught.exception.evidence['api'], 'open')
            self.assertEqual(caught.exception.evidence['errno'], errno.EACCES)
            self.assertEqual(caught.exception.evidence['security_state'], 'denied')
            self.assertEqual(caught.exception.evidence['object_type'], 'file')
        finally:
            self.file.chmod(0o600)
