"""Read-only observations of this interpreter's launch and runtime cache policy."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


def _launch_flags(argv):
    """Disclose only explicit -I/-B interpreter switches, never program arguments."""
    switches = set()
    arguments = iter(argv[1:])
    for argument in arguments:
        if not argument.startswith('-') or argument in ('-', '--'):
            break
        if argument == '--check-hash-based-pycs':
            next(arguments, None)
            continue
        if argument.startswith('--'):
            continue
        for index, flag in enumerate(argument[1:], start=1):
            if flag in 'mc':
                return sorted(switches)
            if flag in 'WX':
                if index == len(argument) - 1:
                    next(arguments, None)
                break
            if flag in 'BI':
                switches.add('-' + flag)
    return sorted(switches)


def _cache_access(package):
    """Probe effective access as the tree owner, including potential new caches."""
    def failed(error):
        raise error

    try:
        if (package.stat().st_uid != os.geteuid() or
                (os.getuid() != os.geteuid() and os.access not in os.supports_effective_ids)):
            return 'UNKNOWN'
        entries = 0
        locations = set()
        for parent, directories, files in os.walk(package, onerror=failed, followlinks=False):
            entries += len(directories) + len(files)
            if entries > 8192:
                return 'UNKNOWN'
            directory = Path(parent)
            if any((directory / name).is_symlink() for name in directories):
                return 'UNKNOWN'
            if directory.name == '__pycache__' or any(name.endswith('.py') for name in files):
                locations.add(directory)
            for name in files:
                if name.endswith(('.pyc', '.pyo')):
                    locations.update((directory, directory / name))
        if not locations:
            return 'UNKNOWN'
        for path in locations:
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                return 'UNKNOWN'
            if getattr(info, 'st_flags', 0) & (getattr(stat, 'UF_IMMUTABLE', 0) | getattr(stat, 'SF_IMMUTABLE', 0)):
                continue
            mode = os.W_OK | (os.X_OK if stat.S_ISDIR(info.st_mode) else 0)
            if os.access(path, mode, effective_ids=os.access in os.supports_effective_ids):
                return 'WRITABLE'
        return 'READ_ONLY'
    except OSError:
        return 'UNKNOWN'


def observer_policy(package):
    return {'isolated': sys.flags.isolated, 'startup_dont_write_bytecode': sys.flags.dont_write_bytecode,
            'dont_write_bytecode': sys.dont_write_bytecode,
            'launch_flags': _launch_flags(sys.orig_argv), 'owner_cache_access': _cache_access(package)}
