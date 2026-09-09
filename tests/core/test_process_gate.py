"""Synthetic, calculation-free startup workload gate tests."""

from dataclasses import replace
import subprocess
import unittest
from unittest.mock import patch

from cmw.core.process_gate import (
    ProcessGate, ProcessGateError, ProcessObservation,
    parse_process_table, sample_processes, wait_for_release,
)


def record(pid, ppid=1, pgid=1, second=0, uid=501, executable="worker"):
    return ProcessObservation(uid, pid, ppid, pgid,
                              f"Tue Sep 8 10:00:{second:02d} 2026", "S", executable)


class ProcessGateTests(unittest.TestCase):
    def gate(self, records, **kwargs):
        return ProcessGate(records, lambda p: p.executable == "target", uid=501, **kwargs)

    def test_snapshot_root_and_recursive_workers_not_shared_group(self):
        records = [record(10, executable="target"), record(11, 10),
                   record(12, 11), record(99)]
        gate = self.gate(records)
        self.assertEqual([p.pid for p in gate.initial], [10, 11, 12])

    def test_wait_ignores_future_independent_match(self):
        root, future = record(10, executable="target"), record(90, second=1, executable="target")
        gate = self.gate([root])
        samples = iter([[root, future], [future]])
        sleeps = []
        wait_for_release(gate, sampler=lambda: next(samples), sleep=sleeps.append)
        self.assertEqual(sleeps, [30])
        self.assertNotIn(future.identity, gate.members)

    def test_new_descendant_then_reparenting_remains_watched(self):
        root = record(10, executable="target")
        child = record(11, 10, second=1)
        gate = self.gate([root])
        self.assertEqual(len(gate.update([root, child])), 2)
        self.assertEqual([p.pid for p in gate.update([replace(child, ppid=1)])], [11])
        self.assertEqual(gate.update([]), ())

    def test_initial_orphan_match_and_user_boundary(self):
        gate = self.gate([record(10, executable="target"), record(11, uid=502, executable="target")])
        self.assertEqual([p.pid for p in gate.initial], [10])

    def test_dedicated_live_group_leader_but_not_reused_group(self):
        root = record(10, pgid=10, executable="target")
        helper = record(11, pgid=10, second=1)
        gate = self.gate([root, helper])
        self.assertEqual(len(gate.initial), 2)
        self.assertEqual(gate.update([record(10, pgid=10, second=2),
                                      record(12, pgid=10, second=2)]), ())

    def test_pid_reuse_does_not_seed_new_children(self):
        root = record(10, executable="target")
        gate = self.gate([root])
        self.assertEqual(gate.update([replace(root, started=record(10, second=1).started),
                                      record(11, 10, second=1)]), ())

    def test_child_older_than_parent_not_attached(self):
        gate = self.gate([record(10, second=2, executable="target"), record(11, 10)])
        self.assertEqual([p.pid for p in gate.initial], [10])

    def test_no_process_fails_closed_and_override(self):
        with self.assertRaises(ProcessGateError):
            self.gate([])
        gate = self.gate([], allow_empty=True)
        self.assertEqual(gate.update([record(10, executable="target")]), ())

    def test_zombie_is_exited(self):
        root = record(10, executable="target")
        gate = self.gate([root])
        self.assertEqual(gate.update([replace(root, state="Z")]), ())

    def test_parse_spaces_and_malformed_fails_closed(self):
        text = "501 10 1 10 Tue Sep  8 10:00:00 2026 S /a path/worker\n"
        self.assertEqual(parse_process_table(text)[0].executable, "/a path/worker")
        for bad in ("bad row", text + text):
            with self.assertRaises(ProcessGateError):
                parse_process_table(bad)

    def test_sampling_error_never_releases(self):
        gate = self.gate([record(10, executable="target")])
        def denied():
            raise ProcessGateError("denied")
        with self.assertRaises(ProcessGateError):
            wait_for_release(gate, sampler=denied)

    def test_cancel_propagates_without_process_mutation(self):
        root = record(10, executable="target")
        gate = self.gate([root])
        def cancel(seconds):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            wait_for_release(gate, sampler=lambda: [root], sleep=cancel)
        self.assertEqual(gate.update([root]), (root,))

    def test_invalid_poll_and_compact_heartbeat(self):
        root = record(10, executable="target")
        gate = self.gate([root])
        for poll in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                wait_for_release(gate, poll_seconds=poll)
        samples = iter([[root], [root], []])
        events = []
        wait_for_release(gate, sampler=lambda: next(samples), sleep=lambda _: None,
                         clock=lambda: 0., observe=events.append)
        self.assertEqual(events, [(root,), ()])

    def test_sampler_wraps_command_errors(self):
        errors = [
            OSError("ps unavailable"),
            subprocess.CalledProcessError(1, ["/bin/ps"]),
            subprocess.TimeoutExpired(["/bin/ps"], 10),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch("cmw.core.process_gate.subprocess.run", side_effect=error):
                    with self.assertRaisesRegex(ProcessGateError, "Process sampling failed") as caught:
                        sample_processes()
                self.assertIs(caught.exception.__cause__, error)

    def test_sampler_parses_output_and_checks_observer(self):
        text = "501 10 1 10 Tue Sep  8 10:00:00 2026 S /a path/worker\n"
        completed = subprocess.CompletedProcess(["/bin/ps"], 0, stdout=text)
        with patch("cmw.core.process_gate.subprocess.run", return_value=completed), \
             patch("cmw.core.process_gate.os.getpid", return_value=10), \
             patch("cmw.core.process_gate.os.getuid", return_value=501):
            self.assertEqual(sample_processes(), (record(10, pgid=10, executable="/a path/worker"),))

    def test_sampler_rejects_missing_or_wrong_uid_observer(self):
        for text in ("", "501 11 1 10 Tue Sep  8 10:00:00 2026 S worker\n",
                     "502 10 1 10 Tue Sep  8 10:00:00 2026 S worker\n"):
            with self.subTest(text=text):
                completed = subprocess.CompletedProcess(["/bin/ps"], 0, stdout=text)
                with patch("cmw.core.process_gate.subprocess.run", return_value=completed), \
                     patch("cmw.core.process_gate.os.getpid", return_value=10), \
                     patch("cmw.core.process_gate.os.getuid", return_value=501):
                    with self.assertRaisesRegex(ProcessGateError, "does not contain this observer"):
                        sample_processes()

    def test_sampler_rejects_malformed_output(self):
        completed = subprocess.CompletedProcess(["/bin/ps"], 0, stdout="bad row\n")
        with patch("cmw.core.process_gate.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ProcessGateError, "Unparseable process row"):
                sample_processes()

    def test_heartbeat_boundary_and_timer_reset(self):
        root = record(10, executable="target")
        gate = self.gate([root])
        now = 0
        events = []
        # Observe just before and exactly at successive 15-minute boundaries.
        times = iter([899, 900, 1799, 1800, 1801])
        def advance(seconds):
            nonlocal now
            now = next(times)
        wait_for_release(
            gate, sampler=lambda: [root] if now < 1801 else [],
            sleep=advance, clock=lambda: now,
            observe=lambda live: events.append((now, live)),
        )
        self.assertEqual(events, [(0, (root,)), (900, (root,)),
                                  (1800, (root,)), (1801, ())])


if __name__ == "__main__":
    unittest.main()
