from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.core.artifacts import EnergyArtifact
from cmw.core.preparation_publication import PublicationError
from cmw.periodic.vasp.result_finalization import ResultFinalizationError
from .result_fixtures import native_texts, write_run


class ResultCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = write_run(self.root / "run")
        self.policy = self.root / "policy.json"
        self.spec = self.root / "spec.json"
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    def call(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output), patch("subprocess.Popen", side_effect=AssertionError("must not launch")), \
                patch("cmw.jobs.store.Store", side_effect=AssertionError("must not open mutable Jobs state")):
            code = main(["vasp", *map(str, arguments)])
        return code, output.getvalue()

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def policy_file(self):
        value = {"schema_version": 1, "name": "invented diagnostic static policy", "calculation": "static",
                 "electronic": {"history": "all", "basis": "native-and-numeric", "criterion": "run-ediff"},
                 "force": None, "endpoint": {"position_tolerance_angstrom": 2e-5, "cell_tolerance_angstrom": 2e-6},
                 "require_execution": False, "require_input_binding": False, "artifact_roles": ["energy:free_energy"]}
        self.policy.write_text(json.dumps(value))

    def test_human_summary_handles_unavailable_constraint_source(self):
        (self.run / "POSCAR").unlink()
        code, output = self.call("inspect-result", self.run)
        self.assertEqual(code, 0)
        self.assertIn("Force free: unavailable constraint evidence", output)

    def test_json_read_only_inspection_and_native_nonconvergence_exit(self):
        before = self.snapshot()
        code, output = self.call("inspect-result", self.run, "--json")
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "inspected")
        self.assertIsNone(result["policy_assessment"])
        self.assertIsNone(result["execution"]["exit_code"])
        self.assertEqual(before, self.snapshot())
        write_run(self.run, native=False, terminal_de="-.1")
        code, output = self.call("inspect-result", self.run, "--json")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["selected_segment"]["evaluations"][0]["native_convergence"])

    def test_readable_summary_keeps_counts_exit_and_force_scope_distinct(self):
        write_run(self.run, relaxation=True, flags=((True,) * 3, (False,) * 3))
        code, output = self.call("inspect-result", self.run)
        self.assertEqual(code, 0)
        for text in ("Mode: fixed-cell-relaxation; VASP: 6.6.1", "Selected segment: 0",
                     "Source snapshot:", "Evaluations: 1; complete: 1", "Native electronic convergence: yes 1",
                     "Numeric electronic convergence: PASS 1", "Normal terminal output: observed",
                     "Process exit: unobserved", "Force free: 1 atoms", "Force fixed: 1 atoms",
                     "endpoint_contcar: match"):
            self.assertIn(text, output)
        self.assertNotIn("Policy ", output)

    def test_invalid_unsupported_and_selection_have_distinct_exit_codes(self):
        code, output = self.call("inspect-result", self.root / "missing", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["status"], "invalid")
        write_run(self.run, version="5.4.4")
        code, output = self.call("inspect-result", self.run, "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["status"], "unsupported")
        write_run(self.run)
        second = native_texts()
        for role in ("OUTCAR", "OSZICAR"):
            with (self.run / role).open("a") as stream:
                stream.write(second[role])
        code, output = self.call("inspect-result", self.run)
        self.assertEqual(code, 2)
        self.assertIn("Select a zero-based --segment", output)
        code, output = self.call("inspect-result", self.run, "--segment", 1, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["selected_segment"]["index"], 1)

    def test_explicit_stdout_and_bounded_inspection(self):
        stdout = self.run / "stdout.log"
        (self.run / "OSZICAR").rename(stdout)
        code, output = self.call("inspect-result", self.run, "--stdout", stdout, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["electronic_table_correspondence"], "matched")
        _, output = self.call("inspect-result", self.run, "--max-bytes", 100, "--json")
        self.assertFalse(json.loads(output)["coverage"]["all_sources_complete"])

    def test_nonfinite_input_still_emits_strict_json(self):
        write_run(self.run, terminal_de="NaN")
        _, output = self.call("inspect-result", self.run, "--json")
        def reject_constant(value):
            raise AssertionError(f"Not strict JSON: {value}")
        result = json.loads(output, parse_constant=reject_constant)
        self.assertIsNone(result["selected_segment"]["evaluations"][0]["final_electronic_row"]["dE"]["value"])

    def test_explicit_policy_changes_assessment_not_sources_and_writes_nothing(self):
        self.policy_file()
        before = self.snapshot()
        code, output = self.call("inspect-result", self.run, "--policy", self.policy, "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["policy_assessment"]["status"], "PASS")
        self.assertEqual(before, self.snapshot())
        code, output = self.call("inspect-result", self.run, "--policy", self.policy)
        self.assertEqual(code, 0)
        self.assertIn("Policy invented diagnostic static policy: PASS", output)
        write_run(self.run, native=False, terminal_de="-.1")
        code, output = self.call("inspect-result", self.run, "--policy", self.policy, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["policy_assessment"]["status"], "FAIL")

    def test_spec_without_policy_and_malformed_policy_are_structured_errors(self):
        for args in (("--spec", self.spec), ("--policy", self.policy)):
            code, output = self.call("inspect-result", self.run, *args, "--json")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["status"], "error")
        self.policy.write_text('{"arbitrary": true}')
        code, output = self.call("inspect-result", self.run, "--policy", self.policy, "--json")
        self.assertEqual(code, 2)
        self.assertIn("policy", json.loads(output)["message"].lower())

    def finalize_args(self):
        return ("finalize-result", self.run, "--policy", self.policy, "--spec", self.spec,
                "--scratch-root", self.scratch, "--record-directory", "new-record")

    def test_finalization_options_are_forwarded_to_existing_adapter(self):
        record = {"record_id": "invented", "publication": {"state": "preview", "record_path": "invented/finalization.json"},
                  "artifact_bundle": {"artifact_count": 1}, "policy_identity": {"name": "invented"},
                  "validation_decision": {"status": "PASS"}}
        before = self.snapshot()
        with patch("cmw.periodic.vasp.result_finalization.finalize_result", return_value=record) as call:
            code, output = self.call(*self.finalize_args(), "--segment", 0, "--stdout", "native.log",
                                     "--scratch-mount", self.root, "--dry-run", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), record)
        self.assertEqual(call.call_args.args, (self.run,))
        self.assertEqual(call.call_args.kwargs, {"policy_path": self.policy, "spec_path": self.spec,
                         "scratch_root": self.scratch, "record_directory": Path("new-record"),
                         "scratch_mount": self.root, "segment": 0, "stdout": Path("native.log"), "dry_run": True})
        self.assertEqual(before, self.snapshot())

    def test_finalization_refusal_and_publication_failure_retain_details(self):
        for error in (ResultFinalizationError("synthetic refusal", findings=[{"code": "missing-binding"}]),
                      PublicationError("synthetic disk failure", code="PUBLICATION_INCOMPLETE", record_path="owned/finalization.json", incomplete=True)):
            with self.subTest(error=error), patch("cmw.periodic.vasp.result_finalization.finalize_result", side_effect=error):
                code, output = self.call(*self.finalize_args(), "--json")
            self.assertEqual(code, 2)
            result = json.loads(output)
            self.assertEqual(result["code"], error.code)
            if isinstance(error, PublicationError):
                self.assertTrue(result["incomplete"])
                self.assertEqual(result["record_path"], error.record_path)
            else:
                self.assertEqual(result["findings"], error.findings)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_verify_serializes_typed_artifacts_and_rejects_incomplete_record(self):
        artifact = EnergyArtifact("invented-producing-calculation")
        with patch("cmw.periodic.vasp.result_finalization.verify_finalization", return_value={"valid": True, "findings": [], "artifacts": [artifact]}) as call:
            code, output = self.call("verify-result-record", self.spec, "--policy", self.policy, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["artifacts"][0], artifact.to_dict())
        call.assert_called_once_with(self.spec, policy_path=self.policy)
        self.spec.write_text('{"schema_version": 1, "kind": "vasp-artifact-finalization", "publication": {"state": "intent"}}')
        before = self.snapshot()
        code, output = self.call("verify-result-record", self.spec, "--json")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output)["valid"])
        self.assertEqual(before, self.snapshot())

    def test_finalization_requires_all_explicit_arguments(self):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as error:
            main(["vasp", "finalize-result", str(self.run)])
        self.assertEqual(error.exception.code, 2)
        for option in ("--policy", "--spec", "--scratch-root", "--record-directory"):
            self.assertIn(option, output.getvalue())


if __name__ == "__main__":
    unittest.main()
