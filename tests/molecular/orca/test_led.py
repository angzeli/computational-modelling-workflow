from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from cmw.molecular.orca.led import (
    IntermolecularLEDResult,
    LEDReconstructionError,
    OrcaLEDParseError,
    UnsupportedOrcaLEDFormatError,
    assemble_interfragment_led,
    parse_orca_interfragment_led,
    parse_orca_interfragment_led_file,
    parse_orca_led_fragment_reference,
    parse_orca_led_fragment_reference_file,
)
from cmw.molecular.stacking.ground_state import HARTREE_TO_KJ_MOL


FIXTURES = Path(__file__).parents[2] / "fixtures" / "orca" / "led"


def _historical_result(name: str) -> IntermolecularLEDResult:
    dimer = parse_orca_interfragment_led_file(FIXTURES / f"{name}_dimer.out")
    references = tuple(
        parse_orca_led_fragment_reference_file(
            FIXTURES / f"{name}_fragment_{suffix}.out",
            fragment_id=fragment_id,
        )
        for suffix, fragment_id in (("a", "A"), ("b", "B"))
    )
    return assemble_interfragment_led(dimer, references)


class HistoricalOrcaLEDRegressionTests(unittest.TestCase):
    def test_h4tbapy_exact_three_calculation_reconstruction(self) -> None:
        result = _historical_result("h4tbapy")

        self.assertEqual(
            result.components.values_hartree,
            {
                "electronic_preparation": 0.22659934802891257,
                "electrostatics": -0.223630311,
                "exchange": -0.031776946,
                "non_dispersive_ccsd_correlation": 0.006812620999999908,
                "dispersion": -0.009286012,
                "triples": -0.0008819439999999956,
            },
        )
        self.assertAlmostEqual(result.reconstructed_total_kj_mol, -84.4445854506)
        self.assertAlmostEqual(
            result.independently_reconstructed_total_hartree,
            -0.03216353747694711,
        )
        self.assertAlmostEqual(
            result.reconstruction_residual_hartree,
            2.935059164460574e-7,
        )

    def test_h4tabapy_exact_three_calculation_reconstruction(self) -> None:
        result = _historical_result("h4tabapy")

        self.assertEqual(
            result.components.values_hartree,
            {
                "electronic_preparation": 0.20651367192294856,
                "electrostatics": -0.202787254,
                "exchange": -0.029577212,
                "non_dispersive_ccsd_correlation": 0.00659887399999981,
                "dispersion": -0.009208363,
                "triples": -0.0007853539999999881,
            },
        )
        self.assertAlmostEqual(result.reconstructed_total_kj_mol, -76.7844096022)
        self.assertAlmostEqual(
            result.reconstruction_residual_hartree,
            3.0180200595933404e-7,
        )

    def test_raw_orca_direct_total_remains_distinct_from_reconstructed_total(
        self,
    ) -> None:
        result = _historical_result("h4tbapy")

        self.assertEqual(
            result.dimer.orca_dimer_direct_total_interaction_hartree,
            -0.277230600348,
        )
        self.assertAlmostEqual(result.reconstructed_total_hartree, -0.0321632439711)
        self.assertNotAlmostEqual(
            result.dimer.orca_dimer_direct_total_interaction_hartree,
            result.reconstructed_total_hartree,
        )

    def test_raw_subcomponents_and_derived_units_are_preserved(self) -> None:
        result = _historical_result("h4tabapy")

        self.assertEqual(result.dimer.dispersion_strong_pairs_hartree, -0.008033781)
        self.assertEqual(result.dimer.dispersion_weak_pairs_hartree, -0.001174582)
        self.assertAlmostEqual(
            result.components.values_kj_mol["dispersion"],
            -0.009208363 * HARTREE_TO_KJ_MOL,
        )
        self.assertLess(
            abs(result.reconstruction_residual_hartree),
            result.reconstruction_tolerance_hartree,
        )

    def test_round_trip_preserves_parser_grammar_and_numerical_identity(self) -> None:
        result = _historical_result("h4tabapy")

        restored = IntermolecularLEDResult.from_mapping(result.to_dict())

        self.assertEqual(restored.to_dict(), result.to_dict())
        self.assertEqual(
            restored.dimer.source_output_identity["fixture_tested_grammar_version"],
            "orca_6_1_1_interfragment_led_v1",
        )


class OrcaLEDParserFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dimer_text = (FIXTURES / "h4tbapy_dimer.out").read_text(
            encoding="utf-8"
        )
        cls.reference_text = (FIXTURES / "h4tbapy_fragment_a.out").read_text(
            encoding="utf-8"
        )

    def test_missing_marker_only_and_truncated_sections_fail_closed(self) -> None:
        for text in (
            self.dimer_text.replace(
                "LOCAL ENERGY DECOMPOSITION FOR DLPNO-CC METHODS", "NO LED SECTION"
            ),
            "Program Version 6.1.1\nLOCAL ENERGY DECOMPOSITION\n"
            "ORCA TERMINATED NORMALLY\n",
            self.dimer_text.replace("#Time for LED analysis", "truncated"),
        ):
            with self.subTest(text=text[:40]):
                with self.assertRaises(OrcaLEDParseError):
                    parse_orca_interfragment_led(text)

    def test_duplicate_led_sections_are_ambiguous(self) -> None:
        start = self.dimer_text.index(
            "LOCAL ENERGY DECOMPOSITION FOR DLPNO-CC METHODS"
        )
        end = self.dimer_text.index("#Time for LED analysis")
        duplicated = self.dimer_text + self.dimer_text[start:end] + "\n"

        with self.assertRaisesRegex(OrcaLEDParseError, "ambiguous"):
            parse_orca_interfragment_led(duplicated)

    def test_missing_malformed_and_non_finite_components_fail_closed(self) -> None:
        for replacement in ("", "not-a-number", "NaN"):
            malformed = self.dimer_text.replace("-0.031776946", replacement, 1)
            with self.subTest(replacement=replacement):
                with self.assertRaises(OrcaLEDParseError):
                    parse_orca_interfragment_led(malformed)

    def test_unsupported_orca_version_fails_closed(self) -> None:
        with self.assertRaises(UnsupportedOrcaLEDFormatError):
            parse_orca_interfragment_led(
                self.dimer_text.replace("Program Version 6.1.1", "Program Version 6.0.1")
            )

    def test_source_output_hash_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(OrcaLEDParseError, "hash"):
            parse_orca_interfragment_led(
                self.dimer_text,
                source_sha256="0" * 64,
            )

    def test_fragment_reference_rejects_ambiguous_e0_and_led_content(self) -> None:
        ambiguous = self.reference_text.replace(
            "E(0)                                       ...   -457.532876805\n",
            "E(0)                                       ...   -457.532876805\n"
            "E(0)                                       ...   -1.000000000\n",
            1,
        )
        with self.assertRaisesRegex(OrcaLEDParseError, "ambiguous"):
            parse_orca_led_fragment_reference(ambiguous, fragment_id="A")
        with self.assertRaises(OrcaLEDParseError):
            parse_orca_led_fragment_reference(
                self.reference_text + "LOCAL ENERGY DECOMPOSITION FOR DLPNO-CC METHODS",
                fragment_id="A",
            )

    def test_unknown_stored_grammar_fails_closed(self) -> None:
        result = _historical_result("h4tbapy")
        stored = result.to_dict()
        stored["dimer_raw"]["parser_grammar"] = "unknown"  # type: ignore[index]

        with self.assertRaises(UnsupportedOrcaLEDFormatError):
            IntermolecularLEDResult.from_mapping(stored)

    def test_duplicate_fragment_identity_and_excessive_residual_fail(self) -> None:
        dimer = parse_orca_interfragment_led_file(FIXTURES / "h4tbapy_dimer.out")
        reference = parse_orca_led_fragment_reference_file(
            FIXTURES / "h4tbapy_fragment_a.out", fragment_id="A"
        )
        with self.assertRaisesRegex(LEDReconstructionError, "identities"):
            assemble_interfragment_led(dimer, (reference, reference))

        second = parse_orca_led_fragment_reference_file(
            FIXTURES / "h4tbapy_fragment_b.out", fragment_id="B"
        )
        inconsistent = replace(
            second,
            final_ccsd_t_energy_hartree=second.final_ccsd_t_energy_hartree + 0.01,
        )
        with self.assertRaisesRegex(LEDReconstructionError, "residual"):
            assemble_interfragment_led(dimer, (reference, inconsistent))


if __name__ == "__main__":
    unittest.main()
