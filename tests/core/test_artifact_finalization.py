from __future__ import annotations

import unittest

from cmw.core.artifact_finalization import (
    ArtifactBundleFinalizationError,
    ArtifactFinalizationEvidence,
    finalize_artifact_bundle,
)
from cmw.core.artifacts import (
    ArtifactValidation,
    CPInteractionArtifact,
    DimerEnergyArtifact,
    FragmentEnergyArtifact,
    LEDArtifact,
    OptimizationArtifact,
    SinglePointArtifact,
    ValidationStatus,
)


PASSED = ArtifactValidation(
    ValidationStatus.PASSED, {"fixture": True}, "VALID", "synthetic fixture"
)


def _evidence(*artifacts):
    return {
        artifact.artifact_id: ArtifactFinalizationEvidence(
            PASSED,
            files={"result": f"results/{artifact.producing_calculation}.json"},
            provenance={"finalized_by": "synthetic test"},
        )
        for artifact in artifacts
    }


class ArtifactBundleFinalizationTests(unittest.TestCase):
    def test_complete_bundle_is_dependency_ordered_and_finalized(self) -> None:
        optimization = OptimizationArtifact(producing_calculation="optimization")
        energy_protocol = {"pno": "TightPNO", "led": True}
        dimer = DimerEnergyArtifact(
            producing_calculation="dimer",
            method="DLPNO-CCSD(T)",
            basis="def2-TZVPP",
            protocol=energy_protocol,
            parent_artifacts=(optimization.artifact_id,),
            metadata={"energy_role": "dimer"},
        )
        fragment_a = FragmentEnergyArtifact(
            producing_calculation="fragment-a",
            method="DLPNO-CCSD(T)",
            basis="def2-TZVPP",
            protocol=energy_protocol,
            parent_artifacts=(optimization.artifact_id,),
            metadata={"energy_role": "fragment"},
        )
        fragment_b = FragmentEnergyArtifact(
            producing_calculation="fragment-b",
            method="DLPNO-CCSD(T)",
            basis="def2-TZVPP",
            protocol=energy_protocol,
            parent_artifacts=(optimization.artifact_id,),
            metadata={"energy_role": "fragment"},
        )
        interaction = CPInteractionArtifact(
            producing_calculation="interaction",
            method="DLPNO-CCSD(T)",
            basis="def2-TZVPP",
            parent_artifacts=(
                dimer.artifact_id,
                fragment_a.artifact_id,
                fragment_b.artifact_id,
            ),
            metadata={"derived_quantity": "E_AB - E_A - E_B"},
        )
        led = LEDArtifact(
            producing_calculation="led",
            parent_artifacts=(dimer.artifact_id,),
        )
        planned = (led, interaction, fragment_b, dimer, optimization, fragment_a)

        bundle = finalize_artifact_bundle(planned, _evidence(*planned))

        positions = {
            artifact.artifact_id: index
            for index, artifact in enumerate(bundle.artifacts)
        }
        self.assertLess(
            positions[optimization.artifact_id], positions[dimer.artifact_id]
        )
        self.assertLess(
            positions[dimer.artifact_id], positions[interaction.artifact_id]
        )
        self.assertTrue(
            all(artifact.validation.passed for artifact in bundle.artifacts)
        )
        self.assertTrue(
            all(artifact.provenance["finalized_by"] for artifact in bundle.artifacts)
        )
        self.assertEqual(
            set(bundle.by_id), {artifact.artifact_id for artifact in planned}
        )

    def test_missing_parent_fails_closed(self) -> None:
        artifact = SinglePointArtifact(
            producing_calculation="orphan",
            parent_artifacts=("missing-parent",),
        )

        with self.assertRaises(ArtifactBundleFinalizationError) as caught:
            finalize_artifact_bundle((artifact,), _evidence(artifact))

        self.assertEqual(caught.exception.failures[0].code, "MISSING_PARENT")

    def test_failed_validation_is_not_promoted(self) -> None:
        artifact = SinglePointArtifact(producing_calculation="failed-sp")
        failed = ArtifactValidation(
            ValidationStatus.FAILED,
            {"scf_converged": False},
            "SCF_NOT_CONVERGED",
            "SCF did not converge",
        )

        with self.assertRaises(ArtifactBundleFinalizationError) as caught:
            finalize_artifact_bundle(
                (artifact,),
                {artifact.artifact_id: ArtifactFinalizationEvidence(failed)},
            )

        self.assertEqual(caught.exception.failures[0].code, "SCF_NOT_CONVERGED")


if __name__ == "__main__":
    unittest.main()
