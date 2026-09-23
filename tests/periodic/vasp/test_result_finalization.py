from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.core import artifact_finalization as core
from cmw.core.artifacts import EnergyArtifact, StructureArtifact, artifact_from_dict
from cmw.core.preparation_publication import PublicationError
from cmw.core.provenance import stable_hash
from cmw.periodic.vasp.result_finalization import (ResultFinalizationError, finalize_result,
    inspect_with_policy, verify_finalization)
from cmw.periodic.vasp.results import inspect_result
from .result_case import make_case, write_json, digest
from .result_fixtures import structure_text


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.case = make_case(self.root / "case")

    def finalize(self, case=None, **options):
        c = case or self.case
        return finalize_result(c["run"], policy_path=c["policy_path"], spec_path=c["spec_path"],
                               scratch_root=c["scratch"], record_directory=options.pop("record_directory", "result"), **options)

    def refresh(self, c):
        e = inspect_result(c["run"])
        c["spec"].update(snapshot_id=e["snapshot_id"], segment_id=e["selected_segment"]["segment_id"],
                         source_identities={s["role"]: s["sha256"] for s in e["sources"]}, policy_id=stable_hash(c["policy"]))
        for role, key in (("jobs_snapshot", "snapshot"), ("jobs_receipt", "receipt")):
            c["spec"]["execution"]["source_identities"][role] = digest(c[key])
        write_json(c["policy_path"], c["policy"])
        write_json(c["spec_path"], c["spec"])

    def test_actual_core_finalizer_and_round_trip_energy(self):
        with patch.object(core, "finalize_artifact_bundle", wraps=core.finalize_artifact_bundle) as finalizer:
            record = self.finalize()
        finalizer.assert_called_once()
        self.assertEqual(record["publication"]["state"], "complete")
        artifact = artifact_from_dict(record["artifact_bundle"]["artifacts"][0])
        self.assertIsInstance(artifact, EnergyArtifact)
        self.assertEqual(artifact.metadata["energy_kind"], "sigma_to_zero")
        self.assertEqual(artifact.metadata["energy"]["value"], -2.995)
        self.assertTrue(artifact.validation.passed)
        self.assertIsNone(artifact.basis)
        self.assertEqual(record["artifact_bundle"]["artifact_count"], 1)
        self.assertEqual([a["artifact_type"] for a in record["artifact_bundle"]["artifacts"]], ["EnergyArtifact"])
        self.assertTrue(verify_finalization(record["publication"]["record_path"])["valid"])

    def test_whole_atom_constrained_endpoint_round_trips(self):
        c = make_case(self.root / "constrained", relaxation=True, constrained=True)
        record = self.finalize(c)
        artifact = artifact_from_dict(record["artifact_bundle"]["artifacts"][0])
        self.assertIsInstance(artifact, StructureArtifact)
        self.assertIsNone(artifact.charge)
        self.assertIsNone(artifact.multiplicity)
        geometry = artifact.metadata["periodic_geometry"]
        self.assertEqual(geometry["constraints"], [[True]*3, [False]*3])
        self.assertEqual(geometry["cartesian"][0][0], .54)
        self.assertEqual(record["evidence"]["endpoint"]["force_summary"]["fixed"]["maximum_norm"], .5)

    def test_inspect_policy_and_dry_run_never_write_or_launch(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch("subprocess.Popen", side_effect=AssertionError("no launch")), patch("cmw.jobs.store.Store", side_effect=AssertionError("no Jobs")):
            evidence = inspect_with_policy(self.case["run"], policy_path=self.case["policy_path"], spec_path=self.case["spec_path"])
            self.assertEqual(evidence["policy_assessment"]["status"], "PASS")
            record = self.finalize(dry_run=True)
        self.assertEqual(record["publication"]["state"], "preview")
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(set(p.name for p in self.case["inputs"].iterdir()), {"INCAR","POSCAR","KPOINTS","POTCAR"})

    def test_preparation_is_unchanged_and_its_input_directory_protected(self):
        from cmw.periodic.vasp.inputs import parse_incar
        prep_path = self.root / "preparation.json"
        record = {"schema_version":1, "record_kind":"vasp-input-preparation",
                  "publication":{"state":"complete", "input_path":str(self.case["inputs"])},
                  "prepared_inputs":{"POSCAR":{"sha256":digest(self.case["run"]/"POSCAR")}},
                  "resolved_settings":{"incar":parse_incar((self.case["run"]/"INCAR").read_text())["settings"]}}
        write_json(prep_path, record)
        original = prep_path.read_bytes()
        self.case["spec"]["preparation_record"] = {"path":str(prep_path), "sha256":digest(prep_path)}
        write_json(self.case["spec_path"], self.case["spec"])
        self.finalize()
        self.assertEqual(original, prep_path.read_bytes())
        with self.assertRaises(PublicationError):
            finalize_result(self.case["run"], policy_path=self.case["policy_path"], spec_path=self.case["spec_path"],
                scratch_root=self.case["inputs"], record_directory="invalid-nesting")
        self.assertEqual(set(p.name for p in self.case["inputs"].iterdir()), {"INCAR","POSCAR","KPOINTS","POTCAR"})

    def test_scratch_only_no_clobber_sources_unchanged(self):
        before = {p: p.read_bytes() for p in self.case["run"].iterdir()}
        record = self.finalize()
        self.assertEqual(set(p.name for p in (self.case["scratch"]/"result").iterdir()), {"finalization.json"})
        self.assertEqual(before, {p:p.read_bytes() for p in self.case["run"].iterdir()})
        with self.assertRaises(PublicationError): self.finalize()
        with self.assertRaises(PublicationError):
            finalize_result(self.case["run"], policy_path=self.case["policy_path"], spec_path=self.case["spec_path"], scratch_root=self.root/"missing", record_directory="record")

    def test_wrong_attempt_source_policy_or_missing_terminal_refuse(self):
        for field in ("snapshot_id", "segment_id", "policy_id"):
            saved = self.case["spec"][field]
            self.case["spec"][field] = "0"*64
            write_json(self.case["spec_path"], self.case["spec"])
            with self.assertRaises(ResultFinalizationError): self.finalize()
            self.case["spec"][field] = saved
        write_json(self.case["spec_path"], self.case["spec"])
        self.case["receipt"].unlink()
        with self.assertRaises(ResultFinalizationError): self.finalize()
        self.assertFalse((self.case["scratch"]/"result").exists())

    def test_permissive_policy_cannot_bypass_finalization_execution(self):
        self.case["policy"].update(require_execution=False, require_input_binding=False)
        self.refresh(self.case)
        self.case["spec"]["execution"]["attempt_id"] = "b"*32
        write_json(self.case["spec_path"], self.case["spec"])
        with self.assertRaises(ResultFinalizationError): self.finalize()

    def test_stale_endpoint_and_unsupported_role_refuse(self):
        c = make_case(self.root/"relax", relaxation=True, constrained=True)
        (c["run"]/"CONTCAR").write_text(structure_text())
        self.refresh(c)
        with self.assertRaises(ResultFinalizationError): self.finalize(c)
        self.case["spec"]["artifact_roles"] = ["wavefunction"]
        write_json(self.case["spec_path"], self.case["spec"])
        with self.assertRaises(ResultFinalizationError): self.finalize()

    def test_core_partial_failure_publishes_nothing(self):
        fail = core.ArtifactBundleFinalizationError([core.ArtifactFinalizationFailure("invented", "FAILED", "invented failure")])
        with patch.object(core, "finalize_artifact_bundle", side_effect=fail):
            with self.assertRaises(ResultFinalizationError): self.finalize()
        self.assertEqual(list(self.case["scratch"].iterdir()), [])

    def test_changed_source_or_policy_invalidates_reuse(self):
        record = self.finalize()
        path = record["publication"]["record_path"]
        self.case["policy_path"].write_text(self.case["policy_path"].read_text()+" ")
        self.assertFalse(verify_finalization(path)["valid"])
        other = make_case(self.root/"other")
        record2 = self.finalize(other)
        (other["run"]/"OUTCAR").write_text((other["run"]/"OUTCAR").read_text()+"\n")
        self.assertFalse(verify_finalization(record2["publication"]["record_path"])["valid"])

    def test_artifact_identity_changes_for_attempt_or_policy(self):
        original = self.finalize(dry_run=True)["artifact_bundle"]["artifacts"][0]["artifact_id"]
        other = make_case(self.root/"another", attempt="b"*32)
        changed = self.finalize(other, dry_run=True)["artifact_bundle"]["artifacts"][0]["artifact_id"]
        self.assertNotEqual(original, changed)
        self.case["policy"]["name"] = "another-explicit-policy"
        self.refresh(self.case)
        policy_changed = self.finalize(dry_run=True)["artifact_bundle"]["artifacts"][0]["artifact_id"]
        self.assertNotEqual(original, policy_changed)

    def test_parent_identity_geometry_and_required_sources(self):
        parent_case = make_case(self.root/"parent", relaxation=True, constrained=True)
        parent = self.finalize(parent_case)
        parent_path = Path(parent["publication"]["record_path"])
        parent_artifact = parent["artifact_bundle"]["artifacts"][0]
        # The child is evaluated at its original geometry, not this relaxed parent.
        self.case["spec"]["parents"] = [{"record":str(parent_path), "sha256":digest(parent_path),
            "artifact_id":parent_artifact["artifact_id"], "relationship":"input-structure"}]
        write_json(self.case["spec_path"], self.case["spec"])
        with self.assertRaises(ResultFinalizationError): self.finalize()
        self.case["spec"]["parents"][0]["artifact_id"] = "f"*64
        write_json(self.case["spec_path"], self.case["spec"])
        with self.assertRaises(ResultFinalizationError): self.finalize()

    def test_valid_required_parent_is_checked_and_retained(self):
        from .result_fixtures import write_run
        parent_case = make_case(self.root/"valid-parent", relaxation=True, constrained=True)
        parent = self.finalize(parent_case)
        parent_path = Path(parent["publication"]["record_path"])
        artifact = parent["artifact_bundle"]["artifacts"][0]
        geometry = artifact["metadata"]["periodic_geometry"]
        write_run(self.case["run"], positions=geometry["cartesian"], flags=geometry["constraints"])
        self.case["spec"]["parents"] = [{"record": str(parent_path), "sha256": digest(parent_path),
            "artifact_id": artifact["artifact_id"], "relationship": "input-structure"}]
        self.refresh(self.case)
        child = self.finalize()
        self.assertEqual(child["artifact_bundle"]["artifacts"][0]["parent_artifacts"], [artifact["artifact_id"]])
        self.assertTrue(verify_finalization(child["publication"]["record_path"])["valid"])
        (parent_case["run"]/"OUTCAR").write_text("changed parent")
        self.assertFalse(verify_finalization(child["publication"]["record_path"])["valid"])

    def test_source_revalidation_failure_preserves_failed_record(self):
        with patch("cmw.periodic.vasp.result_finalization.revalidate_sources", return_value={"valid":False,"findings":[]}):
            with self.assertRaises(PublicationError): self.finalize()
        path = self.case["scratch"]/"result/finalization.json"
        self.assertEqual(json.loads(path.read_text())["publication"]["state"], "failed")
        self.assertFalse(verify_finalization(path)["valid"])

    def test_publisher_failure_does_not_return_success(self):
        with patch("cmw.core.preparation_publication._write_record", side_effect=OSError("invented write failure")):
            with self.assertRaises(PublicationError): self.finalize()


if __name__ == "__main__":
    unittest.main()
