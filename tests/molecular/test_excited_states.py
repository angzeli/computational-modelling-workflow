from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from cmw.core.artifacts import (
    ArtifactValidation,
    ExcitedStateArtifact,
    ValidationStatus,
)
from cmw.molecular.excited_states import (
    ExcitedStateIdentity,
    ExcitedStateRecord,
    ExcitedStateSelectionContractError,
    StateSelectionRequest,
    StateSelectionResult,
    StateSelectionStatus,
    attach_selected_state_identities,
    resolve_excited_state,
    validate_excited_state_selection_contract,
)
from cmw.molecular.orca.excited_states import parse_orca_tda_excited_states_file


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "orca"
    / "excited_states"
    / "orca_6_1_1_tda_mixed.out"
)


class StateResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.states = parse_orca_tda_excited_states_file(FIXTURE).states

    def test_lowest_state_is_manifold_specific(self) -> None:
        singlet = resolve_excited_state(
            self.states, StateSelectionRequest("lowest_state", "singlet")
        )
        triplet = resolve_excited_state(
            self.states, StateSelectionRequest("lowest_state", "triplet")
        )

        self.assertEqual(singlet.selected_identity, ExcitedStateIdentity("singlet", 1))
        self.assertEqual(triplet.selected_identity, ExcitedStateIdentity("triplet", 1))
        self.assertEqual(singlet.candidate_count, 15)
        self.assertIn("energy ascending", singlet.tie_break)

    def test_lowest_bright_singlet_uses_configured_gauge_and_threshold(self) -> None:
        selected = resolve_excited_state(
            self.states,
            StateSelectionRequest(
                "lowest_bright_singlet",
                minimum_oscillator_strength=0.01,
                oscillator_strength_gauge="length",
            ),
        )

        self.assertEqual(selected.selected_identity, ExcitedStateIdentity("singlet", 1))
        self.assertEqual(selected.status, StateSelectionStatus.SELECTED)
        self.assertEqual(selected.configuration["minimum_oscillator_strength"], 0.01)

    def test_brightest_singlet_in_reference_window_is_deterministic(self) -> None:
        selected = resolve_excited_state(
            self.states,
            StateSelectionRequest(
                "brightest_singlet_in_window",
                maximum_energy_above_reference_ev=1.0,
                minimum_oscillator_strength=0.0,
            ),
        )

        self.assertEqual(selected.selected_identity, ExcitedStateIdentity("singlet", 1))
        self.assertIn("oscillator strength descending", selected.tie_break)

    def test_experimental_energy_and_wavelength_windows_are_configurable(self) -> None:
        selected = resolve_excited_state(
            self.states,
            StateSelectionRequest(
                "experimental_window",
                energy_interval_ev=(3.8, 4.1),
                wavelength_interval_nm=(300.0, 330.0),
                window_preference="brightest",
            ),
        )

        self.assertEqual(selected.selected_identity, ExcitedStateIdentity("singlet", 3))
        self.assertEqual(selected.candidate_count, 2)

    def test_tie_break_uses_energy_then_canonical_index(self) -> None:
        states = (
            ExcitedStateRecord(
                2,
                2.5,
                0.2,
                "singlet",
                length_gauge_oscillator_strength=0.2,
                primary_oscillator_strength_gauge="length",
            ),
            ExcitedStateRecord(
                1,
                2.5,
                0.2,
                "singlet",
                length_gauge_oscillator_strength=0.2,
                primary_oscillator_strength_gauge="length",
            ),
        )

        result = resolve_excited_state(
            states,
            StateSelectionRequest(
                "lowest_bright_singlet", minimum_oscillator_strength=0.1
            ),
        )
        self.assertEqual(result.selected_identity, ExcitedStateIdentity("singlet", 1))

    def test_no_match_is_explicit(self) -> None:
        result = resolve_excited_state(
            self.states,
            StateSelectionRequest(
                "lowest_bright_singlet", minimum_oscillator_strength=10.0
            ),
        )
        self.assertEqual(result.status, StateSelectionStatus.NO_MATCH)
        self.assertIsNone(result.selected_identity)
        self.assertEqual(result.candidate_count, 0)

    def test_ct_selection_is_deferred_without_guessing(self) -> None:
        result = resolve_excited_state(
            self.states, StateSelectionRequest("lowest_ct_like_state")
        )

        self.assertEqual(result.status, StateSelectionStatus.DEFERRED)
        self.assertIsNone(result.selected_identity)
        self.assertIn("fragment-resolved", result.rationale)

    def test_selection_result_round_trip_preserves_policy_evidence(self) -> None:
        result = resolve_excited_state(
            self.states,
            StateSelectionRequest(
                "lowest_bright_singlet", minimum_oscillator_strength=0.01
            ),
        )
        restored = StateSelectionResult.from_mapping(result.to_dict())
        self.assertEqual(restored, result)

    def test_legacy_record_mapping_gains_canonical_local_identity(self) -> None:
        legacy = ExcitedStateRecord.from_mapping(
            {
                "state_index": 2,
                "excitation_energy_ev": 3.1,
                "oscillator_strength": 0.04,
                "spin_manifold": "singlet",
                "selection_labels": [],
                "selection_rationale": None,
            }
        )
        self.assertEqual(legacy.identity, ExcitedStateIdentity("singlet", 2))
        self.assertIsNone(legacy.orca_global_state_index)


class ExcitedStateSelectionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = (
            ExcitedStateRecord(1, 2.5, 0.1, "singlet"),
            ExcitedStateRecord(2, 3.0, 0.2, "singlet"),
        )
        self.artifact = ExcitedStateArtifact(
            producing_calculation="tda-target",
            method="wB97X-D4",
            basis="def2-TZVPP",
            protocol={"task": "excited_state"},
            validation=ArtifactValidation(
                ValidationStatus.PASSED,
                {"fixture": True},
                "VALID_EXCITED_STATE_ARTIFACT",
                "fixture parent is valid",
            ),
            metadata={
                "excited_states": [item.to_dict() for item in self.records],
                "source_geometry_hash": "a" * 64,
            },
        )
        self.result = StateSelectionResult(
            StateSelectionStatus.SELECTED,
            self.records[0].identity,
            "explicit_state",
            {"state": "S1"},
            "explicit fixture selection",
            1,
            "explicit identity; no tie",
        )

    def test_attachment_is_canonical_deterministic_and_preserves_legacy_alias(self) -> None:
        selected = attach_selected_state_identities(
            self.artifact, (self.records[0],), (self.result,)
        )
        repeated = attach_selected_state_identities(
            selected, (self.records[0],), (self.result,)
        )

        self.assertNotEqual(selected.artifact_id, self.artifact.artifact_id)
        self.assertEqual(repeated.artifact_id, selected.artifact_id)
        self.assertEqual(
            selected.metadata["selected_state_identities"],
            selected.metadata["selected_states"],
        )
        self.assertEqual(
            validate_excited_state_selection_contract(selected, self.records[0]),
            (self.records[0],),
        )

    def test_conflicting_legacy_alias_fails_closed(self) -> None:
        conflicting = replace(
            self.artifact,
            metadata={
                **self.artifact.metadata,
                "selected_states": [self.records[1].to_dict()],
            },
        )
        with self.assertRaisesRegex(
            ExcitedStateSelectionContractError, "aliases conflict"
        ):
            attach_selected_state_identities(
                conflicting, (self.records[0],), (self.result,)
            )

    def test_missing_canonical_selection_blocks_requested_state(self) -> None:
        with self.assertRaisesRegex(
            ExcitedStateSelectionContractError, "selected_state_identities"
        ):
            validate_excited_state_selection_contract(
                self.artifact, self.records[0]
            )

    def test_requested_state_must_match_quantitative_parent_exactly(self) -> None:
        selected = attach_selected_state_identities(
            self.artifact, (self.records[0],), (self.result,)
        )
        changed = replace(self.records[0], excitation_energy_ev=2.6)
        with self.assertRaisesRegex(
            ExcitedStateSelectionContractError, "uniquely match"
        ):
            validate_excited_state_selection_contract(selected, changed)


if __name__ == "__main__":
    unittest.main()
