from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

from cmw.core.provenance import stable_hash
from cmw.periodic.vasp.result_policy import assess_result, load_policy, validate_policy
from cmw.periodic.vasp.results import inspect_result
from .result_fixtures import write_run


def policy(*, relaxation=False, strict=True):
    return {
        "schema_version": 1, "name": "synthetic explicit acceptance",
        "calculation": "fixed-cell-relaxation" if relaxation else "static",
        "electronic": {"history": "all", "basis": "native-and-numeric", "criterion": "run-ediff"},
        "force": {"criterion": "run-ediffg", "scope": "free"} if relaxation else None,
        "endpoint": {"position_tolerance_angstrom": 2e-5, "cell_tolerance_angstrom": 2e-6},
        "require_execution": strict, "require_input_binding": strict,
        "artifact_roles": ["periodic-structure"] if relaxation else ["energy:free_energy"],
    }


BOUND = {"valid": True, "input_binding": True, "input_comparison_valid": True}
EXECUTED = {"status": "eligible", "exit_code": 0}
WHOLE_FLAGS = [[True, True, True], [False, False, False]]


class ResultPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def evidence(self, **options):
        return inspect_result(write_run(self.root / "run", **options))

    def assess(self, evidence, selected=None, **kwargs):
        return assess_result(evidence, selected or policy(), binding=kwargs.pop("binding", BOUND),
                             execution=kwargs.pop("execution", EXECUTED), **kwargs)

    def checks(self, result):
        return {item["code"]: item for item in result["checks"]}

    def test_static_both_criteria_pass_even_at_nelm_without_ionic_phrase(self):
        observed = self.evidence()
        result = self.assess(observed)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(observed["selected_segment"]["evaluations"][0]["budget_reached"])
        self.assertEqual(result["policy_identity"]["policy_id"], stable_hash(policy()))
        self.assertEqual(self.checks(result)["ionic.relaxation"]["status"], "NOT_APPLICABLE")
        self.assertNotIn("ionic.native_stop", self.checks(result))

    def test_constrained_free_scope_passes_while_all_atoms_rejects(self):
        observed = self.evidence(relaxation=True, flags=WHOLE_FLAGS)
        selected = policy(relaxation=True)
        result = self.assess(observed, selected)
        self.assertEqual(result["status"], "PASS", result)
        counts = self.checks(result)["forces.accounting"]["evidence"]
        self.assertEqual(counts["fixed"]["maximum_norm"], 0.5)
        self.assertEqual(counts["free"]["count"], 1)
        selected["force"]["scope"] = "all"
        result = self.assess(observed, selected)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.checks(result)["forces.threshold"]["status"], "FAIL")

    def test_no_constraints_and_explicit_force_threshold(self):
        observed = self.evidence(relaxation=True, forces=((0.01, 0, 0), (0.012, 0, 0)))
        selected = policy(relaxation=True)
        selected["force"] = {"criterion": "explicit", "scope": "all", "threshold_ev_per_angstrom": 0.015}
        self.assertEqual(self.assess(observed, selected)["status"], "PASS")
        selected["force"]["threshold_ev_per_angstrom"] = 0.011
        self.assertEqual(self.assess(observed, selected)["status"], "FAIL")

    def test_final_only_does_not_require_earlier_convergence(self):
        observed = self.evidence(relaxation=True, evaluations=2, flags=WHOLE_FLAGS)
        first = observed["selected_segment"]["evaluations"][0]
        first.update(native_convergence=False, numeric_convergence="FAIL")
        selected = policy(relaxation=True)
        self.assertEqual(self.assess(observed, selected)["status"], "FAIL")
        selected["electronic"]["history"] = "final"
        result = self.assess(observed, selected)
        self.assertEqual(result["status"], "PASS", result)
        self.assertNotIn("electronic.evaluation_0.native", self.checks(result))
        observed["conflicts"].append({"code": "material_contradiction"})
        self.assertEqual(self.assess(observed, selected)["status"], "FAIL")

    def test_native_numeric_and_both_are_distinct_permitted_bases(self):
        observed = self.evidence(native=None)
        selected = policy()
        self.assertEqual(self.assess(observed, selected)["status"], "UNKNOWN")
        selected["electronic"]["basis"] = "numeric"
        self.assertEqual(self.assess(observed, selected)["status"], "PASS")
        observed = self.evidence()
        (self.root / "run" / "OSZICAR").unlink()
        observed = inspect_result(self.root / "run")
        selected["electronic"]["basis"] = "native"
        self.assertEqual(self.assess(observed, selected)["status"], "PASS")
        selected["electronic"]["basis"] = "native-and-numeric"
        self.assertEqual(self.assess(observed, selected)["status"], "UNKNOWN")

    def test_fixed_count_and_energy_stopping_are_unsupported(self):
        self.assertEqual(self.assess(self.evidence(ediff="0"))["status"], "UNSUPPORTED")
        for threshold in ("0.02", "0"):
            observed = self.evidence(relaxation=True, flags=WHOLE_FLAGS, ediffg=threshold)
            self.assertEqual(self.assess(observed, policy(relaxation=True))["status"], "UNSUPPORTED")

    def test_rounding_at_electronic_and_force_boundaries_stays_unknown(self):
        observed = self.evidence(terminal_de="-.10000000E-05")
        self.assertEqual(self.assess(observed)["status"], "UNKNOWN")
        observed = self.evidence(relaxation=True, flags=WHOLE_FLAGS,
                                 forces=((0.020000, 0, 0), (0.3, 0.4, 0)))
        result = self.assess(observed, policy(relaxation=True))
        self.assertEqual(result["status"], "UNKNOWN")
        force = self.checks(result)["forces.threshold"]["evidence"]
        self.assertAlmostEqual(force["upper_bound"], .02 + math.sqrt(3) * .0000005)
        self.assertLess(force["lower_bound"], .02)

    def test_partial_and_all_fixed_constraints_cannot_pass(self):
        observed = self.evidence(relaxation=True, flags=[[True, False, True], [False] * 3])
        self.assertEqual(self.assess(observed, policy(relaxation=True))["status"], "UNSUPPORTED")
        observed = self.evidence(relaxation=True, flags=[[False] * 3] * 2)
        result = self.assess(observed, policy(relaxation=True))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.checks(result)["forces.nonempty_free_set"]["status"], "UNKNOWN")

    def test_strict_unbound_is_unknown_and_known_invalid_bindings_fail(self):
        observed = self.evidence()
        result = assess_result(observed, policy())
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.checks(result)["electronic.evaluation_0.numeric"]["status"], "PASS")
        self.assertEqual(self.assess(observed, binding={**BOUND, "valid": False})["status"], "FAIL")
        self.assertEqual(self.assess(observed, execution={"status": "invalid"})["status"], "FAIL")
        self.assertEqual(self.assess(observed, binding={**BOUND, "input_comparison_valid": False})["status"], "FAIL")

    def test_explicit_diagnostic_policy_can_accept_without_execution(self):
        observed = self.evidence()
        self.assertEqual(assess_result(observed, policy(strict=False))["status"], "PASS")
        observed = self.evidence(relaxation=True, flags=WHOLE_FLAGS)
        result = assess_result(observed, policy(relaxation=True, strict=False))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("inputs.input_binding", self.checks(result))

    def test_complete_sources_and_endpoint_correspondence_are_mandatory(self):
        observed = self.evidence()
        for field, value, expected in (("all_sources_complete", False, "UNKNOWN"),
                                       ("all_sources_stable", False, "FAIL"),
                                       ("full_electronic_history", False, "UNKNOWN")):
            changed = deepcopy(observed)
            changed["coverage"][field] = value
            self.assertEqual(self.assess(changed)["status"], expected)
        observed["endpoint"]["is_last_complete_evaluation"] = False
        self.assertEqual(self.assess(observed)["status"], "UNKNOWN")

    def test_termination_and_incomplete_evaluations_cannot_pass(self):
        observed = self.evidence(footer=False)
        self.assertEqual(self.assess(observed)["status"], "UNKNOWN")
        observed = self.evidence()
        for key in ("fatal", "requested_stop"):
            changed = deepcopy(observed)
            changed["selected_segment"]["termination"][key] = [{"line_start": 5}]
            self.assertEqual(self.assess(changed)["status"], "FAIL")
        observed["selected_segment"]["trailing_incomplete"] = True
        selected = policy()
        selected["electronic"]["history"] = "final"
        self.assertEqual(self.assess(observed, selected)["status"], "UNKNOWN")

    def test_stricter_endpoint_tolerance_is_applied_and_constraints_match(self):
        observed = self.evidence()
        observed["filesystem_inputs"]["POSCAR"]["cartesian_coordinates"][0][0] += 1e-5
        self.assertEqual(self.assess(observed)["status"], "PASS")
        selected = policy()
        selected["endpoint"]["position_tolerance_angstrom"] = 1e-6
        self.assertEqual(self.assess(observed, selected)["status"], "FAIL")
        observed = self.evidence(relaxation=True, flags=WHOLE_FLAGS)
        observed["constraints"]["contcar_flags_match"] = False
        self.assertEqual(self.assess(observed, policy(relaxation=True))["status"], "FAIL")

    def test_static_does_not_require_contcar_but_relaxation_does(self):
        observed = self.evidence()
        observed["filesystem_inputs"].pop("CONTCAR")
        self.assertEqual(self.assess(observed)["status"], "PASS")
        observed = self.evidence(relaxation=True, flags=WHOLE_FLAGS)
        observed["filesystem_inputs"].pop("CONTCAR")
        self.assertEqual(self.assess(observed, policy(relaxation=True))["status"], "UNKNOWN")

    def test_policy_cannot_claim_more_precision_than_printed_native_geometry(self):
        observed = self.evidence()
        for tolerance in ("position_tolerance_angstrom", "cell_tolerance_angstrom"):
            selected = policy()
            selected["endpoint"][tolerance] = 1e-12
            with self.subTest(tolerance=tolerance):
                result = self.assess(observed, selected)
                self.assertEqual(result["status"], "UNKNOWN")
                comparison = self.checks(result)["endpoint.static_poscar"]
                self.assertEqual(comparison["status"], "UNKNOWN")
                self.assertEqual(comparison["evidence"]["reason"], "requested tolerance is below native printed resolution")
        observed = self.evidence(relaxation=True, flags=WHOLE_FLAGS)
        selected = policy(relaxation=True)
        selected["endpoint"]["position_tolerance_angstrom"] = 1e-12
        result = self.assess(observed, selected)
        self.assertEqual(self.checks(result)["endpoint.initial_poscar"]["status"], "UNKNOWN")
        self.assertEqual(self.checks(result)["endpoint.contcar"]["status"], "UNKNOWN")

    def test_requested_energy_kind_must_exist_and_be_finite(self):
        observed = self.evidence()
        selected = policy()
        selected["artifact_roles"] = ["energy:free_energy", "energy:without_entropy", "energy:sigma_to_zero"]
        result = self.assess(observed, selected)
        self.assertEqual(result["status"], "PASS")
        energies = observed["selected_segment"]["evaluations"][-1]["energies"]
        energies.pop("without_entropy")
        self.assertEqual(self.assess(observed, selected)["status"], "UNKNOWN")

    def test_assessment_is_pure_and_identity_changes_with_scientific_policy(self):
        observed, selected = self.evidence(), policy()
        before = deepcopy((observed, selected, BOUND, EXECUTED))
        first = self.assess(observed, selected)
        second = self.assess(observed, selected)
        self.assertEqual(first, second)
        self.assertEqual((observed, selected, BOUND, EXECUTED), before)
        selected["electronic"]["history"] = "final"
        self.assertNotEqual(first["policy_identity"], self.assess(observed, selected)["policy_identity"])

    def test_policy_schema_has_no_defaults_and_rejects_unknown_keys(self):
        selected = policy()
        for key in selected:
            changed = deepcopy(selected)
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_policy(changed)
        for key, value in (("schema_version", True), ("calculation", "md"),
                           ("require_execution", 1), ("artifact_roles", []),
                           ("artifact_roles", ["wavefunction"]),
                           ("artifact_roles", ["energy:free_energy"] * 2)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_policy({**selected, key: value})
        with self.assertRaises(ValueError):
            validate_policy({**selected, "force_valid": True})
        selected["electronic"]["expression"] = "True"
        with self.assertRaises(ValueError):
            validate_policy(selected)

    def test_force_units_threshold_and_tolerances_are_explicit_and_bounded(self):
        for threshold in (0, -1, True, float("inf"), float("nan")):
            selected = policy(relaxation=True)
            selected["force"] = {"criterion": "explicit", "scope": "free", "threshold_ev_per_angstrom": threshold}
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                validate_policy(selected)
        for tolerance in (True, 0, 1, float("nan")):
            selected = policy()
            selected["endpoint"]["position_tolerance_angstrom"] = tolerance
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                validate_policy(selected)
        selected = policy(relaxation=True)
        selected["force"]["threshold_ev_per_angstrom"] = 0.02
        with self.assertRaises(ValueError):
            validate_policy(selected)
        selected = policy()
        selected["artifact_roles"] = ["periodic-structure"]
        with self.assertRaises(ValueError):
            validate_policy(selected)

    def test_loaded_policy_has_separate_byte_and_semantic_identities(self):
        path = self.root / "policy.json"
        path.write_text(json.dumps(policy()))
        first, source = load_policy(path)
        path.write_text(json.dumps(policy(), indent=2))
        second, other_source = load_policy(path)
        self.assertEqual(stable_hash(first), stable_hash(second))
        self.assertNotEqual(source["sha256"], other_source["sha256"])
        self.assertEqual(source["role"], "acceptance_policy")
        path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError):
            load_policy(path)
        path.write_text('{"threshold":NaN}')
        with self.assertRaises(ValueError):
            load_policy(path)


if __name__ == "__main__":
    unittest.main()
