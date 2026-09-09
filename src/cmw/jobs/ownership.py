"""Kernel locks and birth identity; never identify ownership by engine name."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import socket

import psutil

from .store import JobsError


def identity(pid=None):
    process = psutil.Process(os.getpid() if pid is None else pid)
    return {"pid": process.pid, "birth": process.create_time(), "host": socket.gethostname(), "boot": psutil.boot_time()}


def owner_alive(owner):
    if not owner or owner.get("host") != socket.gethostname() or owner.get("boot") != psutil.boot_time():
        return False
    try:
        process = psutil.Process(owner["pid"])
        return process.create_time() == owner["birth"] and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, KeyError):
        return False


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JobsError(f"Already owned: {path.name}") from exc
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def lock_held(path, *, strict=False):
    if not path.exists():
        return False
    try:
        with path.open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        if strict:
            raise
        return True  # Inaccessible ownership must not authorize dispatch.


def group_members(pgid, *, pids=None, process_factory=psutil.Process):
    """Only inspect membership of the group we created. No engine-name discovery."""
    members = []
    for pid in psutil.pids() if pids is None else pids:
        try:
            if os.getpgid(pid) == pgid and process_factory(pid).status() != psutil.STATUS_ZOMBIE:
                members.append(pid)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied) as exc:
            # A group member whose state is unreadable cannot be declared ended.
            try:
                if os.getpgid(pid) == pgid:
                    raise JobsError("Managed process group is not fully observable") from exc
            except ProcessLookupError:
                pass
    return members


def group_exists(pgid):
    """Kernel-level final gate after reaping our leader; enumeration can race fork."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise JobsError("Process group absence cannot be verified") from exc
    return True


def observed_group_members(store, job, pids, *, process_factory=psutil.Process, identity_provider=identity):
    """Use the existing guardian lock and dedicated process group contract.

The live locked guardian owns surviving group children after the leader exits.
A live replacement with the original leader PID is never accepted.
"""
    group = job.get('group')
    if pids is None or not group or not owner_alive(job.get('worker')):
        return [], 'Managed membership unavailable'
    if not lock_held(store.root/'attempts'/job['attempt_id']/'worker.lock', strict=True):
        return [], 'Managed guardian lock unavailable'
    try:
        if identity_provider(group['pid']) != group:
            return [], 'Managed group leader identity changed'
    except (psutil.NoSuchProcess, ProcessLookupError):
        pass
    members = []
    for pid in group_members(group['pid'], pids=pids, process_factory=process_factory):
        try:
            owner = identity_provider(pid)
            if (owner['host'] != group['host'] or owner['boot'] != group['boot'] or
                    os.getsid(pid) != group['pid'] or os.getpgid(pid) != group['pid']):
                continue
            members.append({'identity': owner, 'pgid': group['pid']})
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
    return members, ''
