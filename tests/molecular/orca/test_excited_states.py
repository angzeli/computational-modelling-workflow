from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from cmw.molecular.orca.excited_states import (
    ExcitedStateJoinError,
    ExcitedStateParseError,
    UnsupportedExcitedStateFormatError,
    parse_orca_tda_excited_states,
    parse_orca_tda_excited_states_file,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "orca" / "excited_states"


class Orca611TDAParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.singlet_path = FIXTURES / "orca_6_1_1_tda_singlets.out"
        cls.mixed_path = FIXTURES / "orca_6_1_1_tda_mixed.out"
        cls.singlet_text = cls.singlet_path.read_text(encoding="utf-8")
        cls.mixed_text = cls.mixed_path.read_text(encoding="utf-8")
        cls.singlet = parse_orca_tda_excited_states_file(cls.singlet_path)
        cls.mixed = parse_orca_tda_excited_states_file(cls.mixed_path)

    def test_singlet_fixture_preserves_version_protocol_and_all_roots(self) -> None:
        result = self.singlet

        self.assertEqual(result.protocol.orca_version, "6.1.1")
        self.assertTrue(result.protocol.tda)
        self.assertEqual(result.protocol.theory, "TDA")
        self.assertEqual(result.protocol.functional, "WB97X-D3")
        self.assertEqual(result.protocol.basis, "6-31+G(d,p)")
        self.assertEqual(result.protocol.requested_singlet_roots, 30)
        self.assertEqual(result.protocol.requested_triplet_roots, 0)
        self.assertFalse(result.protocol.triplets_enabled)
        self.assertEqual(result.protocol.solvent_model, "SMD")
        self.assertEqual(result.protocol.solvent_name, "Water")
        self.assertAlmostEqual(result.protocol.tprint_threshold or 0.0, 1.0e-8)
        self.assertEqual(len(result.states), 30)
        self.assertTrue(result.normal_termination)
        self.assertTrue(result.excited_state_completion)

    def test_mixed_fixture_maps_global_state_16_to_canonical_t1(self) -> None:
        result = self.mixed
        t1 = result.state("triplet", 1)

        self.assertEqual(len(result.states), 30)
        self.assertEqual(result.protocol.requested_singlet_roots, 15)
        self.assertEqual(result.protocol.requested_triplet_roots, 15)
        self.assertEqual(t1.identity.label, "T1")
        self.assertEqual(t1.local_state_index, 1)
        self.assertEqual(t1.orca_global_state_index, 16)
        self.assertEqual(t1.multiplicity, 3)
        self.assertAlmostEqual(t1.s_squared or -1.0, 2.0)
        self.assertAlmostEqual(t1.detailed_energy_hartree or 0.0, 0.059425)
        self.assertAlmostEqual(t1.excitation_energy_ev, 1.617032)
        self.assertAlmostEqual(t1.wavelength_nm or 0.0, 766.7)
        self.assertEqual(t1.length_gauge_oscillator_strength, 0.0)
        self.assertEqual(t1.velocity_gauge_oscillator_strength, 0.0)
        self.assertEqual(t1.oscillator_strength, 0.0)

    def test_detailed_s1_transition_and_both_gauges_are_separate(self) -> None:
        s1 = self.mixed.state("singlet", 1)

        self.assertEqual(s1.orca_global_state_index, 1)
        self.assertEqual(s1.multiplicity, 1)
        self.assertEqual(s1.s_squared, 0.0)
        self.assertAlmostEqual(s1.detailed_energy_ev or 0.0, 2.763)
        self.assertAlmostEqual(s1.spectrum_energy_ev or 0.0, 2.763483)
        self.assertAlmostEqual(s1.wavelength_nm or 0.0, 448.7)
        self.assertAlmostEqual(s1.length_gauge_oscillator_strength or 0.0, 1.310538805)
        self.assertAlmostEqual(s1.velocity_gauge_oscillator_strength or 0.0, 0.244385946)
        self.assertEqual(s1.primary_oscillator_strength_gauge, "length")
        self.assertAlmostEqual(s1.oscillator_strength, 1.310538805)
        self.assertEqual(len(s1.orbital_transitions), 1)
        transition = s1.orbital_transitions[0]
        self.assertEqual((transition.source_orbital, transition.target_orbital), ("99a", "100a"))
        self.assertAlmostEqual(transition.contribution_weight, 0.952591)
        self.assertAlmostEqual(transition.coefficient, -0.97600780)

    def test_mixed_spectrum_is_joined_by_manifold_and_local_index(self) -> None:
        first_detailed = self.mixed.states[0]
        t1 = self.mixed.state("triplet", 1)

        self.assertEqual(first_detailed.identity.label, "S1")
        self.assertLess(t1.excitation_energy_ev, first_detailed.excitation_energy_ev)
        self.assertEqual(t1.orca_global_state_index, 16)

    def test_transition_summary_records_cutoff_without_discarding_rows(self) -> None:
        result = parse_orca_tda_excited_states(
            self.mixed_text,
            transition_weight_cutoff=0.90,
        )
        s1 = result.state("singlet", 1)

        self.assertEqual(len(s1.orbital_transitions), 1)
        self.assertIsNotNone(s1.transition_filter)
        assert s1.transition_filter is not None
        self.assertEqual(s1.transition_filter.total_parsed_count, 1)
        self.assertEqual(s1.transition_filter.retained_count, 1)
        self.assertEqual(s1.transition_filter.omitted_count, 0)

    def test_declared_protocol_is_checked(self) -> None:
        parsed = parse_orca_tda_excited_states(
            self.singlet_text,
            expected_protocol={
                "method": "WB97X-D3",
                "basis": "6-31+G(d,p)",
                "tda": True,
                "number_of_roots": 30,
                "solvent_name": "water",
            },
        )
        self.assertEqual(parsed.protocol.requested_singlet_roots, 30)
        with self.assertRaisesRegex(ExcitedStateParseError, "declared protocol"):
            parse_orca_tda_excited_states(
                self.singlet_text,
                expected_protocol={"basis": "def2-SVP"},
            )

    def test_documented_wb97x_d4_composite_identity_is_validated(self) -> None:
        text = self.singlet_text.replace("wB97X-D3", "wB97X-D4", 1)
        text = text.replace("WB97X-D3", "WB97X-V")
        text = text.replace(
            "Exchange functional (TD-DFT)",
            "DFTD4 V3.4.0\nExchange functional (TD-DFT)",
            1,
        )

        parsed = parse_orca_tda_excited_states(
            text,
            expected_protocol={
                "method": "wB97X-D4",
                "functional": "wB97X-D4",
                "basis": "6-31+G(d,p)",
            },
        )

        self.assertEqual(parsed.protocol.functional, "WB97X-V")
        self.assertEqual(parsed.protocol.dispersion_correction, "D4")

    def test_wb97x_d4_composite_validation_fails_closed(self) -> None:
        text = self.singlet_text.replace("wB97X-D3", "wB97X-D4", 1)
        base_only = text.replace("WB97X-D3", "WB97X-V")
        wrong_base = base_only.replace("WB97X-V", "PBE0")
        with_d4 = wrong_base.replace(
            "Exchange functional (TD-DFT)",
            "DFTD4 V3.4.0\nExchange functional (TD-DFT)",
            1,
        )

        for invalid in (base_only, with_d4):
            with self.subTest(d4_present="DFTD4" in invalid):
                with self.assertRaisesRegex(
                    ExcitedStateParseError, "declared protocol"
                ):
                    parse_orca_tda_excited_states(
                        invalid,
                        expected_protocol={"method": "wB97X-D4"},
                    )

    def test_incomplete_and_abnormally_terminated_outputs_fail_closed(self) -> None:
        without_completion = self.singlet_text.replace(
            "*** ORCA-CIS/TD-DFT FINISHED WITHOUT ERROR ***", "COMPLETION OMITTED"
        )
        without_termination = self.singlet_text.replace(
            "****ORCA TERMINATED NORMALLY****", "TERMINATION OMITTED"
        )
        with self.assertRaisesRegex(ExcitedStateParseError, "incomplete"):
            parse_orca_tda_excited_states(without_completion)
        with self.assertRaisesRegex(ExcitedStateParseError, "normal termination"):
            parse_orca_tda_excited_states(without_termination)

    def test_full_tddft_soc_and_unverified_versions_are_rejected(self) -> None:
        full_tddft = self.singlet_text.replace(
            "Tamm-Dancoff approximation     ... operative",
            "Tamm-Dancoff approximation     ... not operative",
        )
        soc = self.singlet_text + "\nSOC CORRECTED TD-DFT/TDA-EXCITATION SPECTRA\n"
        orca_60 = self.singlet_text.replace("Version 6.1.1", "Version 6.0.1")
        for text in (full_tddft, soc, orca_60):
            with self.subTest(text=text[-60:]):
                with self.assertRaises(UnsupportedExcitedStateFormatError):
                    parse_orca_tda_excited_states(text)

    def test_energy_mismatch_and_missing_authoritative_row_fail_closed(self) -> None:
        mismatch = self.mixed_text.replace(
            "1.617032   13042.2   766.7   0.000000000",
            "1.700000   13042.2   766.7   0.000000000",
            1,
        )
        missing = self.mixed_text.replace(
            "  0-1A  ->  1-3A    1.617032   13042.2   766.7   "
            "0.000000000   0.00000   0.00000   0.00000   0.00000\n",
            "",
            1,
        )
        with self.assertRaisesRegex(ExcitedStateJoinError, "energies disagree"):
            parse_orca_tda_excited_states(mismatch)
        with self.assertRaisesRegex(ExcitedStateJoinError, "identities do not match"):
            parse_orca_tda_excited_states(missing)

    def test_fixture_manifest_hashes_match_reduced_files(self) -> None:
        manifest = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        for item in manifest["fixtures"]:
            path = FIXTURES / item["reduced_fixture"]
            payload = path.read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), item["reduced_fixture_sha256"])
            self.assertEqual(len(payload), item["reduced_fixture_size_bytes"])
        self.assertIn("without numerical editing", manifest["statement"])


if __name__ == "__main__":
    unittest.main()
