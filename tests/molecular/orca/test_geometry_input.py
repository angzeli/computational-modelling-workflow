from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from cmw.core.artifacts import SinglePointArtifact
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.molecular.orca.geometry import (
    OrcaGeometryContractError,
    OrcaGeometryInput,
    prepare_orca_geometry_input,
    validate_orca_geometry_input,
)
from cmw.molecular.orca.input import (
    OrcaResources,
    OrcaStageSpec,
    parse_rendered_orca_input,
    render_orca_input,
)
from cmw.molecular.orca.status import StageType


FIXTURE = Path(__file__).parents[2] / "fixtures" / "xyz" / "synthetic.xyz"


class OrcaGeometryInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.source = self.directory / "source.xyz"
        self.source.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        self.artifact = structure_artifact_from_file(
            self.source,
            source="synthetic fixture",
            provenance={"test": "orca_geometry_contract"},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_structure_artifact_renders_one_xyzfile_reference(self) -> None:
        geometry_input = prepare_orca_geometry_input(
            self.artifact,
            self.directory / "input.xyz",
            provenance={"workflow_node": "sp"},
        )
        rendered = render_orca_input(
            geometry_input=geometry_input,
            charge=0,
            multiplicity=1,
            spec=OrcaStageSpec(StageType.SP, "HF STO-3G"),
            resources=OrcaResources(),
        )
        input_path = self.directory / "stage.inp"
        input_path.write_text(rendered, encoding="utf-8")
        parsed_target, _ = parse_rendered_orca_input(
            input_path,
            StageType.SP,
            geometry_input=geometry_input,
        )

        self.assertIn("* xyzfile 0 1 input.xyz", rendered)
        self.assertNotIn("H  ", rendered)
        self.assertEqual(parsed_target.geometry_sha256, self.artifact.geometry_hash)
        self.assertEqual(geometry_input.atom_count, self.artifact.atom_count)
        self.assertEqual(geometry_input.geometry_hash, self.artifact.geometry_hash)
        self.assertEqual((geometry_input.charge, geometry_input.multiplicity), (0, 1))
        self.assertEqual(
            geometry_input.provenance["source_structure_artifact_id"],
            self.artifact.artifact_id,
        )
        restored = OrcaGeometryInput.from_mapping(geometry_input.to_dict())
        self.assertEqual(restored.to_dict(), geometry_input.to_dict())
        with self.assertRaisesRegex(ValueError, "charge or multiplicity"):
            render_orca_input(
                geometry_input=geometry_input,
                charge=1,
                multiplicity=2,
                spec=OrcaStageSpec(StageType.SP, "HF STO-3G"),
                resources=OrcaResources(),
            )

    def test_geometry_hash_mismatch_fails_closed(self) -> None:
        geometry_input = prepare_orca_geometry_input(
            self.artifact, self.directory / "input.xyz"
        )
        self.source.write_text(
            self.source.read_text(encoding="utf-8").replace("0.000000", "0.100000", 1),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            OrcaGeometryContractError, "FAILED_GEOMETRY_CONTRACT"
        ):
            validate_orca_geometry_input(geometry_input)

    def test_high_precision_coordinates_round_trip_without_identity_drift(self) -> None:
        source = self.directory / "high-precision.xyz"
        source.write_text(
            "2\noptimized geometry\n"
            "H -0.99948315458624 -0.95055704304975 0.68943286244937\n"
            "H 1.23456789012345 2.34567890123456 -3.45678901234567\n",
            encoding="utf-8",
        )
        artifact = structure_artifact_from_file(
            source,
            source="high-precision optimization",
        )

        geometry_input = prepare_orca_geometry_input(
            artifact, self.directory / "high-precision-input.xyz"
        )

        self.assertEqual(geometry_input.geometry_hash, artifact.geometry_hash)
        validate_orca_geometry_input(geometry_input)

    def test_missing_source_or_materialized_geometry_fails_closed(self) -> None:
        missing = replace(
            self.artifact,
            files={"structure": str(self.directory / "missing.xyz")},
        )
        with self.assertRaisesRegex(OrcaGeometryContractError, "MISSING_STRUCTURE_FILE"):
            prepare_orca_geometry_input(missing, self.directory / "input.xyz")

        geometry_input = prepare_orca_geometry_input(
            self.artifact, self.directory / "input.xyz"
        )
        geometry_input.input_path.unlink()
        with self.assertRaisesRegex(OrcaGeometryContractError, "input geometry file is missing"):
            validate_orca_geometry_input(geometry_input)

    def test_legacy_xyzfile_input_remains_explicitly_supported(self) -> None:
        rendered = render_orca_input(
            geometry_path=Path("input.xyz"),
            charge=0,
            multiplicity=1,
            spec=OrcaStageSpec(StageType.SP, "HF STO-3G"),
            resources=OrcaResources(),
        )
        input_path = self.directory / "stage.inp"
        input_path.write_text(rendered, encoding="utf-8")
        (self.directory / "input.xyz").write_text(
            FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        target, _ = parse_rendered_orca_input(input_path, StageType.SP)

        self.assertEqual(target.geometry_sha256, self.artifact.geometry_hash)
        self.assertIn("xyzfile", rendered)

    def test_non_structure_artifact_record_is_rejected(self) -> None:
        record = SinglePointArtifact(producing_calculation="legacy-sp").to_dict()
        with self.assertRaisesRegex(OrcaGeometryContractError, "not a StructureArtifact"):
            OrcaGeometryInput.from_mapping(
                {
                    "schema_version": 1,
                    "mode": "xyzfile",
                    "source_structure_artifact_id": self.artifact.artifact_id,
                    "source_structure_artifact": record,
                    "source_path": str(self.source),
                    "input_geometry_file": str(self.directory / "input.xyz"),
                    "atom_indices": [0],
                    "atom_labels": ["H"],
                    "atom_count": 1,
                    "geometry_hash": "a" * 64,
                    "charge": 0,
                    "multiplicity": 1,
                    "electronic_state_override": False,
                    "provenance": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
