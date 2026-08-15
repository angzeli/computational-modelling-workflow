from __future__ import annotations

import unittest

from cmw.core.artifacts import (
    ArtifactValidation,
    DensityArtifact,
    IGMHArtifact,
    ValidationStatus,
)
from cmw.molecular.workflows.igmh import (
    IgmhExecutionContractError,
    validate_igmh_execution_contract,
)


PASSED = ArtifactValidation(
    ValidationStatus.PASSED, {"fixture": True}, "VALID", "synthetic fixture"
)


def _contract(
    *, density_method: str = "wB97X-D4", artifact_method: str = "wB97X-D4"
):
    plan = {
        "operation": "IGMH",
        "profile": "interfragment",
        "grid_spacing_bohr": 0.2,
        "visualization": {"recommended_isovalue": 0.01},
    }
    density = DensityArtifact(
        producing_calculation="density",
        method=density_method,
        basis="def2-TZVPP",
        validation=PASSED,
    )
    artifact = IGMHArtifact(
        producing_calculation="igmh",
        method=artifact_method,
        basis="def2-TZVPP",
        protocol=plan,
        parent_artifacts=(density.artifact_id,),
        validation=PASSED,
        metadata={
            "density_source": density.artifact_id,
            "grid_spacing_bohr": 0.2,
            "multiwfn_protocol": plan,
            "visualization": plan["visualization"],
        },
    )
    return plan, density, artifact


class IgmhExecutionContractTests(unittest.TestCase):
    def test_valid_production_igmh_contract(self) -> None:
        plan, density, artifact = _contract()

        validate_igmh_execution_contract(plan, artifact, (density,))

    def test_missing_grid_spacing_fails_protocol_validation(self) -> None:
        plan, density, artifact = _contract()
        plan = {key: value for key, value in plan.items() if key != "grid_spacing_bohr"}

        with self.assertRaisesRegex(
            IgmhExecutionContractError, "grid_spacing_bohr"
        ) as caught:
            validate_igmh_execution_contract(plan, artifact, (density,))

        self.assertEqual(caught.exception.code, "FAILED_PROTOCOL_MISMATCH")

    def test_missing_density_parent_fails_protocol_validation(self) -> None:
        plan, _, artifact = _contract()

        with self.assertRaisesRegex(IgmhExecutionContractError, "DensityArtifact"):
            validate_igmh_execution_contract(plan, artifact, ())

    def test_incompatible_density_parent_fails_protocol_validation(self) -> None:
        plan, density, artifact = _contract(artifact_method="PBE0")

        with self.assertRaisesRegex(IgmhExecutionContractError, "incompatible"):
            validate_igmh_execution_contract(plan, artifact, (density,))


if __name__ == "__main__":
    unittest.main()
