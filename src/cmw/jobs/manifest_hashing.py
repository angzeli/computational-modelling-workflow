"""Asynchronous restart-output digests for native manifests (#41 Phase 2c, #65).

Runs detached (launched by manifest_hashes.sh, which owns the environment,
log descriptors and backgrounding) after a VASP attempt's terminal state is
published, so hashing multi-GiB WAVECAR/CHGCAR never blocks drainage, Done,
terminal publication or resource release. A file is hashed only if it is
unchanged (device, inode, size, mtime) across the read; otherwise, or on any
failure, its digest stays null with an evidence item and ``hashes_pending``
clears. The result is written through the state.json single writer.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys

STABLE_KEYS = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
CHUNK = 8 * 1024 * 1024


def launch(store, job_id):
    """Ask the shell entry point to detach the hasher; a refusal leaves the state pending for a retry."""
    from .runtime import runtime_environment
    script = Path(__file__).with_name('manifest_hashes.sh')
    try:
        completed = subprocess.run(
            ['/bin/bash', str(script), sys.executable, runtime_environment()['PYTHONPATH'], str(store.root),
             str(job_id), str(store.root / 'manifest-hashes.log')],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, cwd='/', start_new_session=True)
        return completed.returncode == 0 and json.loads(completed.stdout or '{}').get('status') == 'LAUNCHED'
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def stable_digest(path):
    """SHA-256 of a regular file that did not change while it was read, else (None, reason)."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        return None, 'is not a regular file'
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b''):
            digest.update(chunk)
    after = path.lstat()
    if any(getattr(before, key) != getattr(after, key) for key in STABLE_KEYS):
        return None, 'changed while it was hashed'
    return digest.hexdigest(), None


def hash_outputs(store, job_id):
    """Hash every pending restart output of the job's terminal state and record the result."""
    from .location_resolvers import vasp_runner_arguments
    from .manifest_state import MANIFEST_DIR, record_output_hashes
    with store.read_transaction() as con:
        job = store.get(con, job_id)
    state = json.loads((Path(job['cwd']) / MANIFEST_DIR / 'state.json').read_text(encoding='utf-8'))
    output = vasp_runner_arguments(job)[2]
    digests, findings = {}, []
    for role, digest in state['restart_outputs'].items():
        if digest is not None:
            continue
        try:
            value, problem = stable_digest(output / role)
        except OSError as exc:
            value, problem = None, f'is unreadable: {exc.strerror or exc}'
        if value is None:
            findings.append({'source': 'queue', 'marker': f'{role} {problem}; digest left null', 'line': None})
        else:
            digests[role] = value
    return record_output_hashes(store, job_id, digests, findings)


def main(argv=None):
    from .store import Store
    root, job_id = (sys.argv[1:] if argv is None else argv)
    hash_outputs(Store(root), int(job_id))
    return 0


if __name__ == '__main__':
    sys.exit(main())
