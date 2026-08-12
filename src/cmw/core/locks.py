"""Conservative local locks for stage mutation.

These host/PID locks do not provide scheduler or shared-cluster coordination.
Remote-host or unreadable ownership is uncertain and therefore fails closed.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

class LockState(str, Enum):
    ABSENT = "ABSENT"
    ACTIVE_LOCAL = "ACTIVE_LOCAL"
    STALE_LOCAL = "STALE_LOCAL"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class LockOwner:
    schema_version: int
    token: str
    hostname: str
    pid: int
    job_id: str
    created_at: str


@dataclass(frozen=True)
class LockInspection:
    state: LockState
    reason: str
    owner: LockOwner | None = None


def _pid_state(pid: int) -> LockState:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return LockState.STALE_LOCAL
    except (PermissionError, OSError):
        return LockState.UNCERTAIN
    return LockState.ACTIVE_LOCAL


def inspect_lock(path: Path, *, hostname: str | None = None) -> LockInspection:
    """Diagnose lock ownership without mutating it."""

    if not path.exists():
        return LockInspection(LockState.ABSENT, "lock does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        owner = LockOwner(
            schema_version=int(data["schema_version"]),
            token=str(data["token"]),
            hostname=str(data["hostname"]),
            pid=int(data["pid"]),
            job_id=str(data.get("job_id", "")),
            created_at=str(data["created_at"]),
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return LockInspection(LockState.UNCERTAIN, "lock ownership is unreadable")
    local_hostname = hostname or socket.gethostname()
    if owner.hostname != local_hostname:
        return LockInspection(
            LockState.UNCERTAIN,
            f"lock belongs to another or unknown host: {owner.hostname}",
            owner,
        )
    state = _pid_state(owner.pid)
    reasons = {
        LockState.ACTIVE_LOCAL: f"local owner PID {owner.pid} is active",
        LockState.STALE_LOCAL: f"local owner PID {owner.pid} is absent",
        LockState.UNCERTAIN: f"local owner PID {owner.pid} cannot be verified",
    }
    return LockInspection(state, reasons[state], owner)


def acquire_lock(
    path: Path,
    *,
    job_id: str,
    owner_pid: int | None = None,
    replace_stale: bool = False,
) -> LockOwner:
    """Acquire exclusively; stale replacement must be requested explicitly."""

    path.parent.mkdir(parents=True, exist_ok=True)
    inspection = inspect_lock(path)
    if inspection.state is LockState.STALE_LOCAL and replace_stale:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    elif inspection.state is not LockState.ABSENT:
        raise RuntimeError(f"{inspection.state.value}: {inspection.reason}")

    owner = LockOwner(
        schema_version=1,
        token=uuid4().hex,
        hostname=socket.gethostname(),
        pid=os.getpid() if owner_pid is None else owner_pid,
        job_id=job_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(asdict(owner), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise RuntimeError(f"lock was acquired concurrently: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return owner


def release_lock(path: Path, *, token: str) -> bool:
    """Release only when the caller proves ownership with the lock token."""

    inspection = inspect_lock(path)
    if inspection.owner is None or inspection.owner.token != token:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
