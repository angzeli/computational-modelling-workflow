"""Synthetic tests for advisory process-tree health monitoring."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from cmw.core.process_health import (
    FileMetric,
    HealthPolicy,
    HealthReport,
    HealthState,
    ProcessRecord,
    ProcessTreeSnapshot,
    assess_health,
    format_diagnostic,
    format_status_line,
    monitor_once,
    parse_cpu_time,
    process_cpu_delta,
    process_tree_records,
    sample_files,
    sample_process_tree,
    snapshot_from_records,
)


def _record(
    pid: int,
    ppid: int,
    *,
    cpu_seconds: float,
    cpu_percent: float = 0.0,
    rss_bytes: int = 128 * 1024**2,
) -> ProcessRecord:
    return ProcessRecord(pid, ppid, cpu_percent, rss_bytes, cpu_seconds, "worker")


def _snapshot(timestamp: float, cpu_seconds: float) -> ProcessTreeSnapshot:
    return snapshot_from_records(
        10, [_record(10, 1, cpu_seconds=cpu_seconds)], timestamp=timestamp
    )


def _file(size: int, mtime_ns: int = 1) -> tuple[FileMetric, ...]:
    return (FileMetric("run.log", True, size, mtime_ns),)


class ProcessHealthTests(unittest.TestCase):
    def test_root_only_and_descendants_are_discovered(self) -> None:
        records = (
            _record(10, 1, cpu_seconds=1.0),
            _record(11, 10, cpu_seconds=2.0),
            _record(12, 11, cpu_seconds=3.0),
            _record(99, 1, cpu_seconds=100.0),
        )
        self.assertEqual([item.pid for item in process_tree_records(10, records)], [10, 11, 12])
        self.assertEqual([item.pid for item in process_tree_records(99, records)], [99])

    def test_descendant_turnover_preserves_non_negative_cpu_evidence(self) -> None:
        previous = snapshot_from_records(
            10,
            (_record(10, 1, cpu_seconds=5.0), _record(11, 10, cpu_seconds=100.0)),
            timestamp=100.0,
        )
        current = snapshot_from_records(
            10,
            (_record(10, 1, cpu_seconds=6.0), _record(12, 10, cpu_seconds=20.0)),
            timestamp=130.0,
        )
        self.assertEqual(process_cpu_delta(current, previous), 21.0)
        self.assertEqual(assess_health(current, previous).state, HealthState.ACTIVE)

    def test_aggregate_cpu_and_rss_include_all_descendants(self) -> None:
        snapshot = snapshot_from_records(
            10,
            (
                _record(10, 1, cpu_seconds=1.0, cpu_percent=3.0, rss_bytes=10),
                _record(11, 10, cpu_seconds=2.0, cpu_percent=90.0, rss_bytes=20),
            ),
            timestamp=1.0,
        )
        self.assertEqual(snapshot.cpu_percent, 93.0)
        self.assertEqual(snapshot.rss_bytes, 30)
        self.assertEqual(snapshot.cumulative_cpu_seconds, 3.0)

    def test_cpu_activity_is_active(self) -> None:
        result = assess_health(_snapshot(130.0, 20.0), _snapshot(100.0, 10.0))
        self.assertEqual(result.state, HealthState.ACTIVE)
        self.assertEqual(result.inactive_intervals, 0)

    def test_file_activity_is_active_when_cpu_is_quiet(self) -> None:
        result = assess_health(
            _snapshot(130.0, 10.1), _snapshot(100.0, 10.0), _file(150, 2), _file(100, 1)
        )
        self.assertEqual(result.state, HealthState.ACTIVE)
        self.assertTrue(result.file_activity)
        self.assertEqual(result.file_growth_bytes, 50)

    def test_missing_activity_file_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            metric = sample_files([Path(temporary) / "missing.out"])[0]
        self.assertFalse(metric.exists)
        self.assertEqual(metric.size_bytes, 0)

    def test_unavailable_sampling_is_unknown(self) -> None:
        def denied(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="denied")

        snapshot = sample_process_tree(10, timestamp=100.0, runner=denied)
        self.assertFalse(snapshot.sampling_available)
        self.assertEqual(assess_health(snapshot, None).state, HealthState.UNKNOWN)

    def test_missing_root_is_unknown_not_completion(self) -> None:
        current = snapshot_from_records(10, (), timestamp=130.0)
        result = assess_health(current, _snapshot(100.0, 10.0))
        self.assertEqual(result.state, HealthState.UNKNOWN)
        self.assertIn("workflow-specific validator", result.reason)

    def test_quiet_intervals_are_advisory_only(self) -> None:
        waiting = assess_health(_snapshot(130.0, 10.1), _snapshot(100.0, 10.0))
        stalled = assess_health(
            _snapshot(130.0, 10.1),
            _snapshot(100.0, 10.0),
            previous_inactive_intervals=2,
            policy=HealthPolicy(stalled_after_intervals=3),
        )
        self.assertEqual(waiting.state, HealthState.WAITING)
        self.assertEqual(stalled.state, HealthState.POSSIBLY_STALLED)
        self.assertIn("diagnostic only", stalled.reason)

    def test_health_vocabulary_cannot_claim_scientific_completion(self) -> None:
        values = {state.value for state in HealthState}
        self.assertTrue(values.isdisjoint({"SUCCESS", "COMPLETED", "CONVERGED", "VALID"}))

    def test_monitor_state_persists_with_injected_sampler_and_clock(self) -> None:
        samples = iter((100.0, 130.0))
        cpu = iter((10.0, 20.0))

        def sampler(root_pid: int, *, timestamp: float) -> ProcessTreeSnapshot:
            return snapshot_from_records(
                root_pid, [_record(root_pid, 1, cpu_seconds=next(cpu))], timestamp=timestamp
            )

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "health.json"
            first = monitor_once(10, state, sampler=sampler, clock=lambda: next(samples))
            second = monitor_once(10, state, sampler=sampler, clock=lambda: next(samples))
            self.assertTrue(state.is_file())
        self.assertEqual(first.assessment.state, HealthState.UNKNOWN)
        self.assertEqual(second.assessment.state, HealthState.ACTIVE)

    def test_corrupt_persisted_state_fails_to_fresh_sample(self) -> None:
        def sampler(root_pid: int, *, timestamp: float) -> ProcessTreeSnapshot:
            return _snapshot(timestamp, 10.0)

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "health.json"
            state.write_text("not json", encoding="utf-8")
            report = monitor_once(10, state, sampler=sampler, clock=lambda: 100.0)
        self.assertEqual(report.assessment.state, HealthState.UNKNOWN)

    def test_formatting_includes_evidence_and_scope(self) -> None:
        current = snapshot_from_records(
            10,
            [_record(10, 1, cpu_seconds=20.0, cpu_percent=91.0, rss_bytes=1024**3)],
            timestamp=130.0,
        )
        assessment = assess_health(current, _snapshot(100.0, 10.0))
        report = HealthReport(current, (), assessment)
        self.assertIn("CPU 91.0%", format_status_line(report))
        diagnostic = format_diagnostic(report)
        self.assertIn("Health: ACTIVE", diagnostic)
        self.assertIn("operational evidence only", diagnostic)

    def test_cpu_time_parser_supports_common_posix_forms(self) -> None:
        self.assertEqual(parse_cpu_time("12:34.5"), 754.5)
        self.assertEqual(parse_cpu_time("02:03:04"), 7384.0)
        self.assertEqual(parse_cpu_time("1-02:03:04"), 93784.0)
        self.assertIsNone(parse_cpu_time("?"))

    def test_monitor_never_calls_process_termination(self) -> None:
        source = Path(__import__("cmw.core.process_health", fromlist=["x"]).__file__).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("os.kill(", source)
        self.assertNotIn(".terminate(", source)


if __name__ == "__main__":
    unittest.main()
