from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cmw.core.artifacts import (
    ArtifactValidation,
    ExcitedStateArtifact,
    StructureArtifact,
    ValidationStatus,
    artifact_from_dict,
    validate_artifact_compatibility,
)
from cmw.molecular.excited_states import StateSelectionRequest, resolve_excited_state
from cmw.molecular.orca.excited_states import parse_orca_tda_excited_states_file
from cmw.molecular.stacking.excited_state import (
    ExcitedStateContractError,
    ExcitedStateProtocol,
    create_excited_state_artifact,
    validate_excited_state_artifact,
)


FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "orca"
    / "excited_states"
    / "orca_6_1_1_tda_singlets.out"
)
MIXED_FIXTURE = FIXTURE.with_name("orca_6_1_1_tda_mixed.out")
PASSED = ArtifactValidation(
    ValidationStatus.PASSED,
    {"fixture": True},
    "VALID_STRUCTURE",
    "fixture structure is explicit",
)


class ParsedExcitedStateArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parsed = parse_orca_tda_excited_states_file(FIXTURE)
        cls.source = StructureArtifact(
            producing_calculation="fixture-pdi-geometry",
            source="pdi_opt.xyz",
            format="xyz",
            atom_count=40,
            elemental_composition={"C": 24, "H": 10, "N": 2, "O": 4},
            charge=0,
            multiplicity=1,
            geometry_hash="a" * 64,
            validation=PASSED,
        )
        cls.protocol = ExcitedStateProtocol(
            method="WB97X-D3",
            basis="6-31+G(d,p)",
            number_of_roots=30,
            state_selection={"criteria": ("lowest_bright_state",)},
            tda=True,
            generate_ntos=False,
            functional="WB97X-D3",
            spin_manifold="singlet",
        )

    def artifact(self) -> ExcitedStateArtifact:
        selection = resolve_excited_state(
            self.parsed.states,
            StateSelectionRequest(
                "lowest_bright_singlet", minimum_oscillator_strength=0.01
            ),
        )
        return create_excited_state_artifact(
            self.source,
            self.protocol,
            self.parsed.states,
            state_selection_rationale=selection.rationale,
            runtime_provenance={"program": "ORCA", "version": "6.1.1"},
            files={"output": str(FIXTURE)},
            selection_results=(selection,),
            parser_provenance=self.parsed.source_provenance,
            execution_attempt={"attempt_id": "fixture-attempt-001"},
        )

    def test_parsed_records_finalize_without_nto_or_multiwfn(self) -> None:
        artifact = self.artifact()

        self.assertTrue(artifact.validation.passed)
        self.assertEqual(artifact.metadata["excited_state_contract"], "quantitative_v2")
        self.assertEqual(
            artifact.metadata["selected_state_identities"][0]["label"], "S1"
        )
        self.assertEqual(artifact.metadata["parser_version"], "1.0.1")
        self.assertEqual(
            artifact.metadata["fixture_tested_grammar_version"],
            "orca_6_1_tda_v1",
        )
        self.assertEqual(
            artifact.metadata["execution_attempt"]["attempt_id"],
            "fixture-attempt-001",
        )
        self.assertNotIn("nto", artifact.files)
        self.assertTrue(
            validate_excited_state_artifact(
                artifact, source=self.source, protocol=self.protocol
            ).passed
        )
        validate_artifact_compatibility(artifact, (self.source,))

    def test_artifact_serialization_round_trip_preserves_canonical_identity(self) -> None:
        artifact = self.artifact()
        restored = artifact_from_dict(artifact.to_dict())

        self.assertIsInstance(restored, ExcitedStateArtifact)
        self.assertEqual(restored.artifact_id, artifact.artifact_id)
        self.assertEqual(
            restored.metadata["selected_state_identities"],
            artifact.metadata["selected_state_identities"],
        )

    def test_mixed_manifold_artifact_preserves_s1_t1_identity_collision(self) -> None:
        parsed = parse_orca_tda_excited_states_file(MIXED_FIXTURE)
        protocol = ExcitedStateProtocol(
            method="WB97X-D3",
            basis="6-31+G(d,p)",
            number_of_roots=15,
            state_selection={"criteria": ("lowest_triplet",)},
            tda=True,
            generate_ntos=False,
            functional="WB97X-D3",
            spin_manifold="mixed",
        )
        selection = resolve_excited_state(
            parsed.states, StateSelectionRequest("lowest_triplet")
        )
        artifact = create_excited_state_artifact(
            self.source,
            protocol,
            parsed.states,
            state_selection_rationale=selection.rationale,
            runtime_provenance={"program": "ORCA", "version": "6.1.1"},
            files={"output": str(MIXED_FIXTURE)},
            selection_results=(selection,),
            parser_provenance=parsed.source_provenance,
            execution_attempt={"attempt_id": "fixture-attempt-mixed"},
        )

        identities = {
            (
                item["spin_manifold"],
                item["local_state_index"],
                item["orca_global_state_index"],
            )
            for item in artifact.metadata["excited_states"]
        }
        self.assertIn(("singlet", 1, 1), identities)
        self.assertIn(("triplet", 1, 16), identities)
        self.assertTrue(
            validate_excited_state_artifact(
                artifact, source=self.source, protocol=protocol
            ).passed
        )

    def test_parser_backed_artifact_rejects_output_identity_drift(self) -> None:
        selection = resolve_excited_state(
            self.parsed.states,
            StateSelectionRequest(
                "lowest_bright_singlet", minimum_oscillator_strength=0.01
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "changed.out"
            changed.write_bytes(FIXTURE.read_bytes() + b"\n")
            with self.assertRaisesRegex(
                ExcitedStateContractError, "size does not match"
            ):
                create_excited_state_artifact(
                    self.source,
                    self.protocol,
                    self.parsed.states,
                    state_selection_rationale=selection.rationale,
                    runtime_provenance={"program": "ORCA", "version": "6.1.1"},
                    files={"output": str(changed)},
                    selection_results=(selection,),
                    parser_provenance=self.parsed.source_provenance,
                    execution_attempt={"attempt_id": "fixture-attempt-drift"},
                )


if __name__ == "__main__":
    unittest.main()
