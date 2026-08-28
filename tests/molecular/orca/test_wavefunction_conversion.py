from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cmw.core.artifacts import artifact_from_dict
from cmw.core.job import JobTarget
from cmw.core.provenance import ArtifactRecord, atomic_write_json, records_to_dict
from cmw.molecular.orca.conversion import (
    FRONTIER_SEMANTIC_CONTRACT,
    OrcaWavefunctionConversionError,
    finalize_molden_conversion,
    plan_molden_conversion,
    prepare_molden_conversion,
)
from cmw.molecular.workflows.fmo_cubes import frontier_orbitals
from cmw.molecular.workflows.source import validate_source_result
from cmw.structure.xyz import geometry_hash, read_xyz


ORCA_OUTPUT = """
SCF CONVERGED AFTER 8 CYCLES
FINAL SINGLE POINT ENERGY      -100.250000000000
ORBITAL ENERGIES
----------------

  NO   OCC          E(Eh)            E(eV)
   0   2.0000      -0.700000       -19.0479
   1   2.0000      -0.250000        -6.8028
   2   0.0000      -0.050000        -1.3606
   3   0.0000       0.020000         0.5442

ORCA TERMINATED NORMALLY
"""


def molden_output(*, lumo_energy: float = -0.05) -> str:
    rows = ((-0.7, 2.0), (-0.25, 2.0), (lumo_energy, 0.0), (0.02, 0.0))
    records = []
    for energy, occupation in rows:
        records.append(
            "\n".join(
                (
                    " Sym= A",
                    f" Ene= {energy:.8f}",
                    " Spin= Alpha",
                    f" Occup= {occupation:.8f}",
                    " 1  1.000000",
                )
            )
        )
    return "[Molden Format]\n[Atoms] Angs\n[MO]\n" + "\n".join(records) + "\n"


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
        self.orca_output = self.root / "stage.out"
        self.orca_output.write_text(ORCA_OUTPUT, encoding="utf-8")
        self.converter = self.root / "orca_2mkl"
        self.converter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.converter.chmod(0o755)
        self.target = JobTarget(
            stage_type="SP",
            geometry_sha256=geometry_hash(read_xyz(self.geometry)),
            charge=0,
            multiplicity=1,
            calculation={"adapter": "orca", "keywords": "r2SCAN-3c"},
        )
        self.source_result = self.root / "source-job.json"
        self.write_source_result()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_source_result(self) -> None:
        atomic_write_json(
            self.source_result,
            {
                "schema_version": 1,
                "target": self.target.to_dict(),
                "lineage": {
                    "source": "test",
                    "geometry_sha256": self.target.geometry_sha256,
                    "parent_stage": None,
                    "parent_target_id": None,
                    "parent_artifact_sha256": None,
                },
                "attempt": {
                    "attempt_id": "attempt_001",
                    "target_id": self.target.target_id,
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
                        "output": ArtifactRecord.from_path(
                            self.orca_output, role="output"
                        ),
                    }
                ),
                "reusable": True,
            },
        )

    def plan(self, *, spin_mode: str = "restricted"):
        return plan_molden_conversion(
            source_result=self.source_result,
            output_root=self.root / "outputs",
            converter=self.converter,
            spin_mode=spin_mode,
        )

    def prepare_output(
        self, *, spin_mode: str = "restricted", lumo_energy: float = -0.05
    ) -> dict[str, object]:
        prepared = prepare_molden_conversion(self.plan(spin_mode=spin_mode))
        Path(str(prepared["output_wavefunction"])).write_text(
            molden_output(lumo_energy=lumo_energy), encoding="utf-8"
        )
        return prepared

    def test_finalized_conversion_propagates_typed_frontier_semantics(self) -> None:
        prepared = self.prepare_output()
        result = finalize_molden_conversion(
            plan_path=Path(str(prepared["plan_path"])), process_exit_code=0
        )
        source = validate_source_result(Path(str(prepared["result_path"])))

        self.assertTrue(result["reusable"])
        self.assertEqual(source.wavefunction.format, "molden")
        self.assertEqual(source.wavefunction.spin_mode, "restricted")
        self.assertEqual(source.wavefunction.orbital_indexing, "one_based")
        self.assertEqual(frontier_orbitals(source), (2, 3))
        self.assertEqual(source.wavefunction.frontier_orbitals.homo.source_index, 1)
        self.assertEqual(source.wavefunction.frontier_orbitals.lumo.source_index, 2)
        self.assertEqual(
            result["wavefunction_semantics"]["semantic_contract"],
            FRONTIER_SEMANTIC_CONTRACT,
        )
        self.assertEqual(
            result["wavefunction_semantics"]["molden_cross_validation"]["status"],
            "PASSED",
        )
        parsed_artifact = artifact_from_dict(result["scientific_artifact"])
        self.assertTrue(parsed_artifact.validation.passed)
        self.assertEqual(
            parsed_artifact.metadata["wavefunction_semantics"]["homo_index"], 2
        )
        self.assertEqual(
            result["provenance"]["source_attempt_id"], "attempt_001"
        )
        self.assertEqual(
            result["provenance"]["source_output_sha256"],
            prepared["source_output_sha256"],
        )
        self.assertTrue(self.plan().reusable)

    def test_plan_identity_is_deterministic_and_tracks_source_result(self) -> None:
        first = self.plan()
        second = self.plan()
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.target.target_id, second.target.target_id)
        original_target = first.target.target_id

        self.orca_output.write_text(
            ORCA_OUTPUT.replace("-0.050000", "-0.040000"), encoding="utf-8"
        )
        self.write_source_result()
        changed = self.plan()
        self.assertNotEqual(original_target, changed.target.target_id)
        self.assertNotEqual(first.source_output_sha256, changed.source_output_sha256)
        self.assertNotEqual(first.source_result_sha256, changed.source_result_sha256)

    def test_orca_molden_frontier_mismatch_fails_closed(self) -> None:
        prepared = self.prepare_output(lumo_energy=-0.04)
        with self.assertRaisesRegex(
            OrcaWavefunctionConversionError, "LUMO energies disagree"
        ):
            finalize_molden_conversion(
                plan_path=Path(str(prepared["plan_path"])), process_exit_code=0
            )

    def test_partial_orca_occupation_fails_closed(self) -> None:
        self.orca_output.write_text(
            ORCA_OUTPUT.replace("1   2.0000", "1   1.5000"), encoding="utf-8"
        )
        self.write_source_result()
        prepared = self.prepare_output()
        with self.assertRaisesRegex(ValueError, "partial occupation"):
            finalize_molden_conversion(
                plan_path=Path(str(prepared["plan_path"])), process_exit_code=0
            )

    def test_missing_molden_output_fails_closed(self) -> None:
        prepared = prepare_molden_conversion(self.plan())
        with self.assertRaisesRegex(
            OrcaWavefunctionConversionError, "did not produce a non-empty Molden"
        ):
            finalize_molden_conversion(
                plan_path=Path(str(prepared["plan_path"])), process_exit_code=0
            )

    def test_unrestricted_conversion_preserves_spin_and_fmo_rejects(self) -> None:
        prepared = self.prepare_output(spin_mode="unrestricted")
        finalize_molden_conversion(
            plan_path=Path(str(prepared["plan_path"])), process_exit_code=0
        )
        source = validate_source_result(Path(str(prepared["result_path"])))
        self.assertEqual(source.wavefunction.spin_mode, "unrestricted")
        self.assertIsNone(source.wavefunction.homo_index)
        with self.assertRaisesRegex(ValueError, "open-shell/unrestricted"):
            frontier_orbitals(source)

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
