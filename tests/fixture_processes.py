"""Evidence for explicitly registered fixture children, never a process census.

Registration runs before a fixture can hang. Timeout reporting is performed by
the outer shell after the native watchdog exits, so it needs no Python callback
in the stuck interpreter. Records are observations, not permission to signal:
post-run cleanup must re-establish the same v2 identity first.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


REGISTRY = 'fixture-processes.jsonl'
_Popen = subprocess.Popen


def registry_path(directory):
    return Path(directory) / REGISTRY


def _append_record(directory, record):
    directory = Path(directory)
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError('Fixture evidence directory must be canonical and absolute')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('Fixture evidence directory must be private and owned')
    payload = (json.dumps(record, separators=(',', ':')) + '\n').encode()
    if len(payload) > 4096:
        raise ValueError('Fixture identity record is too large')
    descriptor = os.open(registry_path(directory),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('Fixture registry must be a private owned regular file')
        if os.write(descriptor, payload) != len(payload):
            raise OSError('Incomplete fixture identity write')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def register_process(process, *, role):
    """Record this actual direct child; missing evidence enables no discovery."""
    directory = os.environ.get('CMW_TEST_EVIDENCE_DIR')
    if not directory:
        return None
    try:
        if not isinstance(process, _Popen):
            raise ValueError('Registration requires an actual fixture Popen handle')
        # Reuse the native reader, not psutil's adjusted birth timestamp. The
        # handle pins the unreaped child; a completed child needs no cleanup.
        if process.poll() is not None:
            return None
        from cmw.jobs.process_identity import capture
        owner = capture(process.pid)
        record = {'test': os.environ.get('CMW_TEST_ACTIVE_TEST'), 'role': role,
                  'registered_by': os.getpid(), 'pid': process.pid, 'identity': owner,
                  'registration_complete': True}
        _append_record(directory, record)
        print('CMW_TEST_CASE_PROCESS_REGISTERED ' + json.dumps(record), flush=True)
        return record
    except Exception as exc:
        # Capturing evidence must not bypass the fixture's existing cleanup.
        record = {
            'test': os.environ.get('CMW_TEST_ACTIVE_TEST'),
            'pid': getattr(process, 'pid', None), 'role': role, 'identity': None,
            'registration_complete': False, 'reason': str(exc)}
        try:
            _append_record(directory, record)
        except Exception as recording:
            record['recording_error'] = str(recording)
        print('CMW_TEST_CASE_PROCESS_REGISTRATION_INCOMPLETE ' + json.dumps(record),
            file=sys.stderr, flush=True)
        return None


def read_records(directory):
    """Read only the explicit ledger; reject partial records instead of guessing."""
    path = registry_path(directory)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('Fixture registry must be a private owned regular file')
        records = []
        for line in source:
            if not line.endswith('\n'):
                raise ValueError('Incomplete fixture identity record')
            record = json.loads(line)
            if (isinstance(record, dict) and record.get('registration_complete') is False
                    and record.get('identity') is None):
                records.append(record)
                continue
            if (not isinstance(record, dict) or not isinstance(record.get('identity'), dict)
                    or record['identity'].get('version') != 2
                    or record.get('pid') != record['identity'].get('pid')):
                raise ValueError('Invalid fixture identity record')
            records.append(record)
        return records


def audit_processes(directory):
    """Read-only local audit; unresolved records are never reported as clean.

    Separately authorized cleanup can reuse tests.runtime.owned_process_cleanup
    with these recorded identities. This helper itself never sends a signal.
    """
    from cmw.jobs.ownership import OwnershipStatus, observe_owner, valid_owner_record
    results = []
    seen = set()
    for record in read_records(directory):
        owner = record['identity']
        if owner is None:
            results.append({**record, 'status': 'UNVERIFIABLE',
                            'deciding_check': 'registration', 'signal_sent': False})
            continue
        key = json.dumps(owner, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        if not valid_owner_record(owner):
            raise ValueError('Invalid v2 fixture identity')
        try:
            observed = observe_owner(owner)
            result = {**record, 'status': str(observed.status),
                      'deciding_check': observed.deciding_check, 'signal_sent': False}
        except Exception as exc:
            result = {**record, 'status': str(OwnershipStatus.UNVERIFIABLE),
                      'deciding_check': 'observation', 'reason': str(exc), 'signal_sent': False}
        results.append(result)
    return results
