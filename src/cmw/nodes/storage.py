"""Read-only identity checks for explicitly enrolled local calculation storage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from uuid import uuid4
import re


_LOCAL_FILESYSTEMS = frozenset({'apfs', 'hfs', 'hfsplus', 'ext2', 'ext3', 'ext4',
                              'xfs', 'btrfs', 'zfs', 'ufs', 'exfat', 'msdos', 'vfat'})
_STORAGE_FIELDS = {'schema', 'storage_id', 'root', 'root_identity', 'mount_point',
                   'volume_uuid', 'filesystem', 'device', 'local'}


def _canonical_directory(value):
    if not isinstance(value, (str, Path)):
        raise ValueError('Storage root must be an absolute directory')
    path = Path(value)
    if not path.is_absolute() or path != path.resolve(strict=True) or not path.is_dir():
        raise ValueError('Storage root must exist and be canonical without symlinks')
    return path


def platform_volume(root):
    """Observe an exact local volume; unknown/network filesystems fail closed."""
    if sys.platform == 'darwin':
        located = subprocess.run(['/bin/df', '-P', str(root)], check=True, capture_output=True, timeout=5)
        if len(located.stdout) > 64 * 1024:
            raise ValueError('Mount evidence exceeds 64 KiB')
        rows = located.stdout.decode('utf-8').splitlines()
        if len(rows) != 2 or not rows[1].split():
            raise ValueError('Expected one mounted device for the storage root')
        device = rows[1].split()[0]
        if re.fullmatch(r'/dev/disk[0-9]+(?:s[0-9]+)*', device) is None:
            raise ValueError('Storage root is not on a directly attached local disk')
        result = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', device],
                                check=True, capture_output=True, timeout=5)
        if len(result.stdout) > 256 * 1024:
            raise ValueError('Volume evidence exceeds 256 KiB')
        value = plistlib.loads(result.stdout)
        return {'mount_point': value.get('MountPoint'), 'volume_uuid': value.get('VolumeUUID'),
                'filesystem': value.get('FilesystemType'), 'device': value.get('DeviceIdentifier'),
                'local': not bool(value.get('NetworkVolume', False)), 'mounted': bool(value.get('MountPoint')) and value.get('Mounted', True) is True}
    if sys.platform.startswith('linux'):
        executable = shutil.which('findmnt')
        if executable is None:
            raise ValueError('Local volume UUID discovery requires findmnt')
        result = subprocess.run([executable, '--json', '--target', str(root), '--output',
                                 'TARGET,SOURCE,FSTYPE,UUID'], check=True, capture_output=True, timeout=5)
        if len(result.stdout) > 256 * 1024:
            raise ValueError('Volume evidence exceeds 256 KiB')
        rows = json.loads(result.stdout).get('filesystems', [])
        if len(rows) != 1:
            raise ValueError('Expected one exact mounted filesystem')
        value = rows[0]
        return {'mount_point': value.get('target'), 'volume_uuid': value.get('uuid'),
                'filesystem': value.get('fstype'), 'device': value.get('source'),
                'local': isinstance(value.get('source'), str) and value['source'].startswith('/dev/'), 'mounted': True}
    raise ValueError('Local volume identity is supported on macOS and Linux only')


def _observe(root, probe):
    root = _canonical_directory(root)
    before = root.stat()
    evidence = (platform_volume if probe is None else probe)(root)
    if not isinstance(evidence, dict) or set(evidence) != {
            'mount_point', 'volume_uuid', 'filesystem', 'device', 'local', 'mounted'}:
        raise ValueError('Malformed volume identity evidence')
    for key in ('mount_point', 'volume_uuid', 'filesystem', 'device'):
        value = evidence[key]
        if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
            raise ValueError(f'Volume {key} is unavailable or invalid')
    filesystem = evidence['filesystem'].lower()
    if evidence['local'] is not True or filesystem not in _LOCAL_FILESYSTEMS:
        raise ValueError('Active calculation storage must be a supported local filesystem')
    mount = _canonical_directory(evidence['mount_point'])
    if evidence['mounted'] is not True or mount.stat().st_dev != before.st_dev:
        raise ValueError('Registered volume is not currently mounted at its declared mount')
    # APFS firmlinks can expose the internal Data volume outside its mount path.
    # External /Volumes roots must still be lexically below their exact mount.
    if root.parts[1:2] == ('Volumes',) and not root.is_relative_to(mount):
        raise ValueError('External storage root is outside its exact volume mount')
    after = _canonical_directory(root).stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError('Storage root changed during observation')
    evidence = {key: value for key, value in evidence.items() if key != 'mounted'}
    return {'root': str(root), 'root_identity': [after.st_dev, after.st_ino],
            **evidence, 'mount_point': str(mount), 'filesystem': filesystem}


def identify_storage(root, *, probe=None):
    """Create enrollment evidence without creating a root, mount or directory."""
    return {'schema': 1, 'storage_id': uuid4().hex, **_observe(root, probe)}


def validate_storage(identity):
    if not isinstance(identity, dict) or set(identity) != _STORAGE_FIELDS:
        raise ValueError('Malformed storage enrollment')
    if type(identity['schema']) is not int or identity['schema'] != 1:
        raise ValueError('Unsupported storage enrollment schema')
    if not isinstance(identity['storage_id'], str) or re.fullmatch(r'[0-9a-f]{32}', identity['storage_id']) is None:
        raise ValueError('Storage identity must be a lowercase 32-hex UUID')
    root_identity = identity['root_identity']
    if not isinstance(root_identity, list) or len(root_identity) != 2 or any(
            type(value) is not int or value < 0 for value in root_identity):
        raise ValueError('Malformed storage directory identity')
    for key in ('root', 'mount_point', 'volume_uuid', 'filesystem', 'device'):
        value = identity[key]
        if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
            raise ValueError(f'Malformed storage {key}')
    if not Path(identity['root']).is_absolute() or not Path(identity['mount_point']).is_absolute():
        raise ValueError('Storage paths must be absolute')
    if identity['local'] is not True or identity['filesystem'] not in _LOCAL_FILESYSTEMS:
        raise ValueError('Storage enrollment must identify a supported local filesystem')
    return identity


def check_storage(identity, *, probe=None):
    """Report missing/replaced storage without repairing it or creating paths."""
    try:
        validate_storage(identity)
        observed = _observe(identity['root'], probe)
        expected = {key: identity[key] for key in observed}
        if observed != expected:
            raise ValueError('Enrolled storage identity changed; explicit review is required')
        return {'ready': True, 'identity': identity, 'error': None}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        return {'ready': False, 'identity': None, 'error': str(exc)}
