from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.periodic.vasp.result_execution import inspect_execution


class JobsExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = self.root / "synthetic-run"
        self.run.mkdir()
        self.state = self.root / "synthetic-state"
        self.attempt = "a" * 32
        self.attempt_dir = self.state / "attempts" / self.attempt
        self.attempt_dir.mkdir(parents=True)
        self.snapshot_path = self.root / "exported-snapshot.json"
        self.receipt_path = self.attempt_dir / "payload-exit.json"
        self.job = {
            "id": 7, "display_id": "J7.1", "attempt_id": self.attempt,
            "engine": "Command", "argv": ["/bin/bash", "synthetic-runner.sh"],
            "cwd": str(self.run), "status": "Done", "started_at": 100.0,
            "finished_at": 103.0, "exit_code": 0, "signal": None,
            "cancel_requested": False, "layout": None,
            "logs": {stream: str(self.attempt_dir / f"{stream}.log")
                     for stream in ("stdout", "stderr")},
            "env": {"PRIVATE_TOKEN": "never copy into scientific records"},
        }
        self.snapshot = {"schema": 2, "state_directory": str(self.state),
                         "observed_at": 104.0, "jobs": [self.job]}
        self.receipt = {"exit_code": 0, "recorded_at": 102.0}

    def inspect(self, *, snapshot=None, receipt=None, **options):
        self.snapshot_path.write_text(json.dumps(self.snapshot if snapshot is None else snapshot))
        self.receipt_path.write_text(json.dumps(self.receipt if receipt is None else receipt))
        return inspect_execution(self.snapshot_path, self.receipt_path,
                                 job_id=options.pop("job_id", 7),
                                 attempt_id=options.pop("attempt_id", self.attempt),
                                 run_directory=options.pop("run_directory", self.run), **options)

    def codes(self, result):
        return {item["code"] for item in result["findings"]}

    def test_native_terminal_receipt_and_saved_snapshot_are_eligible(self):
        # No Store object or process observation is needed, even for generic runners.
        with patch("cmw.jobs.store.Store", side_effect=AssertionError("must not access Jobs state")):
            result = self.inspect()
        self.assertEqual(result["status"], "eligible", result)
        self.assertTrue(result["operational_success"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["sources"]), 2)
        self.assertTrue(all(source["sha256"] for source in result["sources"]))
        self.assertNotIn("env", result["job"])
        self.assertEqual(result["binding"]["scientific_source_binding"], "not_provided_by_jobs")

    def test_layout_scientific_attempt_is_a_separate_namespace(self):
        self.job["layout"] = {"path": "/synthetic/layout.json", "target_id": "target",
                              "attempt_id": "attempt-0001"}
        result = self.inspect()
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["binding"]["scientific_layout"]["attempt_id"], "attempt-0001")

    def test_identity_mismatch_and_duplicate_selection_fail(self):
        for options in ({"job_id": 8}, {"attempt_id": "b" * 32},
                        {"job_id": True}, {"attempt_id": "J7.1"}):
            with self.subTest(options=options):
                self.assertEqual(self.inspect(**options)["status"], "invalid")
        self.snapshot["jobs"].append(copy.deepcopy(self.job))
        self.assertIn("execution.job_identity", self.codes(self.inspect()))

    def test_unrelated_receipt_location_is_not_accepted(self):
        self.inspect()
        copied = self.root / "payload-exit.json"
        copied.write_bytes(self.receipt_path.read_bytes())
        result = inspect_execution(self.snapshot_path, copied, job_id=7,
                                   attempt_id=self.attempt, run_directory=self.run)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("execution.receipt_attempt_location", self.codes(result))

    def test_mismatched_directory_or_log_paths_are_rejected(self):
        self.assertIn("execution.working_directory", self.codes(self.inspect(run_directory=self.root)))
        self.job["logs"]["stdout"] = str(self.root / "unrelated-stdout.log")
        self.assertIn("execution.attempt_logs", self.codes(self.inspect()))

    def test_exit_disagreement_nonzero_signal_and_cancellation(self):
        self.assertIn("execution.exit_agreement", self.codes(self.inspect(receipt={"exit_code": 1, "recorded_at": 102})))
        for changes in ({"exit_code": 1, "status": "Fail"}, {"signal": 15},
                        {"cancel_requested": True}, {"status": "Cancelled"}):
            with self.subTest(changes=changes):
                saved = dict(self.job)
                self.job.update(changes)
                self.assertEqual(self.inspect()["status"], "invalid")
                self.job.clear()
                self.job.update(saved)

    def test_unknown_or_missing_execution_is_not_promoted(self):
        self.job.update(status="Unknown", exit_code=None, finished_at=None)
        self.assertEqual(self.inspect()["status"], "unknown")
        result = inspect_execution(None, None, job_id=7, attempt_id=self.attempt, run_directory=self.run)
        self.assertEqual(result["status"], "unknown")
        result = inspect_execution(self.root / "missing.json", None, job_id=7,
                                   attempt_id=self.attempt, run_directory=self.run)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["exit_code"])

    def test_forged_shape_and_permissive_numeric_types_are_rejected(self):
        for receipt in ({"exit_code": True, "recorded_at": 102},
                        {"exit_code": "0", "recorded_at": 102},
                        {"exit_code": -1, "recorded_at": 102},
                        {"exit_code": 0, "recorded_at": True},
                        {"exit_code": 0, "recorded_at": 10 ** 400},
                        {"exit_code": 0, "recorded_at": 102, "assume_success": True}):
            with self.subTest(receipt=receipt):
                self.assertNotEqual(self.inspect(receipt=receipt)["status"], "eligible")
        for schema in (True, 1, "2"):
            snapshot = {**self.snapshot, "schema": schema}
            self.assertIn("execution.snapshot_schema", self.codes(self.inspect(snapshot=snapshot)))
        self.job["exit_code"] = False
        self.assertIn("execution.exit_code_type", self.codes(self.inspect()))

    def test_timestamp_order_and_types_must_bind_receipt_to_attempt(self):
        for time in (99, 104):
            self.assertIn("execution.time_order", self.codes(self.inspect(receipt={"exit_code": 0, "recorded_at": time})))
        self.snapshot["observed_at"] = 101
        self.assertIn("execution.time_order", self.codes(self.inspect()))
        self.snapshot["observed_at"] = True
        self.assertIn("execution.timestamps", self.codes(self.inspect()))

    def test_no_signal_and_no_cancellation_must_be_explicit(self):
        del self.job["signal"]
        self.assertEqual(self.inspect()["status"], "unknown")
        self.job["signal"] = None
        del self.job["cancel_requested"]
        self.assertEqual(self.inspect()["status"], "unknown")

    def test_missing_or_malformed_command_is_not_completion_evidence(self):
        for argv in (None, [], [""], ["runner", 3], ["runner\0"]):
            self.job["argv"] = argv
            self.assertIn("execution.command", self.codes(self.inspect()))

    def test_read_only_inputs_are_unchanged_and_no_state_is_created(self):
        self.inspect()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        inspect_execution(self.snapshot_path, self.receipt_path, job_id=7,
                          attempt_id=self.attempt, run_directory=self.run)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
