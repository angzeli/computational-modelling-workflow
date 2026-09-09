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


def lock_held(path):
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
        return True  # Inaccessible ownership must not authorize dispatch.


def group_members(pgid):
    """Only inspect membership of the group we created. No engine-name discovery."""
    members = []
    for pid in psutil.pids():
        try:
            if os.getpgid(pid) == pgid and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
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
