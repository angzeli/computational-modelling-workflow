from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.core.artifacts import artifact_from_dict
from cmw.jobs.store import Store
from cmw.periodic.vasp.result_finalization import (
    _record_identity, finalize_result, verify_finalization,
)
from cmw.periodic.vasp.results import inspect_result
from .result_case import digest, make_case, write_json
from .result_fixtures import write_run


class FinalizationRecordVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.case = make_case(self.root / "synthetic")
        self.record = finalize_result(self.case["run"], policy_path=self.case["policy_path"],
            spec_path=self.case["spec_path"], scratch_root=self.case["scratch"], record_directory="complete")
        self.path = Path(self.record["publication"]["record_path"])

    def edited(self, mutation, *, artifact_ids=False):
        record = deepcopy(self.record)
        mutation(record)
        if artifact_ids:
            for artifact in record["artifact_bundle"]["artifacts"]:
                artifact.pop("artifact_id", None)
                artifact["artifact_id"] = artifact_from_dict(artifact).artifact_id
        # A record hash is not authentication. Tests deliberately recompute it
        # to prove that semantic consistency is also required before reuse.
        record["record_id"] = _record_identity(record)
        write_json(self.path, record)
        return verify_finalization(self.path)

    def assert_invalid(self, result):
        self.assertIs(result["valid"], False, result)
        self.assertTrue(result["findings"], result)

    def test_valid_record_remains_compatible_and_read_only(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = verify_finalization(self.path, policy_path=self.case["policy_path"])
        self.assertTrue(result["valid"], result)
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertIsNone(result["artifacts"][0].validation.checks["ionic.relaxation"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_unavailable_parent_withholds_artifacts_but_child_source_change_remains_invalid(self):
        parent_case = make_case(self.root / "parent", relaxation=True, constrained=True)
        parent_case["state"].chmod(0o700)
        store = Store(parent_case["state"])
        job = json.loads(parent_case["snapshot"].read_text())["jobs"][0]
        with store.transaction() as conn:
            conn.execute("INSERT INTO jobs(id, data) VALUES (?, ?)", (job["id"], json.dumps(job)))
        parent = finalize_result(parent_case["run"], policy_path=parent_case["policy_path"],
            spec_path=parent_case["spec_path"], scratch_root=parent_case["scratch"], record_directory="complete")
        parent_path = parent["publication"]["record_path"]
        artifact = parent["artifact_bundle"]["artifacts"][0]
        geometry = artifact["metadata"]["periodic_geometry"]
        write_run(self.case["run"], positions=geometry["cartesian"], flags=geometry["constraints"])
        evidence = inspect_result(self.case["run"])
        self.case["spec"].update(snapshot_id=evidence["snapshot_id"], segment_id=evidence["selected_segment"]["segment_id"],
            source_identities={s["role"]: s["sha256"] for s in evidence["sources"]},
            parents=[{"record": parent_path, "sha256": digest(parent_path),
                      "artifact_id": artifact["artifact_id"], "relationship": "input-structure"}])
        write_json(self.case["spec_path"], self.case["spec"])
        child = finalize_result(self.case["run"], policy_path=self.case["policy_path"],
            spec_path=self.case["spec_path"], scratch_root=self.case["scratch"], record_directory="child")
        child_path = child["publication"]["record_path"]
        store.path.unlink()
        for status, valid, expected_exit in (("UNVERIFIABLE", None, 75), ("INVALID", False, 2)):
            with self.subTest(status=status):
                if status == "INVALID":
                    with (self.case["run"] / "OSZICAR").open("a") as output:
                        output.write("changed child source after publication\n")
                with redirect_stdout(io.StringIO()) as console:
                    exit_code = main(["vasp", "verify-result-record", child_path, "--json"])
                result = json.loads(console.getvalue())
                self.assertEqual(exit_code, expected_exit, result["findings"])
                self.assertEqual(result["status"], status)
                self.assertIs(result["valid"], valid)
                self.assertEqual(result["artifacts"], [])
                if status == "INVALID":
                    self.assertTrue(any("OSZICAR" in diff["field"] for diff in result["differences"]))

    def test_exact_schema_kind_and_complete_publication_are_required(self):
        for mutation in (
            lambda r: r.update(schema_version=True),
            lambda r: r.update(kind="other-kind"),
            lambda r: r.update(publication=[]),
            lambda r: r["publication"].update(state="incomplete"),
            lambda r: r["artifact_bundle"].update(artifact_count=True),
        ):
            with self.subTest(mutation=mutation):
                self.assert_invalid(self.edited(mutation))

    def test_stored_policy_and_assessment_cannot_be_relabelled(self):
        for mutation in (
            lambda r: r["validation_decision"].update(status="FAIL"),
            lambda r: r["policy_identity"].update(policy_id="a" * 64),
            lambda r: r["evidence"]["policy"].update(name="different-policy"),
            lambda r: r["evidence"].update(policy_assessment=None),
            lambda r: r["evidence"]["policy"]["electronic"].update(history="unimplemented"),
            lambda r: r["evidence"]["selected_segment"]["evaluations"][-1].update(native_convergence=False),
        ):
            with self.subTest(mutation=mutation):
                self.assert_invalid(self.edited(mutation))

    def test_execution_and_binding_facts_must_agree_with_actual_context(self):
        for mutation in (
            lambda r: r["evidence"]["binding"].update(valid=False),
            lambda r: r["evidence"]["binding"].update(input_comparison_valid=False),
            lambda r: r["evidence"]["binding"]["execution"].update(status="unknown"),
            lambda r: r["evidence"]["binding"]["execution"].update(exit_code=7),
            lambda r: r["evidence"]["binding"]["execution"]["job"].update(attempt_id="b" * 32),
            lambda r: r["evidence"]["binding"]["spec"].update(producing_calculation="different-run"),
            lambda r: r["evidence"]["binding"]["resolved_spec"].update(parents=[{}]),
        ):
            with self.subTest(mutation=mutation):
                self.assert_invalid(self.edited(mutation))

    def test_required_source_closure_cannot_omit_or_duplicate_evidence(self):
        for mutation in (
            lambda r: r.update(required_sources=r["required_sources"][1:]),
            lambda r: r["required_sources"].append(deepcopy(r["required_sources"][0])),
            lambda r: r["required_sources"][0].update(sha256="f" * 64),
            lambda r: r["evidence"]["binding"].update(sources=[]),
            lambda r: r["evidence"]["sources"][0].update(bytes_read=0),
        ):
            with self.subTest(mutation=mutation):
                self.assert_invalid(self.edited(mutation))

    def test_artifact_protocol_and_identity_must_bind_the_observed_run(self):
        for key, value in (("source_snapshot_id", "a" * 64), ("segment_id", "b" * 64),
                           ("policy_id", "c" * 64), ("operational_attempt_id", "d" * 32),
                           ("evaluation_index", 99)):
            with self.subTest(key=key):
                self.assert_invalid(self.edited(
                    lambda r: r["artifact_bundle"]["artifacts"][0]["protocol"].update({key: value}),
                    artifact_ids=True))
        self.assert_invalid(self.edited(
            lambda r: r["artifact_bundle"]["artifacts"][0].update(producing_calculation="other"), artifact_ids=True))

    def test_artifact_role_energy_files_and_parent_coverage_are_checked(self):
        mutations = (
            lambda a: a["metadata"].update(role="energy:free_energy"),
            lambda a: a["metadata"]["energy"].update(value=123),
            lambda a: a["metadata"].update(periodic_geometry_id="e" * 64),
            lambda a: a.update(files={"OUTCAR": "/invented/unrelated"}),
            lambda a: a.update(parent_artifacts=["f" * 64]),
            lambda a: a["validation"].update(checks={"assumed_success": True}),
            lambda a: a["provenance"].update(source_identities=[]),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assert_invalid(self.edited(lambda r: mutation(r["artifact_bundle"]["artifacts"][0]), artifact_ids=True))

    def test_malformed_shapes_and_missing_sources_are_structured_invalid(self):
        for field, value in (("evidence", None), ("artifact_bundle", []),
                             ("required_sources", [None]), ("required_sources", {})):
            with self.subTest(field=field):
                self.assert_invalid(self.edited(lambda r: r.update({field: value})))
        self.assert_invalid(verify_finalization(self.root / "missing.json"))
        self.path.write_text("[]")
        self.assert_invalid(verify_finalization(self.path))

    def test_revalidation_errors_never_escape_or_return_success(self):
        for returned in (None, [], {"valid": False}, {"valid": 1}):
            with self.subTest(returned=returned), patch(
                "cmw.periodic.vasp.result_finalization.revalidate_sources", return_value=returned,
            ):
                self.assert_invalid(verify_finalization(self.path))
        with patch("cmw.periodic.vasp.result_finalization.revalidate_sources", side_effect=RuntimeError("invented read failure")):
            self.assert_invalid(verify_finalization(self.path))


if __name__ == "__main__":
    unittest.main()
