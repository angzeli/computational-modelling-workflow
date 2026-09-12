"""Bounded, test-only lifecycle evidence captured before fixture teardown."""
from contextlib import ExitStack, contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

import psutil

from cmw.jobs.ownership import identity, lock_held, owner_alive


def owned_members(state, root, *, pgid=None):
    groups = {}
    for job in state['jobs']:
        group = job.get('group')
        if not group or (pgid is not None and group['pid'] != pgid):
            continue
        try:
            if identity(group['pid']) != group:
                continue
        except psutil.NoSuchProcess:
            try:
                guardian = owner_alive(job.get('worker')) and lock_held(
                    root/'attempts'/job['attempt_id']/'worker.lock', strict=True)
            except OSError:
                continue
            if not guardian:
                continue
        except (psutil.Error, OSError):
            continue
        groups[group['pid']] = job.get('env', {}).get('CMW_JOBS_OWN_SESSION') == '1'
    records = []
    if not groups:
        return records
    for pid in psutil.pids():
        try:
            pgid, sid = os.getpgid(pid), os.getsid(pid)
            if pgid not in groups and not groups.get(sid):
                continue
            owner = identity(pid)
            process = psutil.Process(pid)
            status = process.status()
            if identity(pid) == owner:
                records.append({'identity': owner, 'status': status, 'ppid': process.ppid(),
                                'pgid': pgid, 'sid': sid, 'live': owner_alive(owner)})
        except (psutil.Error, ProcessLookupError, PermissionError):
            continue
    return records


def bounded_tail(path, limit=16384):
    with path.open('rb') as handle:
        handle.seek(max(0, path.stat().st_size-limit))
        return handle.read(limit).decode(errors='replace')


def capture(store, destination, *, outcome, started_at):
    """Copy only this isolated state; preserve SQLite consistency and bounded tails."""
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = store.snapshot()
    metadata = {'started_at': started_at, 'captured_at': time.time(), 'outcome': outcome,
                'snapshot': state, 'members': owned_members(state, store.root),
                'controller_lock': lock_held(store.root/'controller.lock'),
                'worker_locks': {}, 'receipts': {}, 'log_tails': {}}
    (destination/'state').mkdir(mode=0o700)
    if store.path.exists():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro', uri=True) as source:
            with sqlite3.connect(destination/'state'/store.path.name) as target:
                source.backup(target)
    for job in state['jobs']:
        attempt = store.root/'attempts'/job['attempt_id']
        metadata['worker_locks'][job['attempt_id']] = lock_held(attempt/'worker.lock')
        receipt = attempt/'payload-exit.json'
        metadata['receipts'][job['attempt_id']] = bounded_tail(receipt) if receipt.exists() else None
    for path in [store.root/'controller.log', *store.root.glob('attempts/*/*.log'),
                 *store.root.glob('lifecycle-trace-*.jsonl')]:
        if not path.is_file():
            continue
        tail = bounded_tail(path, 65536 if path.suffix == '.jsonl' else 16384)
        target = destination/'state'/path.relative_to(store.root)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(tail)
        if path.suffix == '.log':
            metadata['log_tails'][str(path.relative_to(store.root))] = tail
    (destination/'capture.json').write_text(json.dumps(metadata, indent=2)+'\n')
    return metadata


def retain_lifecycle_evidence(method):
    @wraps(method)
    def run(test, *args, **kwargs):
        started = time.time()
        outcome = 'PASS'
        with patch.dict(os.environ, {'CMW_TEST_LIFECYCLE_TRACE': '1'}):
            try:
                return method(test, *args, **kwargs)
            except BaseException as exc:
                outcome = f'{type(exc).__name__}: {exc}'
                raise
            finally:
                evidence_root = os.environ.get('CMW_TEST_EVIDENCE_DIR')
                if outcome != 'PASS' or evidence_root:
                    try:
                        if evidence_root:
                            Path(evidence_root).mkdir(parents=True, exist_ok=True, mode=0o700)
                        destination = Path(tempfile.mkdtemp(prefix=method.__name__+'-', dir=evidence_root))
                        capture(test.store, destination, outcome=outcome, started_at=started)
                        print(f'Lifecycle evidence ({outcome}): {destination}', flush=True)
                    except Exception as exc:
                        print(f'Lifecycle evidence unavailable: {type(exc).__name__}', file=sys.stderr)
    return run


@contextmanager
def trace_runtime(root):
    """Instrument owned fixture workers, without changing production signal decisions."""
    from cmw.jobs import runtime
    if os.environ.get('CMW_TEST_LIFECYCLE_TRACE') != '1':
        yield
        return
    root = Path(root)
    path = root/f'lifecycle-trace-{os.getpid()}.jsonl'

    def record(event, **details):
        try:
            with path.open('a') as handle:
                handle.write(json.dumps({'time': time.time(), 'monotonic': time.monotonic(),
                                         'event': event, **details})+'\n')
        except OSError:
            pass  # Evidence failure must never prevent the underlying operation.

    def members(pgid):
        try:
            from cmw.jobs.store import Store
            return owned_members(Store(root).snapshot(), root, pgid=pgid)
        except Exception:
            return None

    killpg, exists, finish, unknown = os.killpg, runtime.group_exists, runtime.finish, runtime.mark_unknown
    previous = {}

    def signal_group(pgid, signum):
        if signum:
            record('signal-attempt', pgid=pgid, signal=signum)
        try:
            result = killpg(pgid, signum)
        except OSError as exc:
            record('signal-error', pgid=pgid, signal=signum, error=str(exc), errno=exc.errno,
                   members=members(pgid))
            raise
        if signum:
            record('signal-delivered', pgid=pgid, signal=signum)
        return result

    def group_exists(pgid):
        result = exists(pgid)
        if previous.get(pgid) != result:
            record('group-exists', pgid=pgid, exists=result)
            previous[pgid] = result
        return result

    def finished(store, job_id, claim, status, code, reason, **kwargs):
        record('finish', job_id=job_id, claim=claim, status=status, code=code, reason=reason, **kwargs)
        return finish(store, job_id, claim, status, code, reason, **kwargs)

    def uncertain(store, con, job, reason):
        record('unknown', job_id=job['id'], claim=job['claim'], reason=reason,
               members=members(job['group']['pid']) if job.get('group') else [])
        return unknown(store, con, job, reason)

    with ExitStack() as patches:
        patches.enter_context(patch.object(os, 'killpg', signal_group))
        patches.enter_context(patch.object(runtime, 'group_exists', group_exists))
        patches.enter_context(patch.object(runtime, 'finish', finished))
        patches.enter_context(patch.object(runtime, 'mark_unknown', uncertain))
        yield
