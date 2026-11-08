"""Versioned runner interpreter pins and explicit launch environments."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import stat

from .runtime_observation import _valid_epoch, file_epoch


def profile_version(profile):
    if not isinstance(profile, dict):
        raise ValueError('Runner profile must be an object')
    if 'schema' not in profile:
        if 'runner_interpreter' in profile:
            raise ValueError('Runner interpreter pin requires profile schema 2')
        return 1
    if type(profile['schema']) is not int or profile['schema'] != 2:
        raise ValueError('Unsupported runner profile schema')
    return 2


def pin_interpreter(path):
    """Bind bytes and complete security metadata without executing the interpreter."""
    from .identity import _file_identity
    if not isinstance(path, str) or not Path(path).is_absolute() or '\0' in path:
        raise ValueError('Runner interpreter must be an absolute path')
    resolved = Path(path).resolve(strict=True)
    before = list(file_epoch(resolved))
    if before[-1] is not True or not os.access(path, os.R_OK | os.X_OK):
        raise ValueError('Runner interpreter needs complete evidence and executable permission')
    value = {**_file_identity(path), 'epoch': list(file_epoch(resolved))}
    if value['epoch'] != before or Path(path).resolve(strict=True) != resolved:
        raise ValueError('Runner interpreter changed while pinning')
    validate_interpreter(value)
    return value


def validate_interpreter(value):
    from .identity import _validate_file
    fields = {'path', 'resolved_path', 'identity', 'bytes', 'sha256', 'epoch'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Malformed runner interpreter pin')
    _validate_file({key: item for key, item in value.items() if key != 'epoch'})
    epoch = value['epoch']
    if (not _valid_epoch(epoch, 12) or epoch[-1] is not True
            or epoch[:4] != value['identity'] or not stat.S_ISREG(epoch[5])
            or not epoch[5] & 0o111):
        raise ValueError('Malformed or incomplete runner interpreter evidence')
    return value


def interpreter_observation(profile, *, verify=False):
    """Report legacy absence explicitly; only an existing pin may mismatch."""
    if profile_version(profile) == 1:
        return {'status': 'unpinned', 'path': None, 'error': None}
    value = validate_interpreter(profile.get('runner_interpreter'))
    result = {'status': 'pinned', 'path': value['path'], 'error': None}
    if verify:
        try:
            if pin_interpreter(value['path']) != value:
                raise ValueError('Pinned runner interpreter identity changed')
        except (OSError, ValueError) as exc:
            result.update(status='mismatch', error=str(exc))
    return result


def runner_environment(profile):
    """Require a current profile pin; never inherit another Python environment."""
    observed = interpreter_observation(profile, verify=True)
    if observed['status'] != 'pinned':
        raise ValueError('Runner execution requires a verified schema-2 interpreter pin: ' +
                         (observed['error'] or observed['status']))
    return {'CMW_MANAGED_PYTHON': observed['path'], 'PYTHON_BIN': observed['path'],
            'PYTHONPATH': '', 'PYTHONHOME': ''}


def profile_capabilities(profiles, readiness=None):
    """Preserve the legacy profile shape beside explicitly versioned records."""
    legacy = {alias: {key: deepcopy(value) for key, value in profile.items()
                      if key not in {'schema', 'runner_interpreter'}}
              for alias, profile in profiles.items()}
    interpreters = {}
    for alias, profile in profiles.items():
        observed = (readiness or {}).get(alias, {}).get('runner_interpreter')
        observed = interpreter_observation(profile) if observed is None else deepcopy(observed)
        interpreters[alias] = {**observed, 'profile_schema': profile_version(profile),
                               'pin': deepcopy(profile.get('runner_interpreter'))}
    return {'profiles': legacy, 'runner_profiles': {'schema': 2, 'interpreters': interpreters}}
