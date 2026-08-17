from __future__ import annotations

import unittest
from pathlib import Path

from cmw.molecular.orca.status import (
    ExecutionStatus,
    FrequencyPolicy,
    ScientificStatus,
    StageType,
    classify_execution,
    parse_orca_input_echo,
    parse_orca_output,
    validate_stage,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "orca"


class OrcaStatusTests(unittest.TestCase):
    def _evidence(self, name: str):
        return parse_orca_output((FIXTURES / name).read_text(encoding="utf-8"))

    def test_factual_parser_recovers_core_evidence(self) -> None:
        evidence = self._evidence("valid_opt.out")
        self.assertTrue(evidence.normal_termination)
        self.assertTrue(evidence.scf_converged)
        self.assertTrue(evidence.optimization_converged)
        self.assertAlmostEqual(evidence.final_energy_hartree or 0.0, -1.2345)
        self.assertEqual(evidence.charge, 0)
        self.assertEqual(evidence.multiplicity, 1)
        self.assertEqual(evidence.orca_version, "6.0.1")
        self.assertEqual(evidence.runtime_seconds, 62.5)

    def test_numbered_input_echo_excludes_later_diagnostic_banners(self) -> None:
        text = """\
Program Version 6.1.1
                             INPUT FILE
================================================================================
NAME = stage.inp
|  1> ! r2SCAN-3c Opt
|  2> %pal nprocs 8 end
|  3> * xyzfile 0 1 input.xyz
|  4>                 ****END OF INPUT****
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!   SERIOUS PROBLEM WITH INTERNALS - ANGLE IS APPROACHING 180 DEGREES       !
!                       REBUILDING A NEW SET OF INTERNALS                    !
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
"""

        self.assertEqual(parse_orca_input_echo(text), ("r2SCAN-3c Opt",))
        self.assertEqual(
            parse_orca_output(text).input_keyword_tokens,
            ("r2SCAN-3c", "Opt"),
        )

    def test_all_numbered_keyword_lines_are_preserved_in_order(self) -> None:
        text = """\
INPUT FILE
| 1> ! DLPNO-CCSD(T) def2-TZVPP
| 2> ! TightPNO LED
| 3> ****END OF INPUT****
INPUT FILE
| 1> ! CPCM(Water)
| 2> ****END OF INPUT****
"""

        self.assertEqual(
            parse_orca_input_echo(text),
            ("DLPNO-CCSD(T) def2-TZVPP", "TightPNO LED", "CPCM(Water)"),
        )

    def test_echo_header_disables_unbounded_legacy_fallback(self) -> None:
        text = """\
INPUT FILE
| 1> ! r2SCAN-3c Opt
| 2> %pal nprocs 8 end
| 9> ! B3LYP def2-SVP
"""

        self.assertEqual(parse_orca_input_echo(text), ())

    def test_legacy_keyword_snippet_remains_supported(self) -> None:
        text = """\
! DLPNO-CCSD(T) def2-TZVPP TightPNO LED
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!                 THIS IS A DECORATIVE OUTPUT MESSAGE                        !
"""

        self.assertEqual(
            parse_orca_input_echo(text),
            ("DLPNO-CCSD(T) def2-TZVPP TightPNO LED",),
        )

    def test_normal_termination_is_not_optimization_convergence(self) -> None:
        evidence = self._evidence("normal_but_invalid_opt.out")
        execution = classify_execution(evidence, process_exit_code=0)
        scientific = validate_stage(evidence, StageType.OPT, execution=execution)
        self.assertEqual(execution.status, ExecutionStatus.SUCCESS)
        self.assertEqual(scientific.status, ScientificStatus.NOT_CONVERGED)

    def test_truncated_and_running_outputs_are_distinct(self) -> None:
        evidence = self._evidence("truncated.out")
        self.assertEqual(classify_execution(evidence).status, ExecutionStatus.INCOMPLETE)
        self.assertEqual(
            classify_execution(evidence, process_running=True).status, ExecutionStatus.RUNNING
        )

    def test_fatal_evidence_wins_over_nonzero_exit(self) -> None:
        evidence = self._evidence("fatal.out")
        result = classify_execution(evidence, process_exit_code=2)
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertIn("fatal evidence", result.reason)

    def test_valid_single_point_requires_energy_scf_and_termination(self) -> None:
        evidence = self._evidence("valid_sp.out")
        result = validate_stage(
            evidence,
            StageType.SP,
            execution=classify_execution(evidence, process_exit_code=0),
        )
        self.assertTrue(result.valid)

    def test_frequency_policy_is_explicit(self) -> None:
        evidence = self._evidence("frequency_with_small_imaginary.out")
        execution = classify_execution(evidence, process_exit_code=0)
        evidence_only = validate_stage(evidence, StageType.FREQ, execution=execution)
        strict = validate_stage(
            evidence,
            StageType.FREQ,
            execution=execution,
            frequency_policy=FrequencyPolicy(require_minimum=True, imaginary_tolerance_cm1=0.0),
        )
        tolerant = validate_stage(
            evidence,
            StageType.FREQ,
            execution=execution,
            frequency_policy=FrequencyPolicy(require_minimum=True, imaginary_tolerance_cm1=10.0),
        )
        self.assertTrue(evidence_only.valid)
        self.assertEqual(strict.status, ScientificStatus.INVALID)
        self.assertTrue(tolerant.valid)

    def test_incomplete_frequency_section_is_invalid(self) -> None:
        evidence = self._evidence("normal_but_invalid_freq.out")
        result = validate_stage(evidence, StageType.FREQ)
        self.assertEqual(result.status, ScientificStatus.INVALID)

    def test_scf_failure_is_not_accepted(self) -> None:
        evidence = self._evidence("normal_but_invalid_scf.out")
        result = validate_stage(evidence, StageType.SP)
        self.assertEqual(result.status, ScientificStatus.NOT_CONVERGED)


if __name__ == "__main__":
    unittest.main()
