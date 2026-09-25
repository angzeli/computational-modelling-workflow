"""Adversarial continuation coverage with invented H/He output only.

The production corpus has single-banner runs. These concatenations test the
documented generic boundary; they are not minimized production calculations.
"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.periodic.vasp.result_policy import assess_result
from cmw.periodic.vasp.results import inspect_result
from .result_fixtures import native_texts, structure_text
from .test_result_policy import policy


SAFE_FORCES = ((0.01, 0, 0), (0.012, 0, 0))
FIRST_POSITIONS = ((0.5, 1, 1.5), (2, 2.5, 3))
NEXT_POSITIONS = ((0.6, 1, 1.5), (2, 2.5, 3))


class ContinuationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = self.root / "run"
        self.run.mkdir()
        for target, message in (("subprocess.Popen", "No execution authorized"),
                                ("cmw.jobs.store.Store", "No mutable Jobs access")):
            guard = patch(target, side_effect=AssertionError(message))
            guard.start()
            self.addCleanup(guard.stop)

    def segment(self, *, positions=FIRST_POSITIONS, **options):
        return {**native_texts(relaxation=True, positions=positions,
                              forces=options.pop("forces", SAFE_FORCES), **options),
                "initial_positions": positions}

    def materialize(self, segments, *, endpoint=None):
        for role in ("OUTCAR", "OSZICAR"):
            (self.run / role).write_text("".join(item[role] for item in segments))
        current = segments[-1]
        (self.run / "INCAR").write_text(current["INCAR"])
        (self.run / "POSCAR").write_text(structure_text(current["initial_positions"]))
        (self.run / "CONTCAR").write_text(structure_text(current["endpoint"] if endpoint is None else endpoint))

    def assess(self, evidence, history="all"):
        selected = policy(relaxation=True, strict=False)
        selected["electronic"]["history"] = history
        return assess_result(evidence, selected)

    def checks(self, assessment):
        return {item["code"]: item["status"] for item in assessment["checks"]}

    def test_completed_success_then_failed_segment_never_accepts_earlier_result(self):
        first = self.segment()
        failed = self.segment(positions=NEXT_POSITIONS, native=False,
                              terminal_de="-.1", forces=((.2, 0, 0), (.3, 0, 0)))
        self.materialize([first, failed])
        self.assertEqual(inspect_result(self.run)["status"], "selection_required")
        earlier = inspect_result(self.run, segment=0)
        final = inspect_result(self.run, segment=1)
        self.assertFalse(earlier["endpoint"]["is_final_segment"])
        self.assertAlmostEqual(final["endpoint"]["geometry"]["cartesian"][0][0], .62)
        self.assertEqual(final["endpoint"]["force_summary"]["all"]["maximum_norm"], .3)
        for history in ("all", "final"):
            with self.subTest(history=history):
                self.assertEqual(self.checks(self.assess(earlier, history))["endpoint.final_segment"], "FAIL")
                assessment = self.assess(final, history)
                self.assertEqual(assessment["status"], "FAIL")
                self.assertEqual(self.checks(assessment)["electronic.evaluation_0.native"], "FAIL")
                self.assertEqual(self.checks(assessment)["forces.threshold"], "FAIL")

    def test_success_then_incomplete_segment_does_not_inherit_endpoint_or_footer(self):
        first = self.segment()
        interrupted = self.segment(positions=NEXT_POSITIONS)
        interrupted["OUTCAR"] = interrupted["OUTCAR"].split(" POSITION")[0]
        interrupted["OSZICAR"] = "\n".join(interrupted["OSZICAR"].splitlines()[:-1]) + "\n"
        self.materialize([first, interrupted], endpoint=first["endpoint"])
        evidence = inspect_result(self.run, segment=1)
        self.assertIsNone(evidence["endpoint"]["geometry"])
        self.assertIsNone(evidence["endpoint"]["force_summary"])
        self.assertFalse(evidence["selected_segment"]["termination"]["normal_footer"])
        self.assertFalse(evidence["coverage"]["full_electronic_history"])
        for history in ("all", "final"):
            self.assertNotEqual(self.assess(evidence, history)["status"], "PASS")
            self.assertNotEqual(self.assess(inspect_result(self.run, segment=0), history)["status"], "PASS")

    def test_failed_then_complete_success_can_accept_only_explicit_final_segment(self):
        failed = self.segment(native=False, terminal_de="-.1")
        succeeded = self.segment(positions=NEXT_POSITIONS, evaluations=2)
        self.materialize([failed, succeeded])
        self.assertEqual(inspect_result(self.run)["status"], "selection_required")
        evidence = inspect_result(self.run, segment=1)
        self.assertEqual(evidence["electronic_table_correspondence"], "matched")
        self.assertEqual(len(evidence["selected_segment"]["evaluations"]), 2)
        self.assertEqual(evidence["endpoint"]["evaluation_index"], 1)
        self.assertAlmostEqual(evidence["endpoint"]["geometry"]["cartesian"][0][0], .64)
        self.assertEqual(evidence["endpoint"]["comparisons"]["endpoint_contcar"]["status"], "match")
        for history in ("all", "final"):
            self.assertEqual(self.assess(evidence, history)["status"], "PASS")
            self.assertEqual(self.assess(inspect_result(self.run, segment=0), history)["status"], "FAIL")

    def test_trailing_repeated_native_header_requires_selection_without_old_endpoint(self):
        first = self.segment()
        empty = self.segment(positions=NEXT_POSITIONS, evaluations=0, footer=False)
        empty["OUTCAR"] = empty["OUTCAR"].replace(
            " reached required accuracy - stopping structural energy minimisation\n", "")
        self.materialize([first, empty], endpoint=first["endpoint"])
        choice = inspect_result(self.run)
        self.assertEqual(choice["status"], "selection_required")
        self.assertEqual([item["evaluation_count"] for item in choice["segments"]], [1, 0])
        current = inspect_result(self.run, segment=1)
        self.assertIsNone(current["endpoint"]["geometry"])
        self.assertFalse(current["coverage"]["full_electronic_history"])
        self.assertNotEqual(self.assess(current, "final")["status"], "PASS")
        self.assertEqual(self.checks(self.assess(inspect_result(self.run, segment=0)))["endpoint.final_segment"], "FAIL")

    def test_final_segment_cannot_bind_stale_contcar_from_earlier_success(self):
        first = self.segment()
        final = self.segment(positions=NEXT_POSITIONS)
        self.materialize([first, final], endpoint=first["endpoint"])
        evidence = inspect_result(self.run, segment=1)
        self.assertEqual(evidence["electronic_table_correspondence"], "matched")
        self.assertEqual(evidence["endpoint"]["comparisons"]["endpoint_contcar"]["status"], "mismatch")
        for history in ("all", "final"):
            assessment = self.assess(evidence, history)
            self.assertEqual(assessment["status"], "FAIL")
            self.assertEqual(self.checks(assessment)["endpoint.contcar"], "FAIL")

    def test_history_selection_is_applied_to_real_parsed_cycles_not_mutated_evidence(self):
        sequence = self.segment(evaluations=2)
        sequence["OUTCAR"] = sequence["OUTCAR"].replace(
            "aborting loop because EDIFF is reached", "aborting loop EDIFF was not reached (unconverged)", 1)
        for role in ("OUTCAR", "OSZICAR"):
            sequence[role] = sequence[role].replace("-2.100000050000", "-2.200000000000", 1)
        sequence["OSZICAR"] = sequence["OSZICAR"].replace("-0.50000000E-07", "-.10000000E+00", 1)
        self.materialize([sequence])
        evidence = inspect_result(self.run)
        self.assertEqual(evidence["electronic_table_correspondence"], "matched")
        self.assertEqual([row["native_convergence"] for row in evidence["selected_segment"]["evaluations"]], [False, True])
        self.assertEqual([row["numeric_convergence"] for row in evidence["selected_segment"]["evaluations"]], ["FAIL", "PASS"])
        self.assertEqual(self.assess(evidence, "all")["status"], "FAIL")
        final_only = self.assess(evidence, "final")
        self.assertEqual(final_only["status"], "PASS", final_only)
        self.assertNotIn("electronic.evaluation_0.native", self.checks(final_only))

    def test_final_only_history_cannot_select_previous_complete_ionic_evaluation(self):
        sequence = self.segment(evaluations=2)
        first_force, second_force = [index for index in range(len(sequence["OUTCAR"]))
                                     if sequence["OUTCAR"].startswith(" POSITION", index)]
        self.assertLess(first_force, second_force)
        sequence["OUTCAR"] = sequence["OUTCAR"][:second_force]
        sequence["OSZICAR"] = "\n".join(sequence["OSZICAR"].splitlines()[:-1]) + "\n"
        self.materialize([sequence], endpoint=((.52, 1, 1.5), (2, 2.5, 3)))
        evidence = inspect_result(self.run)
        self.assertEqual(evidence["electronic_table_correspondence"], "matched")
        self.assertEqual(evidence["endpoint"]["evaluation_index"], 0)
        self.assertFalse(evidence["endpoint"]["is_last_complete_evaluation"])
        for history in ("all", "final"):
            assessment = self.assess(evidence, history)
            self.assertNotEqual(assessment["status"], "PASS")
            self.assertEqual(self.checks(assessment)["endpoint.associated"], "UNKNOWN")

    def test_reordered_electronic_segments_conflict_even_when_last_outcar_is_valid(self):
        first = self.segment()
        final = self.segment(positions=NEXT_POSITIONS, evaluations=2)
        self.materialize([first, final])
        (self.run / "OSZICAR").write_text(final["OSZICAR"] + first["OSZICAR"])
        evidence = inspect_result(self.run, segment=1)
        self.assertEqual(evidence["electronic_table_correspondence"], "conflict")
        self.assertTrue(evidence["selected_segment"]["termination"]["normal_footer"])
        for history in ("all", "final"):
            assessment = self.assess(evidence, history)
            self.assertEqual(assessment["status"], "FAIL")
            self.assertEqual(self.checks(assessment)["evidence.coherent"], "FAIL")


if __name__ == "__main__":
    unittest.main()
