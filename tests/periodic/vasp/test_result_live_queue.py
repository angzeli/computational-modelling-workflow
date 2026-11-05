"""Verify published synthetic VASP records against a real, fixture-owned SQLite queue."""
from contextlib import closing, contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs.store import Store
from cmw.periodic.vasp.result_finalization import _record_identity, finalize_result, verify_finalization
from .result_case import digest, make_case, write_json
from .runner_case import add_runner


class LiveQueueVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = make_case(Path(temporary.name) / "synthetic")
        self.case["state"].chmod(0o700)
        self.store = Store(self.case["state"])
        self.job = json.loads(self.case["snapshot"].read_text())["jobs"][0]
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO jobs(id, data) VALUES (?, ?)", (self.job["id"], json.dumps(self.job)))
        record = finalize_result(
            self.case["run"], policy_path=self.case["policy_path"], spec_path=self.case["spec_path"],
            scratch_root=self.case["scratch"], record_directory="complete",
        )
        self.path = Path(record["publication"]["record_path"])
        self.run_options = (self.case["run"], "--policy", self.case["policy_path"],
                            "--spec", self.case["spec_path"], "--json")

    @contextmanager
    def exclusive_lock(self):
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            yield writer

    def call_cli(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["vasp", *map(str, arguments)])
        return code, output.getvalue()

    def verify_cli(self, *options):
        return self.call_cli("verify-result-record", self.path, *options)

    def test_exclusive_lock_throughout_returns_temporarily_unverifiable(self):
        original_record = self.path.read_bytes()
        with self.exclusive_lock():
            code, output = self.verify_cli("--json")
        result = json.loads(output)
        self.assertEqual(code, 75)
        self.assertEqual(result["exit_code"], 75)
        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertIsNone(result["valid"])
        self.assertEqual(result["live_queue"]["status"], "UNAVAILABLE")
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(self.path.read_bytes(), original_record)

    def test_locked_queue_prevents_inspection_verdict_and_finalization_preview(self):
        scratch = self.case["scratch"]
        before = set(scratch.rglob("*"))
        with self.exclusive_lock():
            code, output = self.call_cli("inspect-result", *self.run_options)
            result = json.loads(output)
            self.assertEqual(code, 75)
            self.assertEqual(result["exit_code"], 75)
            binding = result["binding"]
            self.assertIsNone(binding["valid"])
            self.assertIsNone(binding["checks"]["execution"])
            self.assertEqual(binding["execution"]["status"].upper(), "UNVERIFIABLE")
            self.assertEqual(binding["execution"]["live_queue"]["status"], "UNAVAILABLE")
            code, output = self.call_cli("finalize-result", *self.run_options, "--scratch-root",
                                         scratch, "--record-directory", "preview", "--dry-run")
        result = json.loads(output)
        self.assertEqual(code, 75)
        self.assertEqual(result["exit_code"], 75)
        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertNotIn("publication", result)
        self.assertEqual(set(scratch.rglob("*")), before)

    def test_lock_released_during_retry_matches_saved_attempt(self):
        with self.exclusive_lock() as writer:
            # Release only after an actual busy read reaches its retry interval.
            with patch("cmw.periodic.vasp.result_execution.time.sleep",
                       side_effect=lambda delay: writer.rollback()) as retry:
                code, output = self.verify_cli("--json")
            retry.assert_called_once()
        result = json.loads(output)
        self.assertEqual(code, 0, result["findings"])
        self.assertEqual(result["status"], "VERIFIED")
        self.assertIs(result["valid"], True)
        self.assertEqual(result["live_queue"]["status"], "MATCH")
        self.assertEqual(result["live_queue"]["differences"], [])
        self.assertEqual(len(result["artifacts"]), 1)

    def test_changed_live_fields_report_saved_and_observed_values(self):
        with self.store.transaction() as conn:
            changed = {**self.job, "status": "Fail", "exit_code": 17}
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(changed), self.job["id"]))
        code, output = self.verify_cli("--json")
        result = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "INVALID")
        self.assertIs(result["valid"], False)
        self.assertEqual(result["live_queue"]["status"], "CONTRADICTED")
        for field, stored, observed in (("job.status", "Done", "Fail"), ("job.exit_code", 0, 17)):
            entry = {"field": field, "stored": stored, "observed": observed}
            self.assertIn(entry, result["live_queue"]["differences"])
            self.assertIn(entry, result["differences"])
        code, output = self.verify_cli()
        self.assertEqual(code, 2)
        self.assertRegex(output, r"job\.status[^\n]*Done[^\n]*Fail")
        self.assertRegex(output, r"job\.exit_code[^\n]*0[^\n]*17")

    def test_runner_potential_mismatch_exposes_identity_without_input_contents(self):
        add_runner(self.case["root"], self.case["run"], self.case["spec"]["execution"])
        write_json(self.case["spec_path"], self.case["spec"])
        job = json.loads(self.case["snapshot"].read_text())["jobs"][0]
        with self.store.transaction() as conn:
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["id"]))
        potential = self.case["run"] / "POTCAR"
        original = potential.read_text().strip()
        replacement = "changed synthetic potential for privacy regression"
        potential.write_text(replacement + "\n")
        code, output = self.call_cli("inspect-result", *self.run_options)
        self.assertNotEqual(code, 0)
        execution = json.loads(output)["binding"]["execution"]
        self.assertEqual(execution["live_queue"]["status"], "MATCH")
        self.assertIs(execution["checks"]["runner_association"], False)
        observed = [s["sha256"] for s in execution["sources"] if s["role"] == "runner_input:POTCAR"]
        self.assertEqual(observed, [digest(potential)])
        self.assertNotIn(original, output)
        self.assertNotIn(replacement, output)

    def test_legacy_record_requires_previously_observed_database(self):
        record = json.loads(self.path.read_text())
        execution = record["evidence"]["binding"]["execution"]
        self.assertIs(execution["checks"]["native_attempt"], True)
        self.assertIs(execution["checks"]["native_queue_schema"], True)
        execution.pop("live_queue")
        record["record_id"] = _record_identity(record)
        write_json(self.path, record)
        verified = verify_finalization(self.path)
        self.assertIs(verified["valid"], True, verified["findings"])
        self.assertEqual(verified["live_queue"]["status"], "MATCH")

        self.store.path.unlink()
        unavailable = verify_finalization(self.path)
        self.assertIsNone(unavailable["valid"], unavailable["findings"])
        self.assertEqual(unavailable["status"], "UNVERIFIABLE")
        self.assertEqual(unavailable["live_queue"]["status"], "UNAVAILABLE")
        self.assertFalse(self.store.path.exists())

    def test_complete_record_cannot_claim_a_contradicted_live_queue(self):
        record = json.loads(self.path.read_text())
        record["evidence"]["binding"]["execution"]["live_queue"]["status"] = "CONTRADICTED"
        record["record_id"] = _record_identity(record)
        write_json(self.path, record)
        result = verify_finalization(self.path)
        self.assertIs(result["valid"], False)
        self.assertEqual(result["differences"][0]["field"], "binding.execution.live_queue.status")

    def test_missing_row_in_readable_queue_contradicts_the_binding(self):
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (self.job["id"],))
        result = verify_finalization(self.path)
        self.assertIs(result["valid"], False)
        self.assertEqual(result["live_queue"]["status"], "CONTRADICTED")
        self.assertIn({"field": "job.id", "stored": self.job["id"], "observed": None}, result["differences"])

    def test_changed_native_output_is_invalid_even_when_queue_is_locked(self):
        path = self.case["run"] / "OSZICAR"
        original_hash = digest(path)
        with path.open("a") as source:
            source.write("synthetic change after publication\n")
        with self.exclusive_lock():
            result = verify_finalization(self.path)
        self.assertEqual(result["live_queue"]["status"], "UNAVAILABLE")
        self.assertEqual(result["status"], "INVALID")
        self.assertIs(result["valid"], False)
        self.assertIn({"field": f"sources[OSZICAR:{path}].sha256", "stored": original_hash,
                       "observed": digest(path)}, result["differences"])
        self.assertEqual(result["artifacts"], [])
