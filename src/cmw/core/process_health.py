"""Advisory process-tree health sampling for long-running workflows.

The monitor follows one explicitly owned root PID and its descendants.  It
describes operational activity only: none of its states certify scientific
success, program normal termination, or convergence.  In particular,
``POSSIBLY_STALLED`` is diagnostic evidence and never triggers termination.

The default sampler uses the POSIX ``ps`` command.  Callers can inject another
sampler and clock without changing the health-classification logic.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence


class HealthState(str, Enum):
    """Advisory liveness states; deliberately no scientific terminal states."""

    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    POSSIBLY_STALLED = "POSSIBLY_STALLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProcessRecord:
    """One operating-system process observation."""

    pid: int
    ppid: int
    cpu_percent: float | None
    rss_bytes: int | None
    cpu_seconds: float | None
    command: str = ""


@dataclass(frozen=True)
class ProcessTreeSnapshot:
    """Aggregate-able records for one root PID and its descendants."""

    root_pid: int
    timestamp: float
    alive: bool
    sampling_available: bool
    processes: tuple[ProcessRecord, ...] = ()
    error: str = ""

    @property
    def process_count(self) -> int:
        return len(self.processes)

    @property
    def cpu_percent(self) -> float | None:
        values = [record.cpu_percent for record in self.processes]
        if not values or any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    @property
    def rss_bytes(self) -> int | None:
        values = [record.rss_bytes for record in self.processes]
        if not values or any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    @property
    def cumulative_cpu_seconds(self) -> float | None:
        values = [record.cpu_seconds for record in self.processes]
        if not values or any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)


@dataclass(frozen=True)
class FileMetric:
    """Size and modification time for one explicitly selected activity file."""

    path: str
    exists: bool
    size_bytes: int = 0
    mtime_ns: int = 0


@dataclass(frozen=True)
class HealthPolicy:
    """Conservative thresholds for advisory liveness classification."""

    stalled_after_intervals: int = 3
    cpu_seconds_per_minute: float = 1.0
    minimum_cpu_delta_seconds: float = 0.5
    maximum_cpu_delta_seconds: float = 5.0

    def cpu_activity_threshold(self, interval_seconds: float) -> float:
        scaled = interval_seconds * self.cpu_seconds_per_minute / 60.0
        return min(
            self.maximum_cpu_delta_seconds,
            max(self.minimum_cpu_delta_seconds, scaled),
        )


@dataclass(frozen=True)
class HealthAssessment:
    """Classification and the evidence used for one sample transition."""

    state: HealthState
    reason: str
    inactive_intervals: int
    cpu_delta_seconds: float | None
    sample_interval_seconds: float | None
    file_activity: bool
    file_growth_bytes: int
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitorState:
    """Small persisted comparison state for periodic sampling."""

    snapshot: ProcessTreeSnapshot
    files: tuple[FileMetric, ...]
    inactive_intervals: int


@dataclass(frozen=True)
class HealthReport:
    """Current process/file evidence and its advisory classification."""

    snapshot: ProcessTreeSnapshot
    files: tuple[FileMetric, ...]
    assessment: HealthAssessment


class ProcessSampler(Protocol):
    """Backend boundary for process observation."""

    def __call__(self, root_pid: int, *, timestamp: float) -> ProcessTreeSnapshot: ...


PsRunner = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], float]


def parse_cpu_time(value: str) -> float | None:
    """Parse ``ps`` CPU time as MM:SS, HH:MM:SS, or D-HH:MM:SS."""

    value = value.strip()
    if not value or value in {"-", "?"}:
        return None
    days = 0
    if "-" in value:
        day_text, value = value.split("-", 1)
        days = int(day_text)
    fields = value.split(":")
    if len(fields) == 2:
        hours = 0
        minutes, seconds = fields
    elif len(fields) == 3:
        hours, minutes, seconds = fields
    else:
        raise ValueError(f"unsupported process CPU time: {value!r}")
    return days * 86_400 + int(hours) * 3_600 + int(minutes) * 60 + float(seconds)


def _optional_float(value: str) -> float | None:
    return None if value in {"-", "?"} else float(value.replace(",", "."))


def _optional_rss_bytes(value: str) -> int | None:
    return None if value in {"-", "?"} else int(value) * 1024


def parse_ps_output(output: str) -> tuple[ProcessRecord, ...]:
    """Parse the isolated representation requested from POSIX ``ps``."""

    records: list[ProcessRecord] = []
    for line in output.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) < 5:
            continue
        try:
            record = ProcessRecord(
                pid=int(fields[0]),
                ppid=int(fields[1]),
                cpu_percent=_optional_float(fields[2]),
                rss_bytes=_optional_rss_bytes(fields[3]),
                cpu_seconds=parse_cpu_time(fields[4]),
                command=fields[5] if len(fields) == 6 else "",
            )
        except (TypeError, ValueError):
            continue
        records.append(record)
    return tuple(records)


def process_tree_records(
    root_pid: int, records: Iterable[ProcessRecord]
) -> tuple[ProcessRecord, ...]:
    """Return the root and recursive descendants without process-name matching."""

    by_pid = {record.pid: record for record in records}
    if root_pid not in by_pid:
        return ()
    children: dict[int, list[int]] = {}
    for record in by_pid.values():
        children.setdefault(record.ppid, []).append(record.pid)
    ordered: list[ProcessRecord] = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop(0)
        if pid in seen or pid not in by_pid:
            continue
        seen.add(pid)
        ordered.append(by_pid[pid])
        pending.extend(sorted(children.get(pid, ())))
    return tuple(ordered)


def snapshot_from_records(
    root_pid: int,
    records: Iterable[ProcessRecord],
    *,
    timestamp: float,
) -> ProcessTreeSnapshot:
    """Build a deterministic snapshot from supplied process records."""

    tree = process_tree_records(root_pid, records)
    return ProcessTreeSnapshot(
        root_pid=root_pid,
        timestamp=timestamp,
        alive=bool(tree),
        sampling_available=True,
        processes=tree,
    )


def sample_process_tree(
    root_pid: int,
    *,
    timestamp: float | None = None,
    runner: PsRunner = subprocess.run,
) -> ProcessTreeSnapshot:
    """Observe a process tree with one lightweight POSIX ``ps`` call."""

    sampled_at = time.time() if timestamp is None else timestamp
    command = ("ps", "-axo", "pid=,ppid=,%cpu=,rss=,time=,command=")
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return ProcessTreeSnapshot(
            root_pid, sampled_at, False, False, error=str(exc)
        )
    if completed.returncode != 0:
        error = completed.stderr.strip() or f"ps exited with {completed.returncode}"
        return ProcessTreeSnapshot(root_pid, sampled_at, False, False, error=error)
    return snapshot_from_records(
        root_pid, parse_ps_output(completed.stdout), timestamp=sampled_at
    )


def sample_files(paths: Iterable[Path | str]) -> tuple[FileMetric, ...]:
    """Collect size and mtime only for explicitly supplied paths."""

    metrics: list[FileMetric] = []
    for value in paths:
        path = Path(value)
        try:
            stat = path.stat()
        except (FileNotFoundError, PermissionError, OSError):
            metrics.append(FileMetric(path=str(path), exists=False))
            continue
        metrics.append(
            FileMetric(str(path), True, stat.st_size, stat.st_mtime_ns)
        )
    return tuple(metrics)


def process_cpu_delta(
    current: ProcessTreeSnapshot, previous: ProcessTreeSnapshot
) -> float | None:
    """Return non-negative CPU evidence robust to descendant turnover."""

    if current.root_pid != previous.root_pid:
        return None
    if not current.sampling_available or not previous.sampling_available:
        return None
    current_values = {record.pid: record.cpu_seconds for record in current.processes}
    previous_values = {record.pid: record.cpu_seconds for record in previous.processes}
    if not current_values or any(value is None for value in current_values.values()):
        return None
    delta = 0.0
    for pid, current_value in current_values.items():
        assert current_value is not None
        prior = previous_values.get(pid)
        delta += max(0.0, current_value if prior is None else current_value - prior)
    return delta


def compare_file_metrics(
    current: Sequence[FileMetric], previous: Sequence[FileMetric]
) -> tuple[bool, int, tuple[str, ...]]:
    """Report explicit file metadata changes and aggregate signed size growth."""

    previous_by_path = {metric.path: metric for metric in previous}
    changed: list[str] = []
    growth = 0
    for metric in current:
        old = previous_by_path.get(metric.path)
        if old is None:
            if metric.exists:
                changed.append(metric.path)
                growth += metric.size_bytes
            continue
        if (
            metric.exists != old.exists
            or metric.size_bytes != old.size_bytes
            or metric.mtime_ns != old.mtime_ns
        ):
            changed.append(metric.path)
            growth += metric.size_bytes - old.size_bytes
    return bool(changed), growth, tuple(changed)


def assess_health(
    current: ProcessTreeSnapshot,
    previous: ProcessTreeSnapshot | None,
    current_files: Sequence[FileMetric] = (),
    previous_files: Sequence[FileMetric] = (),
    *,
    previous_inactive_intervals: int = 0,
    policy: HealthPolicy = HealthPolicy(),
) -> HealthAssessment:
    """Classify generic liveness without asserting completion or validity."""

    if not current.sampling_available:
        return HealthAssessment(
            HealthState.UNKNOWN,
            f"process sampling unavailable: {current.error or 'unknown error'}",
            previous_inactive_intervals,
            None,
            None,
            False,
            0,
        )
    if not current.alive:
        return HealthAssessment(
            HealthState.UNKNOWN,
            "root process is absent; completion requires a workflow-specific validator",
            previous_inactive_intervals,
            None,
            None,
            False,
            0,
        )
    if previous is None or previous.root_pid != current.root_pid:
        return HealthAssessment(
            HealthState.UNKNOWN,
            "first process sample; no CPU-time delta is available",
            0,
            None,
            None,
            False,
            0,
        )

    interval = current.timestamp - previous.timestamp
    file_activity, growth, changed_paths = compare_file_metrics(
        current_files, previous_files
    )
    if interval <= 0 or not math.isfinite(interval):
        return HealthAssessment(
            HealthState.UNKNOWN,
            "sample timestamps do not define a positive interval",
            previous_inactive_intervals,
            None,
            interval,
            file_activity,
            growth,
            changed_paths,
        )
    cpu_delta = process_cpu_delta(current, previous)
    if cpu_delta is not None and cpu_delta >= policy.cpu_activity_threshold(interval):
        return HealthAssessment(
            HealthState.ACTIVE,
            f"cumulative process-tree CPU advanced by {cpu_delta:.1f} s "
            f"during the last {interval:.1f} s",
            0,
            cpu_delta,
            interval,
            file_activity,
            growth,
            changed_paths,
        )
    if file_activity:
        return HealthAssessment(
            HealthState.ACTIVE,
            "selected output metadata changed during the last sample interval",
            0,
            cpu_delta,
            interval,
            True,
            growth,
            changed_paths,
        )
    if cpu_delta is None:
        return HealthAssessment(
            HealthState.UNKNOWN,
            "CPU-time data are unavailable and no file activity was observed",
            previous_inactive_intervals,
            None,
            interval,
            False,
            0,
        )

    inactive = previous_inactive_intervals + 1
    if inactive >= max(1, policy.stalled_after_intervals):
        state = HealthState.POSSIBLY_STALLED
        reason = (
            f"negligible CPU and file activity persisted for {inactive} intervals; "
            "diagnostic only"
        )
    else:
        state = HealthState.WAITING
        reason = "process is alive but this interval had negligible activity"
    return HealthAssessment(
        state, reason, inactive, cpu_delta, interval, False, 0
    )


def _snapshot_from_dict(data: dict[str, Any]) -> ProcessTreeSnapshot:
    return ProcessTreeSnapshot(
        root_pid=int(data["root_pid"]),
        timestamp=float(data["timestamp"]),
        alive=bool(data["alive"]),
        sampling_available=bool(data["sampling_available"]),
        processes=tuple(ProcessRecord(**item) for item in data.get("processes", ())),
        error=str(data.get("error", "")),
    )


def load_monitor_state(path: Path) -> MonitorState | None:
    """Load prior state; invalid or partial state starts a fresh sample."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return MonitorState(
            _snapshot_from_dict(data["snapshot"]),
            tuple(FileMetric(**item) for item in data.get("files", ())),
            int(data.get("inactive_intervals", 0)),
        )
    except (FileNotFoundError, OSError, UnicodeError, ValueError, KeyError, TypeError):
        return None


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def save_monitor_state(path: Path, state: MonitorState) -> None:
    """Atomically persist only small numeric samples and file metadata."""

    payload = {
        "schema_version": 1,
        "snapshot": asdict(state.snapshot),
        "files": [asdict(metric) for metric in state.files],
        "inactive_intervals": state.inactive_intervals,
    }
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def monitor_once(
    root_pid: int,
    state_path: Path,
    activity_paths: Sequence[Path | str] = (),
    *,
    policy: HealthPolicy = HealthPolicy(),
    sampler: ProcessSampler | None = None,
    clock: Clock = time.time,
) -> HealthReport:
    """Sample, classify, and persist one monitoring interval."""

    timestamp = clock()
    process_sampler = sampler or sample_process_tree
    previous = load_monitor_state(state_path)
    snapshot = process_sampler(root_pid, timestamp=timestamp)
    files = sample_files(activity_paths)
    assessment = assess_health(
        snapshot,
        previous.snapshot if previous else None,
        files,
        previous.files if previous else (),
        previous_inactive_intervals=previous.inactive_intervals if previous else 0,
        policy=policy,
    )
    try:
        save_monitor_state(
            state_path, MonitorState(snapshot, files, assessment.inactive_intervals)
        )
    except OSError as exc:
        assessment = replace(
            assessment, reason=f"{assessment.reason}; state was not persisted: {exc}"
        )
    return HealthReport(snapshot, files, assessment)


def format_bytes(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "?"
    value = float(max(0, size_bytes))
    units = ("B", "KB", "MB", "GiB", "TiB")
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} {units[unit]}"
    precision = 2 if unit >= 3 else 1
    return f"{value:.{precision}f} {units[unit]}"


def _format_optional(value: float | None, suffix: str = "") -> str:
    return "?" if value is None else f"{value:.1f}{suffix}"


def format_status_line(report: HealthReport) -> str:
    """Render a compact human diagnostic with no scientific verdict."""

    sampled = datetime.fromtimestamp(report.snapshot.timestamp).isoformat(timespec="seconds")
    return " | ".join(
        (
            sampled,
            f"PID {report.snapshot.root_pid}",
            f"processes {report.snapshot.process_count}",
            f"CPU {_format_optional(report.snapshot.cpu_percent, '%')}",
            f"RSS {format_bytes(report.snapshot.rss_bytes)}",
            f"health {report.assessment.state.value}",
        )
    )


def format_diagnostic(report: HealthReport) -> str:
    """Render the evidence and explicitly retain its advisory scope."""

    assessment = report.assessment
    lines = [
        f"Health: {assessment.state.value}",
        f"Reason: {assessment.reason}",
        f"Process tree: {report.snapshot.process_count}",
        f"Aggregate CPU: {_format_optional(report.snapshot.cpu_percent, '%')}",
        f"Aggregate RSS: {format_bytes(report.snapshot.rss_bytes)}",
    ]
    if assessment.cpu_delta_seconds is not None:
        lines.append(f"CPU delta: {assessment.cpu_delta_seconds:.1f} s")
    lines.append(f"File growth: {assessment.file_growth_bytes:+d} B")
    lines.append("Scope: operational evidence only; scientific status is not assessed")
    return "\n".join(lines)
