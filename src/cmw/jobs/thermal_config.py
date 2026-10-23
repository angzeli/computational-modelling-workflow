"""Explicit, literal thermal settings; no automatic global configuration loading."""
from __future__ import annotations

import math
import os
from pathlib import Path
import stat


MAX_SETTINGS_BYTES = 4096
_KEYS = frozenset({'CMW_THERMAL_MACMON', 'CMW_THERMAL_INTERVAL_SECONDS'})


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, info.st_mode)


def read_settings(path):
    """Read an optional private file of two reviewed literal KEY=value settings.

    The caller explicitly selects the file; values are never shell-evaluated or
    merged with ambient environment. Missing settings leave temperature disabled.
    """
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Thermal settings path must be absolute')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    parent = path.parent
    parent_info = parent.stat()
    if (parent != parent.resolve() or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077):
        raise ValueError('Thermal settings directory must be canonical and owner-only')
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_SETTINGS_BYTES):
        raise ValueError('Thermal settings must be an owner-only regular file of at most 4096 bytes')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        if _identity(opened) != _identity(info):
            raise ValueError('Thermal settings changed while opening')
        raw = handle.read(MAX_SETTINGS_BYTES + 1)
        if _identity(os.fstat(handle.fileno())) != _identity(opened):
            raise ValueError('Thermal settings changed while reading')
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValueError('Thermal settings exceed 4096 bytes')
    settings = {}
    for line in raw.decode('utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if (not separator or key not in _KEYS or key in settings or not value
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError('Thermal settings require unique reviewed literal KEY=value entries')
        if key == 'CMW_THERMAL_MACMON':
            if not Path(value).is_absolute():
                raise ValueError('Configured macmon path must be absolute and unquoted')
        else:
            interval = float(value)
            if not math.isfinite(interval) or not 15 <= interval <= 30:
                raise ValueError('CPU temperature interval must be 15 to 30 seconds')
        settings[key] = value
    return settings
