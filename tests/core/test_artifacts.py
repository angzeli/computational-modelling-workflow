from __future__ import annotations

import unittest

from cmw.core.artifacts import (
    ArtifactCompatibilityError,
    ArtifactValidation,
    CPInteractionArtifact,
    DensityArtifact,
    DimerEnergyArtifact,
    FragmentEnergyArtifact,
    IGMHArtifact,
    LEDArtifact,
    SinglePointArtifact,
    ValidationStatus,
    artifact_from_dict,
    artifact_from_result,
    artifact_matches_type,
    validate_artifact_compatibility,
)
from cmw.core.job import JobTarget


PASSED = ArtifactValidation(
    ValidationStatus.PASSED, {"fixture": True}, "VALID", "synthetic fixture"
)


def _energy(cls, calculation: str, role: str, *, basis: str = "def2-TZVPP"):
    return cls(
        producing_calculation=calculation,
        method="DLPNO-CCSD(T)",
        basis=basis,
        protocol={"pno": "TightPNO"},
        validation=PASSED,
        metadata={"energy_role": role},
    )


class ArtifactIdentityTests(unittest.TestCase):
    def test_typed_artifact_round_trip_preserves_semantic_identity(self) -> None:
        artifact = SinglePointArtifact(
            producing_calculation="target-1",
            method="PBE0",
            basis="def2-SVP",
            files={"output": "stage.out"},
            validation=PASSED,
            provenance={"engine": "synthetic"},
        )
        restored = artifact_from_dict(artifact.to_dict())
        self.assertIsInstance(restored, SinglePointArtifact)
        self.assertEqual(restored.artifact_id, artifact.artifact_id)
        self.assertTrue(artifact_matches_type(restored, "EnergyArtifact"))
        self.assertTrue(restored.validation.passed)

    def test_existing_schema_one_manifest_remains_readable(self) -> None:
        restored = artifact_from_dict(
            {
                "schema_version": 1,
                "artifact_id": (
                    "d0247ba3658fe2791077cbafcf3e88cede3570ba57554eb7bb666c3ef85f6f36"
                ),
                "artifact_type": "SinglePointArtifact",
                "producing_calculation": "legacy-sp",
                "method": "PBE0",
                "basis": "def2-SVP",
                "protocol": {},
                "parent_artifacts": [],
                "files": {},
                "validation": {},
                "provenance": {},
                "metadata": {},
            }
        )

        self.assertIsInstance(restored, SinglePointArtifact)
        self.assertEqual(
            restored.artifact_id,
            "d0247ba3658fe2791077cbafcf3e88cede3570ba57554eb7bb666c3ef85f6f36",
        )

    def test_legacy_orca_result_maps_without_replacing_existing_fields(self) -> None:
        target = JobTarget(
            "SP",
            "a" * 64,
            0,
            1,
            {
                "keywords": "PBE0 def2-SVP",
                "protocol": {"method": "PBE0", "basis": "def2-SVP"},
            },
        )
        record = {
            "target": target.to_dict(),
            "execution": {"status": "SUCCESS"},
            "scientific": {"status": "VALID", "reason": "valid"},
            "artifacts": {"output": {"path": "stage.out"}},
            "reusable": True,
        }
        artifact = artifact_from_result(record)
        self.assertIsInstance(artifact, SinglePointArtifact)
        self.assertEqual(artifact.method, "PBE0")
        self.assertEqual(artifact.basis, "def2-SVP")
        self.assertEqual(artifact.files["output"], "stage.out")


class ArtifactCompatibilityTests(unittest.TestCase):
    def test_cp_interaction_requires_comparable_dimer_and_fragment_energies(self) -> None:
        dimer = _energy(DimerEnergyArtifact, "dimer", "dimer")
        fragment_a = _energy(FragmentEnergyArtifact, "fragment-a", "fragment")
        fragment_b = _energy(FragmentEnergyArtifact, "fragment-b", "fragment")
        artifact = CPInteractionArtifact(
            producing_calculation="aggregate",
            method="DLPNO-CCSD(T)",
            basis="def2-TZVPP",
            protocol={"pno": "TightPNO"},
            parent_artifacts=(
                dimer.artifact_id,
                fragment_a.artifact_id,
                fragment_b.artifact_id,
            ),
            validation=PASSED,
            metadata={"derived_quantity": "E_AB - E_A - E_B"},
        )
        validate_artifact_compatibility(artifact, (dimer, fragment_a, fragment_b))

        wrong_basis = _energy(
            FragmentEnergyArtifact, "fragment-b-wrong", "fragment", basis="def2-SVP"
        )
        incompatible = CPInteractionArtifact(
            producing_calculation="aggregate-wrong",
            parent_artifacts=(
                dimer.artifact_id,
                fragment_a.artifact_id,
                wrong_basis.artifact_id,
            ),
            validation=PASSED,
            metadata={"derived_quantity": "E_AB - E_A - E_B"},
        )
        with self.assertRaisesRegex(ArtifactCompatibilityError, "different methods"):
            validate_artifact_compatibility(
                incompatible, (dimer, fragment_a, wrong_basis)
            )

    def test_led_requires_dlpno_parent_with_led_enabled(self) -> None:
        parent = SinglePointArtifact(
            producing_calculation="dlpno-led",
            method="DLPNO-CCSD(T)",
            basis="def2-TZVPP",
            protocol={"pno": "TightPNO", "led": True},
            validation=PASSED,
        )
        led = LEDArtifact(
            producing_calculation="led-analysis",
            parent_artifacts=(parent.artifact_id,),
            validation=PASSED,
        )
        validate_artifact_compatibility(led, (parent,))

        dft = SinglePointArtifact(
            producing_calculation="dft",
            method="B3LYP",
            basis="def2-SVP",
            validation=PASSED,
        )
        invalid = LEDArtifact(
            producing_calculation="invalid-led",
            parent_artifacts=(dft.artifact_id,),
            validation=PASSED,
        )
        with self.assertRaisesRegex(ArtifactCompatibilityError, "DLPNO"):
            validate_artifact_compatibility(invalid, (dft,))

    def test_igmh_requires_valid_density_parent(self) -> None:
        density = DensityArtifact(
            producing_calculation="density",
            method="wB97X-D4",
            basis="def2-TZVPP",
            validation=PASSED,
        )
        igmh = IGMHArtifact(
            producing_calculation="igmh",
            parent_artifacts=(density.artifact_id,),
            validation=PASSED,
        )
        validate_artifact_compatibility(igmh, (density,))

        invalid_density = DensityArtifact(
            producing_calculation="invalid-density",
            validation=ArtifactValidation(
                ValidationStatus.FAILED, {"density": False}, "INVALID", "invalid"
            ),
        )
        invalid = IGMHArtifact(
            producing_calculation="invalid-igmh",
            parent_artifacts=(invalid_density.artifact_id,),
        )
        with self.assertRaisesRegex(ArtifactCompatibilityError, "invalid parents"):
            validate_artifact_compatibility(invalid, (invalid_density,))


if __name__ == "__main__":
    unittest.main()
