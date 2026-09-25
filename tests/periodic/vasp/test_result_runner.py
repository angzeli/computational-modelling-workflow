"""Synthetic external-runner records; no real executable or potential data."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.result_binding import inspect_binding
from cmw.periodic.vasp.result_execution import inspect_execution
from cmw.periodic.vasp.results import inspect_result
from .result_fixtures import write_run
from .test_result_binding import make_spec


from .runner_case import DIALECT, add_runner, sha, timestamp


class RunnerBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = write_run(self.root / "run")
        self.native = inspect_result(self.run)
        self.spec = make_spec(self.root, self.native)
        self.metadata = add_runner(self.root, self.run, self.spec["execution"])
        self.original = self.metadata.read_text()

    def inspect(self, *, runner=True, native=None):
        execution = self.spec["execution"]
        options = {"runner_record": execution["runner_record"],
                   "native_sources": self.native["sources"] if native is None else native} if runner else {}
        return inspect_execution(self.root / execution["snapshot"], self.root / execution["receipt"],
                                 job_id=7, attempt_id=execution["attempt_id"],
                                 run_directory=self.run, **options)

    def replace_metadata(self, text):
        self.metadata.write_text(text)
        self.spec["execution"]["runner_record"]["sha256"] = sha(self.metadata)

    def edit_job(self, change):
        path = self.root / self.spec["execution"]["snapshot"]
        value = json.loads(path.read_text())
        change(value["jobs"][0])
        path.write_text(json.dumps(value))
        self.spec["execution"]["source_identities"]["jobs_snapshot"] = sha(path)

    def test_explicit_record_binds_original_output_without_changing_wrapper_cwd(self):
        self.assertEqual(self.inspect(runner=False)["status"], "invalid")
        result = self.inspect()
        self.assertEqual(result["status"], "eligible", result)
        self.assertEqual(result["job"]["cwd"], str(self.root / "inputs"))
        self.assertEqual(result["binding"]["wrapper_cwd"], str(self.root / "inputs"))
        self.assertEqual(result["binding"]["payload_cwd"], str(self.run))
        self.assertEqual({s["role"] for s in result["sources"]},
                         {"jobs_snapshot", "jobs_receipt", "runner_record", "runner_input:INCAR",
                          "runner_input:POSCAR", "runner_input:KPOINTS", "runner_input:POTCAR"})
        self.assertNotIn("synthetic POTCAR", json.dumps(result))

    def test_binding_spec_accepts_explicit_record_and_closes_all_sources(self):
        path = self.root / "spec.json"
        path.write_text(json.dumps(self.spec))
        result = inspect_binding(path, self.native, "c" * 64)
        self.assertTrue(result["valid"], result)
        self.assertIn("runner_input:POTCAR", {s["role"] for s in result["sources"]})

    def test_record_hash_and_dialect_are_explicit(self):
        for field, value in (("sha256", "d" * 64), ("dialect", "unknown")):
            original = self.spec["execution"]["runner_record"][field]
            self.spec["execution"]["runner_record"][field] = value
            self.assertNotEqual(self.inspect()["status"], "eligible")
            self.spec["execution"]["runner_record"][field] = original
        self.spec["execution"]["runner_record"]["guess_output"] = True
        self.assertNotEqual(self.inspect()["status"], "eligible")

    def test_duplicate_identity_lifecycle_stop_and_exit_are_rejected(self):
        for text in (self.original + "output: " + str(self.run) + "\n",
                     self.original.replace("state: RUNNING", "state: STAGING"),
                     self.original.replace("child_status: 0", "child_status: 1"),
                     self.original.replace("launcher_status: 0", "launcher_status: 143"),
                     self.original.replace("reason: completed", "reason: timeout"),
                     self.original.replace("advance_stop: not reached", "advance_stop: requested"),
                     self.original.replace(timestamp(101.8), timestamp(105)),
                     self.original + "source: " + str(self.root / "inputs") + "\n",
                     self.original.replace("staged_file: POTCAR", "staged_file: POSCAR"),
                     self.original.replace("state: RUNNING", "state: RUNNING\nstate: RUNNING"),
                     self.original.replace("state: FINISHED", ""),
                     self.original.replace("state: RUNNING", "state: RUNNING\nvalidation_failure: ignored"),
                     self.original.replace("end_utc: " + timestamp(101.8), "end_utc: NaN")):
            with self.subTest(text=text[-80:]):
                self.replace_metadata(text)
                self.assertNotEqual(self.inspect()["status"], "eligible")

    def test_changed_input_hash_size_and_native_source_disagreement_are_rejected(self):
        for name in ("INCAR", "POSCAR", "POTCAR", "KPOINTS"):
            path = self.run / name
            original = path.read_bytes()
            path.write_bytes(original + b"changed\n")
            self.assertNotEqual(self.inspect()["status"], "eligible", name)
            path.write_bytes(original)
        native = copy.deepcopy(self.native["sources"])
        next(s for s in native if s["role"] == "POSCAR")["sha256"] = "e" * 64
        self.assertNotEqual(self.inspect(native=native)["status"], "eligible")
        self.replace_metadata(self.original.replace("source_stat=(1, 2,", "source_stat=(1, -2,"))
        self.assertNotEqual(self.inspect()["status"], "eligible")
        self.replace_metadata(self.original.replace("bytes=", "bytes=1", 1))
        self.assertNotEqual(self.inspect()["status"], "eligible")

    def test_ambiguous_or_unrecognized_argv_is_rejected(self):
        changes = [lambda j: j["argv"].extend(["--output", str(self.run)]),
                   lambda j: j["argv"].append("--dry-run"),
                   lambda j: j["argv"].remove("--managed-foreground"),
                   lambda j: j["argv"].__setitem__(0, "/tmp/other-wrapper.sh"),
                   lambda j: j["argv"].__setitem__(j["argv"].index("--ranks") + 1, "64"),
                   lambda j: j["argv"].__setitem__(j["argv"].index("--timeout") + 1, "NaN"),
                   lambda j: j.__setitem__("cwd", str(self.run)),
                   lambda j: j["argv"].__setitem__(j["argv"].index("--output") + 1, str(self.root / "moved"))]
        snapshot = (self.root / self.spec["execution"]["snapshot"]).read_bytes()
        for change in changes:
            (self.root / self.spec["execution"]["snapshot"]).write_bytes(snapshot)
            self.edit_job(change)
            self.assertNotEqual(self.inspect()["status"], "eligible")

    def test_missing_native_receipt_and_nonzero_completion_remain_ineligible(self):
        receipt = self.root / self.spec["execution"]["receipt"]
        receipt.unlink()
        self.assertNotEqual(self.inspect()["status"], "eligible")
        receipt.write_text(json.dumps({"exit_code": 143, "recorded_at": 102.0}))
        self.edit_job(lambda j: j.update(status="Cancelled", exit_code=143, cancel_requested=True))
        self.assertNotEqual(self.inspect()["status"], "eligible")

    def test_copied_metadata_location_and_symlinked_inputs_are_rejected(self):
        copied = self.root / "copied.txt"
        copied.write_text(self.original)
        self.spec["execution"]["runner_record"]["path"] = str(copied)
        self.assertNotEqual(self.inspect()["status"], "eligible")
        self.spec["execution"]["runner_record"]["path"] = str(self.metadata)
        potential = self.run / "POTCAR"
        original = potential.read_bytes()
        potential.unlink()
        donor = self.root / "other"
        donor.write_bytes(original)
        potential.symlink_to(donor)
        self.assertNotEqual(self.inspect()["status"], "eligible")

    def test_binding_rejects_changed_declared_runner_record_and_missing_runtime_input(self):
        path = self.root / "spec.json"
        self.spec["execution"]["runner_record"]["sha256"] = "d" * 64
        path.write_text(json.dumps(self.spec))
        self.assertFalse(inspect_binding(path, self.native, "c" * 64)["valid"])
        self.spec["execution"]["runner_record"]["sha256"] = sha(self.metadata)
        (self.run / "KPOINTS").unlink()
        self.assertNotEqual(self.inspect()["status"], "eligible")

    def test_staging_fields_cannot_be_moved_into_terminal_block(self):
        field = "effective_incar_sha256: " + sha(self.run / "INCAR") + "\n"
        misplaced = self.original.replace(field, "").replace("state: RUNNING\n", "state: RUNNING\n" + field)
        self.replace_metadata(misplaced)
        self.assertNotEqual(self.inspect()["status"], "eligible")

    def test_sources_remain_reverifiable_after_binding(self):
        from cmw.periodic.vasp.result_sources import revalidate_sources
        result = self.inspect()
        self.assertTrue(revalidate_sources(result["sources"])["valid"])
        (self.run / "POTCAR").write_text("changed synthetic potential\n")
        self.assertFalse(revalidate_sources(result["sources"])["valid"])
