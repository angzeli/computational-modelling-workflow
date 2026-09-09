"""Read-only, startup-scoped local process release gate (no scientific verdict).

Unlike advisory process_health, membership survives reparenting and is keyed by
UID, PID and start time. New descendants of still-observed members are admitted;
new independent matches are not. Polling cannot see a child born and orphaned
entirely between samples. POSIX ps start times have one-second resolution.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable


class ProcessGateError(RuntimeError):
    """Missing or unreliable process evidence; never permission to release."""


@dataclass(frozen=True)
class ProcessIdentity:
    uid: int
    pid: int
    started: str


@dataclass(frozen=True)
class ProcessObservation:
    uid: int
    pid: int
    ppid: int
    pgid: int
    started: str
    state: str
    executable: str

    @property
    def identity(self) -> ProcessIdentity:
        return ProcessIdentity(self.uid, self.pid, self.started)

    @property
    def running(self) -> bool:
        # Zombies have exited and cannot consume compute or spawn children.
        return not self.state.startswith(("Z", "X"))


def parse_process_table(text: str) -> tuple[ProcessObservation, ...]:
    """Parse headerless LC_ALL=C ps output; malformed rows fail closed."""
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 10)
        try:
            if len(fields) != 11:
                raise ValueError("missing columns")
            started = " ".join(fields[4:9])
            datetime.strptime(started, "%a %b %d %H:%M:%S %Y")
            result.append(ProcessObservation(
                *map(int, fields[:4]), started, fields[9], fields[10]
            ))
        except ValueError as exc:
            raise ProcessGateError(f"Unparseable process row: {line!r}") from exc
    if len({p.pid for p in result}) != len(result):
        raise ProcessGateError("Duplicate PIDs in process observation")
    return tuple(result)


def sample_processes() -> tuple[ProcessObservation, ...]:
    """One read-only macOS/Linux ps observation; no name scans in the waiter."""
    try:
        completed = subprocess.run(
            ["/bin/ps", "-ww", "-axo", "uid=,pid=,ppid=,pgid=,lstart=,stat=,comm="],
            capture_output=True, text=True, check=True, timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
        records = parse_process_table(completed.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessGateError(f"Process sampling failed: {exc}") from exc
    if not any(p.pid == os.getpid() and p.uid == os.getuid() for p in records):
        raise ProcessGateError("Process table does not contain this observer")
    return records


class ProcessGate:
    """Track only initial matches and their observed workload descendants.

    A group is eligible only while its leader is a live admitted member. Shared
    ancestor/terminal groups are never followed. Membership is monotonic, but
    identities which have exited cannot seed descendants after PID reuse.
    """

    def __init__(
        self, records: Iterable[ProcessObservation],
        matches: Callable[[ProcessObservation], bool], *, uid: int,
        allow_empty: bool = False,
    ) -> None:
        records = tuple(records)
        self.uid = uid
        self.members = {p.identity: p for p in records
                        if p.uid == uid and p.running and matches(p)}
        if not self.members and not allow_empty:
            raise ProcessGateError("No active matching workload at startup; refusing release")
        self.initial = self.update(records)

    def update(self, records: Iterable[ProcessObservation]) -> tuple[ProcessObservation, ...]:
        current = {p.identity: p for p in records if p.uid == self.uid and p.running}
        live = {key: current[key] for key in self.members if key in current}
        while True:
            parents = {p.pid: p for p in live.values()}
            groups = {p.pgid for p in live.values() if p.pid == p.pgid}
            additions = {}
            for key, p in current.items():
                if key in live:
                    continue
                parent = parents.get(p.ppid)
                # The ps table is not atomic: do not attach a child older than
                # its supposed parent (e.g. a PID changed during observation).
                parent_ok = parent is not None and self._start(p) >= self._start(parent)
                leader = parents.get(p.pgid)
                group_ok = p.pgid in groups and leader is not None and self._start(p) >= self._start(leader)
                if parent_ok or group_ok:
                    additions[key] = p
            if not additions:
                break
            live.update(additions)
        self.members.update(live)
        return tuple(sorted(live.values(), key=lambda p: p.pid))

    @staticmethod
    def _start(p: ProcessObservation) -> datetime:
        return datetime.strptime(p.started, "%a %b %d %H:%M:%S %Y")


def wait_for_release(
    gate: ProcessGate, *, poll_seconds: float = 30,
    sampler: Callable[[], Iterable[ProcessObservation]] = sample_processes,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    observe: Callable[[tuple[ProcessObservation, ...]], object] = lambda records: None,
) -> None:
    """Wait read-only, reporting transitions/15-minute heartbeats.

    Callers own cancellation (an injected sleep may raise). This primitive
    installs no signal handlers and never signals any watched process.
    """
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll_seconds must be finite and positive")
    previous = None
    reported_at = clock()
    while True:
        live = gate.update(sampler())
        identities = tuple(p.identity for p in live)
        if identities != previous or clock() - reported_at >= 900:
            observe(live)
            previous, reported_at = identities, clock()
        if not live:
            return
        sleep(poll_seconds)
