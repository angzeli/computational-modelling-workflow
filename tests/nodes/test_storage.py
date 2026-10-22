"""Storage identity checks use local disposable roots and injected volume evidence."""
import copy
import os
import plistlib
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.nodes.storage import check_storage, identify_storage, validate_storage, platform_volume


def volume(root):
    mount = root
    while mount.parent != mount and not os.path.ismount(mount):
        mount = mount.parent
    return {'mount_point': str(mount), 'volume_uuid': 'fixture-volume-uuid',
            'filesystem': 'apfs', 'device': 'fixture-local-device', 'local': True, 'mounted': True}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_enroll_and_check_do_not_create_files(self):
        record = identify_storage(self.root, probe=volume)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(check_storage(record, probe=volume)['ready'])
        self.assertRegex(record['storage_id'], r'^[0-9a-f]{32}$')

    def test_replaced_same_named_volume_refused(self):
        record = identify_storage(self.root, probe=volume)
        observed = lambda root: {**volume(root), 'volume_uuid': 'replacement'}
        self.assertFalse(check_storage(record, probe=observed)['ready'])

    def test_replaced_root_refused(self):
        root = self.root / 'calculations'
        root.mkdir()
        record = identify_storage(root, probe=volume)
        root.rename(self.root / 'old')
        root.mkdir()
        self.assertFalse(check_storage(record, probe=volume)['ready'])

    def test_missing_root_refused_without_mkdir(self):
        root = self.root / 'calculations'
        root.mkdir()
        record = identify_storage(root, probe=volume)
        root.rmdir()
        self.assertFalse(check_storage(record, probe=volume)['ready'])
        self.assertFalse(root.exists())

    def test_symlink_and_network_storage_refused(self):
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            identify_storage(link, probe=volume)
        for filesystem in ('smbfs', 'nfs', 'sshfs', 'unknown'):
            with self.subTest(filesystem=filesystem), self.assertRaises(ValueError):
                identify_storage(self.root, probe=lambda path: {**volume(path), 'filesystem': filesystem})
        with self.assertRaises(ValueError):
            identify_storage(self.root, probe=lambda path: {**volume(path), 'local': False})

    def test_missing_uuid_mount_and_malformed_schema_refused(self):
        with self.assertRaises(ValueError):
            identify_storage(self.root, probe=lambda path: {**volume(path), 'volume_uuid': None})
        with patch('cmw.nodes.storage.os.path.ismount', return_value=False):
            with self.assertRaises(ValueError):
                identify_storage(self.root, probe=lambda path: {**volume(path), 'mount_point': '/', 'mounted': False})
        record = identify_storage(self.root, probe=volume)
        for change in ({'schema': True}, {'storage_id': None}, {'local': 'yes'},
                       {'root_identity': [True, 1]}, {'unexpected': 'field'}):
            broken = {**copy.deepcopy(record), **change}
            self.assertFalse(check_storage(broken, probe=volume)['ready'])
            with self.assertRaises(ValueError):
                validate_storage(broken)

    def test_darwin_resolves_directory_to_exact_device_before_diskutil(self):
        disk = {'MountPoint': '/System/Volumes/Data', 'VolumeUUID': 'fixture-volume',
                'FilesystemType': 'apfs', 'DeviceIdentifier': 'disk3s5'}
        results = [SimpleNamespace(stdout=b'Filesystem Blocks Used Available Capacity Mounted on\n/dev/disk3s5 100 40 60 40% /System/Volumes/Data\n'),
                   SimpleNamespace(stdout=plistlib.dumps(disk))]
        with patch('cmw.nodes.storage.sys.platform', 'darwin'), patch(
                'cmw.nodes.storage.subprocess.run', side_effect=results) as run:
            value = platform_volume(self.root)
            self.assertEqual(value['volume_uuid'], 'fixture-volume')
            self.assertEqual(run.call_args_list[0].args[0], ['/bin/df', '-P', str(self.root)])
            self.assertEqual(run.call_args_list[1].args[0][-1], '/dev/disk3s5')
