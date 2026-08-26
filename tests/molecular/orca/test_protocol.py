from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cmw.molecular.orca.protocol import (
    ProtocolIntent,
    ProtocolValidationStatus,
    validate_protocol,
)
from cmw.molecular.orca.status import StageType, parse_orca_output
from cmw.molecular.workflows.opt_freq_sp import load_workflow_config


FIXTURES = Path(__file__).parents[2] / "fixtures" / "orca"


class OrcaProtocolTests(unittest.TestCase):
    def _evidence(self, name: str):
        return parse_orca_output((FIXTURES / name).read_text(encoding="utf-8"))

    def test_dlpno_led_protocol_matches_explicit_output_evidence(self) -> None:
        intent = ProtocolIntent(
            method="DLPNO-CCSD(T)",
            basis="def2-TZVPP",
            auxiliary_basis={
                "correlation": "def2-TZVPP/C",
                "coulomb_exchange": "def2/JK",
            },
            reference_approximation="RIJK",
            pno="TightPNO",
            led=True,
            fragments_required=True,
            expected_fragments=2,
        )
        result = validate_protocol(
            self._evidence("protocol_dlpno_led.out"), intent, StageType.SP
        )
        self.assertEqual(result.status, ProtocolValidationStatus.PASSED)
        self.assertTrue(all(result.checks.values()))
        self.assertTrue(result.checks["auxiliary_basis_correlation_match"])
        self.assertTrue(result.checks["reference_approximation_match"])
        self.assertEqual(result.detected["fragment_count"], 2)

    def test_wrong_method_basis_and_missing_features_fail_closed(self) -> None:
        intent = ProtocolIntent(
            method="DLPNO-CCSD(T)",
            basis="def2-TZVPP",
            auxiliary_basis={"correlation": "def2-TZVPP/C"},
            reference_approximation="RIJK",
            pno="TightPNO",
            led=True,
            fragments_required=True,
        )
        result = validate_protocol(
            self._evidence("protocol_wrong_method.out"), intent, StageType.SP
        )
        self.assertEqual(
            result.status, ProtocolValidationStatus.FAILED_PROTOCOL_MISMATCH
        )
        self.assertFalse(result.checks["method_match"])
        self.assertFalse(result.checks["basis_match"])
        self.assertFalse(result.checks["auxiliary_basis_correlation_match"])
        self.assertFalse(result.checks["reference_approximation_match"])
        self.assertFalse(result.checks["pno_match"])
        self.assertFalse(result.checks["led_present"])
        self.assertIn("FAILED_PROTOCOL_MISMATCH", result.reason)

    def test_empty_optional_basis_does_not_create_a_protocol_requirement(self) -> None:
        intent = ProtocolIntent(basis="   ")
        result = validate_protocol(
            self._evidence("protocol_dlpno_led.out"), intent, StageType.SP
        )

        self.assertEqual(result.status, ProtocolValidationStatus.PASSED)
        self.assertIsNone(result.expected["basis"])
        self.assertNotIn("basis_match", result.checks)

    def test_workflow_config_records_method_intent_without_changing_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "workflow.json"
            stages = {
                name: {"keywords": "HF STO-3G", "blocks": []}
                for name in ("opt", "freq", "sp")
            }
            stages["sp"] = {
                "keywords": (
                    "DLPNO-CCSD(T) def2-TZVPP def2-TZVPP/C def2/JK "
                    "RIJK TightPNO"
                ),
                "blocks": [],
                "method": "DLPNO-CCSD(T)",
                "basis": "def2-TZVPP",
                "auxiliary_basis": {
                    "correlation": "def2-TZVPP/C",
                    "coulomb_exchange": "def2/JK",
                },
                "reference_approximation": "RIJK",
                "pno": "TightPNO",
                "led": True,
            }
            path.write_text(
                json.dumps({"schema_version": 1, "stages": stages}),
                encoding="utf-8",
            )
            config = load_workflow_config(path, env={})
        sp = config.stages[StageType.SP]
        self.assertIn("def2-TZVPP/C def2/JK RIJK", sp.keywords)
        self.assertEqual(sp.protocol["method"], "DLPNO-CCSD(T)")
        self.assertEqual(sp.protocol["basis"], "def2-TZVPP")
        self.assertEqual(
            sp.protocol["auxiliary_basis"]["correlation"], "def2-TZVPP/C"
        )
        self.assertIs(sp.protocol["led"], True)


if __name__ == "__main__":
    unittest.main()
