from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.result_binding import inspect_binding
from cmw.periodic.vasp.results import inspect_result
from .result_fixtures import write_run


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_execution(root, run, *, attempt="a" * 32):
    """Invent native Jobs records without opening or modifying an actual queue."""
    state = root / "invented-state"
    attempt_dir = state / "attempts" / attempt
    attempt_dir.mkdir(parents=True, exist_ok=True)
    snapshot = root / "jobs-snapshot.json"
    receipt = attempt_dir / "payload-exit.json"
    receipt.write_text(json.dumps({"exit_code": 0, "recorded_at": 102.0}))
    snapshot.write_text(json.dumps({"schema": 2, "state_directory": str(state), "observed_at": 104.0,
                                   "jobs": [{"id": 7, "attempt_id": attempt, "engine": "Command",
                                             "argv": ["synthetic-runner"], "cwd": str(run),
                                             "status": "Done", "started_at": 100.0, "finished_at": 103.0,
                                             "exit_code": 0, "signal": None, "cancel_requested": False,
                                             "logs": {stream: str(attempt_dir / f"{stream}.log")
                                                      for stream in ("stdout", "stderr")}}]}))
    return {"snapshot": str(snapshot.relative_to(root)), "receipt": str(receipt.relative_to(root)),
            "job_id": 7, "attempt_id": attempt,
            "source_identities": {"jobs_snapshot": file_sha(snapshot), "jobs_receipt": file_sha(receipt)}}


def make_spec(root, evidence, policy_id="c" * 64):
    return {"schema_version": 1, "intent": "retrospective", "producing_calculation": "invented static case",
            "snapshot_id": evidence["snapshot_id"], "segment_id": evidence["selected_segment"]["segment_id"],
            "policy_id": policy_id, "source_identities": {s["role"]: s["sha256"] for s in evidence["sources"]},
            "execution": write_execution(root, Path(evidence["run_directory"])),
            "artifact_roles": ["energy:free_energy", "periodic-structure"]}


class ResultBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = write_run(self.root / "run", flags=((True, True, True), (False, False, False)))
        self.evidence = inspect_result(self.run)
        self.policy_id = "c" * 64
        self.spec = make_spec(self.root, self.evidence, self.policy_id)
        self.spec_path = self.root / "finalization-spec.json"

    def inspect(self, spec=None, evidence=None, policy_id=None):
        self.spec_path.write_text(json.dumps(self.spec if spec is None else spec))
        return inspect_binding(self.spec_path, self.evidence if evidence is None else evidence,
                               self.policy_id if policy_id is None else policy_id)

    def codes(self, result):
        return {item["code"] for item in result["findings"]}

    def refresh(self):
        self.evidence = inspect_result(self.run)
        self.spec["snapshot_id"] = self.evidence["snapshot_id"]
        self.spec["segment_id"] = self.evidence["selected_segment"]["segment_id"]
        self.spec["source_identities"] = {s["role"]: s["sha256"] for s in self.evidence["sources"]}

    def prepare_record(self):
        record = {"schema_version": 1, "record_kind": "vasp-input-preparation",
                  "publication": {"state": "complete"},
                  "prepared_inputs": {"POSCAR": {"sha256": file_sha(self.run / "POSCAR")}},
                  "resolved_settings": {"incar": copy.deepcopy(self.evidence["filesystem_inputs"]["INCAR"]["settings"])}}
        path = self.root / "preparation.json"
        path.write_text(json.dumps(record))
        self.spec["preparation_record"] = {"path": path.name, "sha256": file_sha(path)}
        return path, record

    def test_exact_retrospective_binding_is_eligible_without_fabricated_preplan(self):
        result = self.inspect()
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["input_binding"])
        self.assertTrue(result["input_comparison_valid"])
        self.assertEqual(result["execution"]["status"], "eligible")
        self.assertEqual(result["spec"], self.spec)
        self.assertEqual(result["spec"]["intent"], "retrospective")
        self.assertFalse(Path(result["spec"]["execution"]["snapshot"]).is_absolute())
        self.assertTrue(Path(result["resolved_spec"]["execution"]["snapshot"]).is_absolute())
        self.assertEqual({s["role"] for s in result["sources"]}, {"finalization_spec", "jobs_snapshot", "jobs_receipt"})
        self.assertIn("not authenticated pre-run", result["limitations"][0])

    def test_read_only_binding_does_not_read_potential_or_create_payloads(self):
        (self.run / "POTCAR").write_bytes(b"not a usable potential; must remain unread")
        self.spec_path.write_text(json.dumps(self.spec))
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = inspect_binding(self.spec_path, self.evidence, self.policy_id)
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(result["valid"])
        self.assertEqual(before, after)
        self.assertNotIn("POTCAR", {s["role"] for s in result["sources"]})

    def test_unknown_missing_and_wrong_spec_fields_fail(self):
        changes = [{"schema_version": True}, {"schema_version": 2}, {"intent": "preplanned"},
                   {"producing_calculation": " "}, {"policy_id": "short"}, {"assume_finished": True},
                   {"artifact_roles": []}, {"artifact_roles": ["wavefunction"]},
                   {"artifact_roles": ["periodic-structure", "periodic-structure"]}]
        for change in changes:
            with self.subTest(change=change):
                result = self.inspect(self.spec | change)
                self.assertFalse(result["valid"])
                self.assertIn("binding.specification", self.codes(result))
        missing = dict(self.spec)
        del missing["source_identities"]
        self.assertIn("binding.specification", self.codes(self.inspect(missing)))

    def test_duplicate_nonfinite_and_missing_spec_sources_fail(self):
        for text in ('{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}'):
            self.spec_path.write_text(text)
            self.assertFalse(inspect_binding(self.spec_path, self.evidence, self.policy_id)["valid"])
        self.assertFalse(inspect_binding(self.root / "missing", self.evidence, self.policy_id)["valid"])

    def test_exact_snapshot_segment_policy_and_native_role_set_required(self):
        for key in ("snapshot_id", "segment_id", "policy_id"):
            result = self.inspect(self.spec | {key: "d" * 64})
            self.assertFalse(result["valid"])
        for edit in (lambda ids: ids.pop("OUTCAR"), lambda ids: ids.update(extra="d" * 64),
                     lambda ids: ids.update(INCAR="d" * 64)):
            spec = copy.deepcopy(self.spec)
            edit(spec["source_identities"])
            result = self.inspect(spec)
            self.assertFalse(result["valid"])
            self.assertIn("binding.source_identities", self.codes(result))

    def test_execution_hashes_attempt_and_directory_must_match(self):
        for key, value in (("job_id", True), ("attempt_id", "not-uuid"), ("assume_finished", True)):
            spec = copy.deepcopy(self.spec)
            spec["execution"][key] = value
            self.assertIn("binding.specification", self.codes(self.inspect(spec)))
        spec = copy.deepcopy(self.spec)
        spec["execution"]["source_identities"]["jobs_snapshot"] = "d" * 64
        self.assertIn("binding.execution_source_identities", self.codes(self.inspect(spec)))
        evidence = copy.deepcopy(self.evidence)
        evidence["run_directory"] = str(self.root)
        self.assertIn("execution.working_directory", self.codes(self.inspect(evidence=evidence)))

    def test_missing_execution_evidence_never_establishes_success(self):
        spec = copy.deepcopy(self.spec)
        spec["execution"]["receipt"] = "missing.json"
        result = self.inspect(spec)
        self.assertFalse(result["valid"])
        self.assertEqual(result["execution"]["status"], "unknown")

    def test_changed_or_incomplete_sources_and_missing_input_binding_fail(self):
        for edit in (lambda e: e["sources"][0].update(stable=False),
                     lambda e: e["sources"][0].update(coverage="incomplete"),
                     lambda e: e["filesystem_inputs"]["POSCAR"].update(status="unsupported"),
                     lambda e: e["endpoint"]["comparisons"]["initial_poscar"].update(status="mismatch"),
                     lambda e: e["constraints"].update(flags=[[True, True, True]])):
            evidence = copy.deepcopy(self.evidence)
            edit(evidence)
            result = self.inspect(evidence=evidence)
            self.assertFalse(result["valid"])
            self.assertFalse(result["input_binding"])
        (self.run / "INCAR").unlink()
        self.refresh()
        result = self.inspect()
        self.assertFalse(result["valid"])
        self.assertIn("binding.input_sources", self.codes(result))

    def test_initial_geometry_match_is_required_even_when_hashes_are_declared(self):
        path = self.run / "POSCAR"
        path.write_text(path.read_text().replace("0.5000000000", "0.7000000000"))
        self.refresh()
        result = self.inspect()
        self.assertFalse(result["input_binding"])
        self.assertIn("binding.initial_geometry", self.codes(result))

    def test_unexpected_scientific_setting_change_fails(self):
        path = self.run / "INCAR"
        path.write_text(path.read_text().replace("ENCUT = 300", "ENCUT = 400"))
        self.refresh()
        result = self.inspect()
        self.assertFalse(result["input_comparison_valid"])
        self.assertFalse(result["valid"])
        row = next(row for row in result["input_comparisons"] if row["tag"] == "ENCUT")
        self.assertEqual(row["filesystem"], 400)
        self.assertEqual(row["effective"], 300)
        self.assertEqual(row["status"], "conflict")

    def test_explicit_runtime_overlay_requires_observed_exact_value(self):
        write_run(self.run, settings={"NCORE": "2", "KPAR": "1"})
        path = self.run / "INCAR"
        path.write_text(path.read_text().replace("NCORE = 2", "NCORE = 1"))
        self.refresh()
        self.assertFalse(self.inspect()["valid"])
        self.spec["expected_runtime_overlay"] = {"NCORE": 2}
        result = self.inspect()
        self.assertTrue(result["valid"], result)
        row = next(row for row in result["input_comparisons"] if row["tag"] == "NCORE")
        self.assertEqual(row["status"], "declared_runtime_overlay")
        self.spec["expected_runtime_overlay"] = {"NCORE": 3}
        self.assertFalse(self.inspect()["valid"])
        self.spec["expected_runtime_overlay"] = {"NPAR": 2}
        self.assertIn("binding.specification", self.codes(self.inspect()))
        for count in (True, 0, 1.5):
            self.spec["expected_runtime_overlay"] = {"NCORE": count}
            self.assertIn("binding.specification", self.codes(self.inspect()))

    def test_overlay_cannot_invent_unobserved_effective_setting(self):
        self.spec["expected_runtime_overlay"] = {"NCORE": 2}
        self.assertFalse(self.inspect()["input_comparison_valid"])

    def test_uncaptured_settings_remain_explicitly_unassessed(self):
        path = self.run / "INCAR"
        path.write_text(path.read_text() + "PREC = Normal\n")
        self.refresh()
        result = self.inspect()
        self.assertTrue(result["valid"], result)
        row = next(row for row in result["input_comparisons"] if row["tag"] == "PREC")
        self.assertEqual(row["status"], "unassessed")
        self.assertIsNone(row["effective"])

    def test_optional_preparation_binds_exact_record_and_poscar(self):
        path, record = self.prepare_record()
        result = self.inspect()
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["preparation"]["potentials"], "unassessed")
        self.assertEqual(result["preparation"]["kpoints"], "unassessed")
        self.assertIn("preparation_record", {s["role"] for s in result["sources"]})
        self.spec["preparation_record"]["sha256"] = "d" * 64
        self.assertIn("binding.preparation_identity", self.codes(self.inspect()))
        record["prepared_inputs"]["POSCAR"]["sha256"] = "d" * 64
        path.write_text(json.dumps(record))
        self.spec["preparation_record"]["sha256"] = file_sha(path)
        self.assertIn("binding.preparation_poscar", self.codes(self.inspect()))

    def test_preparation_schema_and_incar_differences_fail(self):
        path, original = self.prepare_record()
        mutations = [lambda r: r.update(schema_version=True), lambda r: r.update(record_kind="other"),
                     lambda r: r["publication"].update(state="intent"),
                     lambda r: r["resolved_settings"]["incar"].update(ENCUT=400),
                     lambda r: r["resolved_settings"]["incar"].update(ISPIN=True),
                     lambda r: r["resolved_settings"]["incar"].update(FAKE="not-current")]
        for mutation in mutations:
            record = copy.deepcopy(original)
            mutation(record)
            path.write_text(json.dumps(record))
            self.spec["preparation_record"]["sha256"] = file_sha(path)
            self.assertFalse(self.inspect()["valid"])

    def test_prepared_runtime_overlay_is_explicit_and_limited(self):
        write_run(self.run, settings={"NCORE": "2"})
        self.refresh()
        path, record = self.prepare_record()
        record["resolved_settings"]["incar"]["NCORE"] = 1
        path.write_text(json.dumps(record))
        self.spec["preparation_record"]["sha256"] = file_sha(path)
        self.assertFalse(self.inspect()["valid"])
        self.spec["expected_runtime_overlay"] = {"NCORE": 2}
        result = self.inspect()
        self.assertTrue(result["valid"], result)
        row = next(row for row in result["preparation"]["comparisons"] if row["tag"] == "NCORE")
        self.assertEqual(row["status"], "declared_runtime_overlay")

    def test_parent_declarations_are_validated_but_not_read_here(self):
        parent = {"record": "not-loaded-here.json", "sha256": "d" * 64,
                  "artifact_id": "e" * 64, "relationship": "input-structure"}
        self.spec["parents"] = [parent]
        result = self.inspect()
        self.assertTrue(result["valid"])
        self.assertEqual(result["spec"]["parents"], [parent])
        self.assertTrue(Path(result["resolved_spec"]["parents"][0]["record"]).is_absolute())
        for change in ({"relationship": "invented"}, {"artifact_id": "short"}, {"extra": True}):
            self.spec["parents"] = [parent | change]
            self.assertIn("binding.specification", self.codes(self.inspect()))
        self.spec["parents"] = [parent, parent]
        self.assertIn("binding.specification", self.codes(self.inspect()))


if __name__ == "__main__":
    unittest.main()
