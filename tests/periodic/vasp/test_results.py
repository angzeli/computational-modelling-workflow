from __future__ import annotations

import math
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.results import (
    below_printed, compare_geometry, inspect_result, number, parse_oszicar,
)
from .result_fixtures import CELL, POSITIONS, SKEW_CELL, native_texts, structure_text, write_run


class ResultEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = self.root / "run"

    def inspect(self, **options):
        write_run(self.run, **options)
        return inspect_result(self.run)

    def mutate(self, role, old, new):
        path = self.run / role
        self.assertIn(old, path.read_text())
        path.write_text(path.read_text().replace(old, new))

    def codes(self, result):
        return {item["code"] for item in result["conflicts"]}

    def test_static_complete_distinguishes_energy_roles_without_policy_or_exit(self):
        result = self.inspect()
        self.assertEqual(result["status"], "inspected", result)
        self.assertTrue(result["mode"]["supported"])
        self.assertEqual(result["mode"]["calculation"], "static")
        self.assertIsNone(result["policy_assessment"])
        self.assertIsNone(result["execution"]["exit_code"])
        evaluation = result["selected_segment"]["evaluations"][0]
        self.assertEqual(evaluation["energies"]["free_energy"]["value"], -3)
        self.assertEqual(evaluation["energies"]["without_entropy"]["value"], -2.99)
        self.assertEqual(evaluation["energies"]["sigma_to_zero"]["value"], -2.995)
        self.assertEqual(evaluation["iterations"][-1]["electronic_free_energy"]["value"], -2.10000005)
        self.assertEqual(evaluation["numeric_convergence"], "PASS")
        self.assertTrue(evaluation["native_convergence"])
        self.assertEqual(result["electronic_table_correspondence"], "matched")
        self.assertTrue(result["selected_segment"]["termination"]["normal_footer"])
        self.assertTrue(result["coverage"]["full_electronic_history"])
        self.assertEqual(result["endpoint"]["comparisons"]["endpoint_poscar"]["status"], "match")

    def test_convergence_at_nelm_is_not_failure_and_missing_native_stays_missing(self):
        result = self.inspect(nelm=3)
        evaluation = result["selected_segment"]["evaluations"][0]
        self.assertTrue(evaluation["budget_reached"])
        self.assertEqual(evaluation["numeric_convergence"], "PASS")
        result = self.inspect(native=None)
        self.assertIsNone(result["selected_segment"]["evaluations"][0]["native_convergence"])

    def test_nonconverged_normal_exit_is_separate_from_scientific_evidence(self):
        result = self.inspect(native=False, terminal_de="-.10000000E-02")
        evaluation = result["selected_segment"]["evaluations"][0]
        self.assertTrue(result["selected_segment"]["termination"]["normal_footer"])
        self.assertFalse(evaluation["native_convergence"])
        self.assertEqual(evaluation["numeric_convergence"], "FAIL")
        self.assertEqual(evaluation["numeric_checks"]["d_eps"], "PASS")

    def test_both_electronic_deltas_are_required(self):
        result = self.inspect(terminal_deps="-.10000000E-02")
        self.assertIn("native_numeric_convergence_conflict", self.codes(result))
        self.assertEqual(result["selected_segment"]["evaluations"][0]["numeric_convergence"], "FAIL")

    def test_printed_threshold_ambiguity_is_not_a_numeric_pass(self):
        result = self.inspect(terminal_de="-.1000E-05")
        evaluation = result["selected_segment"]["evaluations"][0]
        self.assertTrue(evaluation["native_convergence"])
        self.assertEqual(evaluation["numeric_checks"]["dE"], "UNKNOWN")
        self.assertEqual(evaluation["numeric_convergence"], "UNKNOWN")

    def test_zero_ediff_is_not_a_convergence_pass(self):
        result = self.inspect(ediff="0", native=None)
        self.assertEqual(result["selected_segment"]["evaluations"][0]["numeric_convergence"], "NOT_APPLICABLE")

    def test_fixed_free_forces_and_evaluation_coverage(self):
        flags = ((True, True, True), (False, False, False))
        result = self.inspect(relaxation=True, evaluations=2, flags=flags)
        self.assertEqual(result["mode"]["calculation"], "fixed-cell-relaxation")
        self.assertEqual(len(result["selected_segment"]["evaluations"]), 2)
        force = result["endpoint"]["force_summary"]
        self.assertAlmostEqual(force["free"]["maximum_norm"], math.sqrt(.000113))
        self.assertAlmostEqual(force["fixed"]["maximum_norm"], .5)
        self.assertEqual(force["all"]["maximum_atom_index"], 1)
        self.assertEqual(force["free"]["atom_indices"], [0])
        self.assertEqual(force["fixed"]["atom_indices"], [1])
        self.assertEqual(result["endpoint"]["evaluation_index"], 1)
        self.assertTrue(result["endpoint"]["is_last_complete_evaluation"])
        self.assertEqual(result["endpoint"]["comparisons"]["endpoint_contcar"]["status"], "match")
        self.assertEqual(result["endpoint"]["comparisons"]["endpoint_poscar"]["status"], "mismatch")
        self.assertEqual(result["ionic"]["criterion_kind"], "force")
        self.assertIsNone(result["ionic"]["accepted_step_count"])
        self.assertEqual(result["constraints"]["input_binding"], "unestablished")

    def test_all_free_all_fixed_and_partial_flags_are_explicit(self):
        for flags, free, fixed, partial in ((None, 2, 0, False),
                (((False,) * 3,) * 2, 0, 2, False),
                (((True, False, True), (False,) * 3), 0, 1, True)):
            with self.subTest(flags=flags):
                result = self.inspect(relaxation=True, flags=flags)
                summary = result["endpoint"]["force_summary"]
                self.assertEqual(summary["free"]["count"], free)
                self.assertEqual(summary["fixed"]["count"], fixed)
                self.assertEqual(result["constraints"]["partial"], partial)
                if not free:
                    self.assertIsNone(summary["free"]["maximum_norm"])

    def test_positive_zero_and_negative_ediffg_remain_distinct(self):
        for value, kind in (("0.001", "energy"), ("0", "step_budget"), ("-0.01", "force")):
            self.assertEqual(self.inspect(relaxation=True, ediffg=value)["ionic"]["criterion_kind"], kind)

    def test_dav_rmm_columns_d_exponents_and_adjacent_signed_fields(self):
        for algorithm in ("DAV", "RMM"):
            with self.subTest(algorithm=algorithm):
                result = self.inspect(algorithm=algorithm)
                evaluation = result["selected_segment"]["evaluations"][0]
                self.assertEqual(evaluation["final_electronic_row"]["algorithm"], algorithm)
                self.assertNotIn("rms_c", evaluation["final_electronic_row"])
                self.assertIn("rms_c", evaluation["iterations"][0]["oszicar"])
                self.mutate("OSZICAR", "-0.50000000E-07 -0.40000000E-07", "-0.50000000D-07-0.40000000D-07")
                result = inspect_result(self.run)
                self.assertEqual(result["electronic_table_correspondence"], "matched")
                self.assertEqual(result["selected_segment"]["evaluations"][0]["numeric_convergence"], "PASS")

    def test_row_missing_field_and_nonfinite_delta_cannot_pass(self):
        for value in ("NaN", "Inf", "*******"):
            with self.subTest(value=value):
                self.inspect(terminal_de=value)
                result = inspect_result(self.run)
                self.assertEqual(result["selected_segment"]["evaluations"][0]["numeric_convergence"], "UNKNOWN")
        self.inspect()
        self.mutate("OSZICAR", " 12 0.10000000E-03\n", " 12\n")
        self.assertEqual(inspect_result(self.run)["electronic_table_correspondence"], "conflict")

    def test_cross_file_different_energy_or_cycle_identity_conflicts(self):
        for old, new in (("-2.100000050000", "-8.100000050000"),
                         ("E0= -.29950000E+01", "E0= -.19950000E+01"),
                         ("   1 F=", "   9 F="),
                         ("DAV:   2", "DAV:   7")):
            with self.subTest(change=old):
                self.inspect()
                self.mutate("OSZICAR", old, new)
                result = inspect_result(self.run)
                self.assertIn("electronic_table_correspondence", self.codes(result))
                self.assertEqual(result["selected_segment"]["evaluations"][0]["numeric_convergence"], "UNKNOWN")

    def test_unknown_algorithm_and_nonfinite_earlier_row_are_retained(self):
        self.inspect()
        self.mutate("OSZICAR", "DAV:   2", "ODD:   2")
        result = inspect_result(self.run)
        self.assertFalse(result["mode"]["supported"])
        self.assertIn("unsupported_table_algorithm", result["mode"]["reasons"])
        self.inspect()
        self.mutate("OSZICAR", "0.20000000E-02", "NaN")
        result = inspect_result(self.run)
        self.assertIn("nonfinite_electronic_values", self.codes(result))

    def test_missing_electronic_prefix_cannot_be_hidden_by_matching_tables(self):
        self.inspect()
        outcar = self.run / "OUTCAR"
        lines = outcar.read_text().splitlines(keepends=True)
        first = next(i for i, line in enumerate(lines) if "Iteration" in line)
        second = next(i for i in range(first + 1, len(lines)) if "Iteration" in lines[i])
        outcar.write_text("".join(lines[:first] + lines[second:]))
        oszicar = self.run / "OSZICAR"
        oszicar.write_text("".join(line for line in oszicar.read_text().splitlines(keepends=True) if not line.startswith("DAV:   1")))
        result = inspect_result(self.run)
        self.assertIn("missing_electronic_prefix", self.codes(result))

    def test_missing_table_never_infers_numeric_success(self):
        self.inspect()
        (self.run / "OSZICAR").unlink()
        result = inspect_result(self.run)
        self.assertEqual(result["electronic_table_correspondence"], "unavailable")
        self.assertEqual(result["selected_segment"]["evaluations"][0]["numeric_convergence"], "UNKNOWN")

    def test_explicit_stdout_is_supported_without_guessing_another_file(self):
        self.inspect()
        (self.run / "OSZICAR").rename(self.run / "stdout.log")
        self.assertEqual(inspect_result(self.run)["electronic_table_correspondence"], "unavailable")
        result = inspect_result(self.run, stdout=self.run / "stdout.log")
        self.assertEqual(result["electronic_table_correspondence"], "matched")
        self.assertIn("stdout", [source["role"] for source in result["sources"]])

    def test_partial_electronic_loop_does_not_invent_force_endpoint(self):
        self.inspect()
        path = self.run / "OUTCAR"
        path.write_text(path.read_text().split("------------------------ aborting loop")[0])
        result = inspect_result(self.run)
        self.assertFalse(result["coverage"]["full_electronic_history"])
        self.assertIsNone(result["endpoint"]["geometry"])
        self.assertFalse(result["selected_segment"]["termination"]["normal_footer"])

    def test_truncated_force_or_energy_block_is_incomplete(self):
        for boundary in ("2.00000 2.50000 3.00000", " energy  without entropy="):
            with self.subTest(boundary=boundary):
                self.inspect()
                path = self.run / "OUTCAR"
                path.write_text(path.read_text().split(boundary)[0])
                result = inspect_result(self.run)
                self.assertFalse(result["coverage"]["full_electronic_history"])
                self.assertIsNone(result["endpoint"]["geometry"])

    def test_nonfinite_evaluated_energy_or_force_never_completes(self):
        for old, new in (("free  energy   TOTEN = -3.00000000", "free  energy   TOTEN = NaN"),
                         ("0.300000 0.400000 0.000000", "Inf 0.400000 0.000000")):
            with self.subTest(old=old):
                self.inspect()
                self.mutate("OUTCAR", old, new)
                result = inspect_result(self.run)
                self.assertFalse(result["selected_segment"]["evaluations"][0]["complete"])
                self.assertIsNone(result["endpoint"]["geometry"])

    def test_footer_marker_alone_does_not_prove_terminal_output(self):
        self.inspect()
        path = self.run / "OUTCAR"
        path.write_text(path.read_text().split(" Total CPU time used")[0])
        result = inspect_result(self.run)
        self.assertFalse(result["selected_segment"]["termination"]["normal_footer"])
        self.assertIsNotNone(result["selected_segment"]["termination"]["footer_reference"])

    def test_appended_segments_require_selection_and_do_not_hide_failed_tail(self):
        self.inspect()
        later = native_texts(native=False, footer=False)
        for role in ("OUTCAR", "OSZICAR"):
            with (self.run / role).open("a") as stream:
                stream.write(later[role])
        result = inspect_result(self.run)
        self.assertEqual(result["status"], "selection_required")
        self.assertEqual(len(result["segments"]), 2)
        earlier = inspect_result(self.run, segment=0)
        self.assertFalse(earlier["endpoint"]["is_final_segment"])
        final = inspect_result(self.run, segment=1)
        self.assertFalse(final["selected_segment"]["evaluations"][0]["native_convergence"])
        self.assertFalse(final["selected_segment"]["termination"]["normal_footer"])
        self.assertEqual(inspect_result(self.run, segment=2)["status"], "invalid")

    def test_complete_evaluation_followed_by_partial_evaluation_is_not_final(self):
        self.inspect(relaxation=True)
        with (self.run / "OUTCAR").open("a") as stream:
            stream.write("\n---------------- Iteration 2( 1) ----------------\n free energy TOTEN = -2.0 eV\n")
        result = inspect_result(self.run)
        self.assertFalse(result["endpoint"]["is_last_complete_evaluation"])
        self.assertFalse(result["coverage"]["full_electronic_history"])
        self.assertIn("evaluation_after_footer", self.codes(result))

    def test_extra_force_rows_and_missing_terminator_are_conflicts(self):
        for replacement in (" 9.0 9.0 9.0 0.0 0.0 0.0\n", " arbitrary unrecognized line\n"):
            self.inspect()
            original = "0.300000 0.400000 0.000000\n"
            self.mutate("OUTCAR", original, original + replacement)
            self.assertIn("force_block_terminator", self.codes(inspect_result(self.run)))

    def test_fatal_error_and_requested_stop_are_independent_facts(self):
        self.inspect()
        with (self.run / "OUTCAR").open("a") as stream:
            stream.write("VERY BAD NEWS: synthetic failure\n STOPCAR found: stop requested\n")
        termination = inspect_result(self.run)["selected_segment"]["termination"]
        self.assertTrue(termination["normal_footer"])
        self.assertEqual(len(termination["fatal"]), 1)
        self.assertEqual(len(termination["requested_stop"]), 1)

    def test_stale_reordered_and_missing_contcar_are_not_endpoint_matches(self):
        self.inspect(relaxation=True)
        (self.run / "CONTCAR").write_text(structure_text())
        result = inspect_result(self.run)
        self.assertEqual(result["endpoint"]["comparisons"]["endpoint_contcar"]["status"], "mismatch")
        (self.run / "CONTCAR").write_text(structure_text(POSITIONS[::-1]))
        self.assertEqual(inspect_result(self.run)["endpoint"]["comparisons"]["endpoint_contcar"]["status"], "mismatch")
        (self.run / "CONTCAR").unlink()
        self.assertEqual(inspect_result(self.run)["endpoint"]["comparisons"]["endpoint_contcar"]["status"], "unavailable")

    def test_periodic_translations_match_in_skew_cell(self):
        self.inspect(cell=SKEW_CELL)
        translated = tuple(tuple(x + SKEW_CELL[1][axis] - SKEW_CELL[2][axis]
                                 for axis, x in enumerate(row)) for row in POSITIONS)
        (self.run / "CONTCAR").write_text(structure_text(translated, cell=SKEW_CELL))
        result = inspect_result(self.run)
        comparison = result["endpoint"]["comparisons"]["endpoint_contcar"]
        self.assertEqual(comparison["status"], "match", comparison)
        self.assertLess(comparison["max_displacement_angstrom"], 1e-12)

    def test_transient_cell_change_is_not_erased_by_final_cell_restoration(self):
        self.inspect(relaxation=True)
        changed = " direct lattice vectors reciprocal lattice vectors\n4.1 0 0 0 0 0\n0 5 0 0 0 0\n0 0 6 0 0 0\n"
        restored = " direct lattice vectors reciprocal lattice vectors\n4 0 0 0 0 0\n0 5 0 0 0 0\n0 0 6 0 0 0\n"
        self.mutate("OUTCAR", " POSITION", changed + restored + " POSITION")
        result = inspect_result(self.run)
        self.assertEqual(result["endpoint"]["comparisons"]["initial_poscar"]["status"], "match")
        self.assertEqual(result["endpoint"]["comparisons"]["fixed_cell"]["status"], "mismatch")

    def test_constraint_mismatch_is_retained_without_assuming_input_binding(self):
        self.inspect(relaxation=True, flags=((True,) * 3, (False,) * 3))
        self.mutate("CONTCAR", "F F F", "T T T")
        result = inspect_result(self.run)
        self.assertFalse(result["constraints"]["contcar_flags_match"])
        self.assertEqual(result["constraints"]["input_binding"], "unestablished")

    def test_geometry_precision_is_bounded_and_cell_difference_not_ignored(self):
        left = {"cell": CELL, "species": ["H", "He"], "cartesian": POSITIONS}
        for displacement, status in ((1e-5, "match"), (4e-5, "mismatch")):
            right = {**left, "cartesian": [(POSITIONS[0][0] + displacement, *POSITIONS[0][1:]), POSITIONS[1]]}
            self.assertEqual(compare_geometry(left, right)["status"], status)
        self.assertEqual(compare_geometry(left, {**left, "cell": ((4.01, 0, 0), CELL[1], CELL[2])})["status"], "mismatch")

    def test_unsupported_version_modes_and_missing_mode_evidence(self):
        for changes in ({"version": "5.4.4"}, {"settings": {"LHFCALC": "T"}},
                        {"settings": {"LSORBIT": "T"}}, {"settings": {"LNONCOLLINEAR": "T"}},
                        {"settings": {"ICHARG": "11"}}, {"settings": {"ISPIN": "4"}},
                        {"settings": {"NSW": "5", "IBRION": "0"}},
                        {"settings": {"NSW": "5", "IBRION": "2", "ISIF": "3"}},
                        {"settings": {"ML_LMLFF": "T"}}, {"settings": {"IMAGES": "3"}},
                        {"settings": {"LEPSILON": "T"}}, {"settings": {"EFIELD": "0.1"}}):
            with self.subTest(changes=changes):
                result = self.inspect(**changes)
                self.assertFalse(result["mode"]["supported"])
                self.assertEqual(result["status"], "unsupported")
        self.inspect()
        self.mutate("OUTCAR", "LHFCALC = F\n", "")
        self.assertIn("missing_mode_evidence:LHFCALC", inspect_result(self.run)["mode"]["reasons"])

    def test_vector_controls_with_first_component_zero_are_not_treated_as_absent(self):
        for control in ("EFIELD_PEAD", "EFOR", "LATTICE_CONSTRAINTS"):
            result = self.inspect(settings={control: "0 1 0"})
            self.assertFalse(result["mode"]["supported"])
            self.assertIn("unsupported_control:" + control, result["mode"]["reasons"])

    def test_species_accounting_mismatch_is_reported(self):
        self.inspect()
        self.mutate("OUTCAR", "ions per type = 1 1", "ions per type = 1 2")
        result = inspect_result(self.run)
        self.assertIn("native_species_count_mismatch", self.codes(result))

    def test_collinear_spin_mode_preserves_evidence(self):
        result = self.inspect(settings={"ISPIN": "2"})
        self.assertTrue(result["mode"]["supported"])
        self.assertEqual(result["selected_segment"]["settings"]["ISPIN"]["value"], 2)

    def test_effective_settings_win_over_changed_filesystem_incar(self):
        self.inspect()
        self.mutate("INCAR", "NELM = 3", "NELM = 500")
        self.mutate("INCAR", "EDIFF = 0.10000000E-05", "EDIFF = 0.1")
        result = inspect_result(self.run)
        evaluation = result["selected_segment"]["evaluations"][0]
        self.assertEqual(evaluation["effective_nelm"], 3)
        self.assertEqual(evaluation["effective_ediff"], 1e-6)
        self.assertEqual({item["tag"] for item in result["input_comparison"]}, {"NELM", "EDIFF"})

    def test_unknown_and_nonfinite_printed_numbers(self):
        self.assertEqual(number("-.2D-04")["value"], -2e-5)
        self.assertAlmostEqual(number("1.000E-6")["half_last_place"], 5e-10)
        for token in ("NaN", "Infinity", "-Inf", "*******", "1E99999"):
            self.assertIsNone(number(token)["value"])
        self.assertEqual(below_printed(number("1.000E-6"), 1e-6), "UNKNOWN")
        self.assertEqual(below_printed(number(".999E-6"), 1e-6), "PASS")
        self.assertEqual(below_printed(number("1.001E-6"), 1e-6), "FAIL")

    def test_source_byte_ranges_and_identity_are_complete_and_read_only(self):
        self.inspect()
        before = {path.name: path.read_bytes() for path in self.run.iterdir()}
        first = inspect_result(self.run)
        second = inspect_result(self.run)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertTrue(first["coverage"]["all_sources_complete"])
        ref = first["selected_segment"]["evaluations"][0]["energies"]["free_energy"]["reference"]
        self.assertIn(b"TOTEN", before["OUTCAR"][ref["byte_start"]:ref["byte_end"]])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.run.iterdir()})
        self.assertEqual({"OUTCAR", "OSZICAR", "POSCAR", "CONTCAR", "INCAR"}, set(before))

    def test_bounded_prefix_is_labeled_incomplete(self):
        self.inspect()
        result = inspect_result(self.run, max_bytes=100)
        self.assertFalse(result["coverage"]["all_sources_complete"])

    def test_oszicar_trailing_loop_and_repeated_headers_keep_order(self):
        self.inspect(relaxation=True, evaluations=2)
        with (self.run / "OSZICAR").open("a") as stream:
            stream.write(" N E dE d eps ncg rms rms(c)\nDAV: 1 -1.0 -1.0 -.1 12 .01\n")
        tables, _ = parse_oszicar(self.run / "OSZICAR")
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]), 3)
        self.assertIsNone(tables[0][-1]["summary"])
        self.assertEqual(inspect_result(self.run)["electronic_table_correspondence"], "conflict")


if __name__ == "__main__":
    unittest.main()
