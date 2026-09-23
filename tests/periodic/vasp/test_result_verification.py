from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.core.artifacts import artifact_from_dict
from cmw.periodic.vasp.result_finalization import (
    _record_identity, finalize_result, verify_finalization,
)
from .result_case import make_case, write_json


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
        self.assertFalse(result["valid"], result)
        self.assertTrue(result["findings"], result)

    def test_valid_record_remains_compatible_and_read_only(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = verify_finalization(self.path, policy_path=self.case["policy_path"])
        self.assertTrue(result["valid"], result)
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertIsNone(result["artifacts"][0].validation.checks["ionic.relaxation"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

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
