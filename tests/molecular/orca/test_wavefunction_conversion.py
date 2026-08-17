from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cmw.core.job import JobTarget
from cmw.core.provenance import ArtifactRecord, atomic_write_json, records_to_dict
from cmw.molecular.orca.conversion import (
    OrcaWavefunctionConversionError,
    finalize_molden_conversion,
    plan_molden_conversion,
    prepare_molden_conversion,
)
from cmw.molecular.workflows.source import validate_source_result
from cmw.structure.xyz import geometry_hash, read_xyz


class OrcaWavefunctionConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.geometry = self.root / "input.xyz"
        self.geometry.write_text(
            "2\nsource\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n", encoding="utf-8"
        )
        self.gbw = self.root / "stage.gbw"
        self.gbw.write_bytes(b"synthetic gbw fixture")
        self.converter = self.root / "orca_2mkl"
        self.converter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.converter.chmod(0o755)
        target = JobTarget(
            stage_type="SP",
            geometry_sha256=geometry_hash(read_xyz(self.geometry)),
            charge=0,
            multiplicity=1,
            calculation={"adapter": "orca", "keywords": "r2SCAN-3c"},
        )
        self.source_result = self.root / "source-job.json"
        atomic_write_json(
            self.source_result,
            {
                "schema_version": 1,
                "target": target.to_dict(),
                "lineage": {
                    "source": "test",
                    "geometry_sha256": target.geometry_sha256,
                    "parent_stage": None,
                    "parent_target_id": None,
                    "parent_artifact_sha256": None,
                },
                "execution": {"status": "SUCCESS"},
                "scientific": {"status": "VALID"},
                "artifacts": records_to_dict(
                    {
                        "wavefunction": ArtifactRecord.from_path(
                            self.gbw, role="wavefunction"
                        ),
                        "input_geometry": ArtifactRecord.from_path(
                            self.geometry, role="input_geometry"
                        ),
                    }
                ),
                "reusable": True,
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plan(self):
        return plan_molden_conversion(
            source_result=self.source_result,
            output_root=self.root / "outputs",
            converter=self.converter,
            spin_mode="restricted",
        )

    def test_finalized_conversion_is_a_valid_multiwfn_source(self) -> None:
        prepared = prepare_molden_conversion(self.plan())
        output = Path(str(prepared["output_wavefunction"]))
        output.write_text("[Molden Format]\n[Atoms] Angs\n", encoding="utf-8")

        result = finalize_molden_conversion(
            plan_path=Path(str(prepared["plan_path"])), process_exit_code=0
        )
        source = validate_source_result(Path(str(prepared["result_path"])))

        self.assertTrue(result["reusable"])
        self.assertEqual(source.wavefunction.format, "molden")
        self.assertEqual(source.wavefunction.spin_mode, "restricted")
        self.assertTrue(self.plan().reusable)

    def test_missing_molden_output_fails_closed(self) -> None:
        prepared = prepare_molden_conversion(self.plan())
        with self.assertRaisesRegex(
            OrcaWavefunctionConversionError, "did not produce a non-empty Molden"
        ):
            finalize_molden_conversion(
                plan_path=Path(str(prepared["plan_path"])), process_exit_code=0
            )

    def test_spin_semantics_must_be_explicit(self) -> None:
        with self.assertRaisesRegex(OrcaWavefunctionConversionError, "spin_mode"):
            plan_molden_conversion(
                source_result=self.source_result,
                output_root=self.root / "outputs",
                converter=self.converter,
                spin_mode="",
            )


if __name__ == "__main__":
    unittest.main()
