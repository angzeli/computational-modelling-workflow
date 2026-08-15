from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.core.artifacts import (
    DensityArtifact,
    FrequencyArtifact,
    OptimizationArtifact,
    SinglePointArtifact,
    StructureArtifact,
    artifact_from_dict,
    validate_artifact_compatibility,
)
from cmw.core.structure_artifacts import (
    structure_artifact_from_file,
    validate_structure_artifact,
)


XYZ = """3
water
O  0.000000  0.000000  0.000000
H  0.758602  0.000000  0.504284
H -0.758602  0.000000  0.504284
"""


class StructureArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "water.xyz"
        self.path.write_text(XYZ, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creation_generates_valid_format_neutral_metadata(self) -> None:
        artifact = structure_artifact_from_file(
            self.path,
            source="test fixture",
            charge=0,
            multiplicity=1,
            metadata={"structure_role": "input"},
        )

        self.assertIsInstance(artifact, StructureArtifact)
        self.assertEqual(artifact.format, "xyz")
        self.assertEqual(artifact.atom_count, 3)
        self.assertEqual(artifact.elemental_composition, {"O": 1, "H": 2})
        self.assertEqual(len(artifact.geometry_hash), 64)
        self.assertTrue(artifact.validation.passed)

    def test_validation_fails_closed_for_missing_unreadable_and_inconsistent_files(self) -> None:
        missing = StructureArtifact(
            producing_calculation="import",
            files={"structure": str(self.path.with_name("missing.xyz"))},
            source="test fixture",
            format="xyz",
            atom_count=3,
            elemental_composition={"O": 1, "H": 2},
            charge=0,
            multiplicity=1,
            geometry_hash="a" * 64,
        )
        self.assertEqual(
            validate_structure_artifact(missing).code, "MISSING_STRUCTURE_FILE"
        )

        unreadable_path = self.path.with_name("broken.xyz")
        unreadable_path.write_text("not an xyz record\n", encoding="utf-8")
        unreadable = StructureArtifact(
            producing_calculation="import",
            files={"structure": str(unreadable_path)},
            source="test fixture",
            format="xyz",
            atom_count=1,
            elemental_composition={"H": 1},
            charge=0,
            multiplicity=1,
            geometry_hash="b" * 64,
        )
        self.assertEqual(
            validate_structure_artifact(unreadable).code, "UNREADABLE_STRUCTURE"
        )

        valid = structure_artifact_from_file(self.path, source="test fixture")
        missing_format = StructureArtifact(
            producing_calculation=valid.producing_calculation,
            files=valid.files,
            source=valid.source,
            atom_count=valid.atom_count,
            elemental_composition=valid.elemental_composition,
            charge=valid.charge,
            multiplicity=valid.multiplicity,
            geometry_hash=valid.geometry_hash,
        )
        self.assertEqual(
            validate_structure_artifact(missing_format).code,
            "MISSING_STRUCTURE_METADATA",
        )

        inconsistent = StructureArtifact(
            producing_calculation=valid.producing_calculation,
            files=valid.files,
            source=valid.source,
            format=valid.format,
            atom_count=2,
            elemental_composition={"H": 2},
            charge=valid.charge,
            multiplicity=valid.multiplicity,
            geometry_hash=valid.geometry_hash,
        )
        self.assertEqual(
            validate_structure_artifact(inconsistent).code,
            "INCONSISTENT_STRUCTURE_METADATA",
        )

    def test_manifest_round_trip_preserves_structure_metadata_and_identity(self) -> None:
        artifact = structure_artifact_from_file(
            self.path,
            source="generated geometry",
            producing_calculation="geometry-generator",
            provenance={"generator": "synthetic-test"},
            metadata={"structure_role": "generated"},
        )
        restored = artifact_from_dict(artifact.to_dict())

        self.assertIsInstance(restored, StructureArtifact)
        self.assertEqual(restored.artifact_id, artifact.artifact_id)
        self.assertEqual(restored.geometry_hash, artifact.geometry_hash)
        self.assertEqual(restored.metadata["structure_role"], "generated")
        self.assertEqual(restored.provenance["generator"], "synthetic-test")

    def test_structure_can_parent_calculation_and_density_artifacts(self) -> None:
        structure = structure_artifact_from_file(self.path, source="test fixture")
        for artifact_type in (
            OptimizationArtifact,
            FrequencyArtifact,
            SinglePointArtifact,
            DensityArtifact,
        ):
            with self.subTest(artifact_type=artifact_type.__name__):
                child = artifact_type(
                    producing_calculation=artifact_type.__name__,
                    parent_artifacts=(structure.artifact_id,),
                    validation=structure.validation,
                )
                validate_artifact_compatibility(child, (structure,))


if __name__ == "__main__":
    unittest.main()
