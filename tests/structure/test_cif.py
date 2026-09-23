from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
from math import cos, radians, sin, sqrt
from pathlib import Path
import unittest
from unittest.mock import patch

from cmw.periodic.vasp.inputs import parse_poscar
from cmw.structure.cif import CifImportError, read_ordered_cif


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cif"


def fixture(name):
    return (FIXTURES / f"{name}.cif").read_bytes()


def without_operations(data):
    before = data.split(b"loop_\n_space_group_symop_id", 1)[0]
    sites = data.split(b"loop_\n_atom_site_label", 1)[1]
    return before + b"loop_\n_atom_site_label" + sites


class OrderedCifTests(unittest.TestCase):
    def assert_vectors(self, actual, expected, places=11):
        self.assertEqual(len(actual), len(expected))
        for vector, oracle in zip(actual, expected):
            self.assertEqual(len(vector), len(oracle))
            for value, wanted in zip(vector, oracle):
                self.assertAlmostEqual(value, wanted, places=places)

    def rejected(self, data, **kwargs):
        with self.assertRaises(CifImportError) as caught:
            read_ordered_cif(data, **kwargs)
        self.assertIsInstance(caught.exception.code, str)
        self.assertTrue(caught.exception.code)
        self.assertTrue(hasattr(caught.exception, "details"))
        return caught.exception

    def test_triclinic_conventional_cell_and_outside_source_fractions(self):
        data = fixture("p1_triclinic_outside")
        with patch("subprocess.Popen", side_effect=AssertionError("CIF import never launches engines")):
            result = read_ordered_cif(data)
        self.assert_vectors(result.structure.cell,
                            [[4, 0, 0], [2.5, 5 * sqrt(3) / 2, 0], [0, 2 * sqrt(3), 2 * sqrt(6)]])
        self.assert_vectors([atom.fractional for atom in result.structure.atoms],
                            [[.25, .75, .5], [.25, .5, .75]])
        self.assert_vectors([atom.cartesian for atom in result.structure.atoms],
                            [[2.875, 23 * sqrt(3) / 8, sqrt(6)], [2.25, 11 * sqrt(3) / 4, 3 * sqrt(6) / 2]])
        sites = result.metadata["source_sites"]
        self.assert_vectors([site["fractional"] for site in sites], [[1.25, -.25, .5], [.25, .5, .75]])
        self.assert_vectors([site["canonical_fractional"] for site in sites], [[.25, .75, .5], [.25, .5, .75]])
        self.assertEqual(result.structure.source_sha256, hashlib.sha256(data).hexdigest())
        parsed = parse_poscar(result.poscar)
        self.assertEqual(parsed["status"], "valid")
        self.assert_vectors(parsed["cell"], result.structure.cell)
        self.assert_vectors(parsed["cartesian_coordinates"], [atom.cartesian for atom in result.structure.atoms])
        with self.assertRaises(FrozenInstanceError):
            result.poscar = b"changed"

    def test_inversion_special_positions_and_both_mapping_directions(self):
        result = read_ordered_cif(fixture("pminus1_general_special_mapping"))
        atoms, sites = result.structure.atoms, result.metadata["source_sites"]
        self.assertEqual([atom.element for atom in atoms], ["C", "C", "C", "O"])
        self.assert_vectors([atom.fractional for atom in atoms],
                            [[.1, .2, .3], [.9, .8, .7], [.5, .5, .5], [0, 0, 0]])
        self.assertEqual([site["multiplicity"] for site in sites], [2, 1, 1])
        self.assertEqual([site["expanded_indices"] for site in sites], [[0, 1], [2], [3]])
        self.assertEqual([site["output_indices"] for site in sites], [[0, 1], [3], [2]])
        self.assertEqual([atom.source_site_index for atom in atoms], [0, 0, 2, 1])
        self.assertEqual([atom.expanded_index for atom in atoms], [0, 1, 3, 2])
        operations = {item["id"]: item["xyz"] for item in result.metadata["symmetry_operations"]}
        self.assertEqual({operations[value] for value in atoms[0].symmetry_operation_ids}, {"x,y,z"})
        self.assertEqual({operations[value] for value in atoms[1].symmetry_operation_ids}, {"-x,-y,-z"})
        for atom in atoms[2:]:
            self.assertEqual({operations[value] for value in atom.symmetry_operation_ids}, {"x,y,z", "-x,-y,-z"})
        for output_index, atom in enumerate(atoms):
            self.assertIn(output_index, sites[atom.source_site_index]["output_indices"])
            self.assertIn(atom.expanded_index, sites[atom.source_site_index]["expanded_indices"])

    def test_centered_cell_full_operation_set_is_not_expanded_twice_or_reduced(self):
        result = read_ordered_cif(fixture("c2_centered_conventional"))
        self.assert_vectors(result.structure.cell,
                            [[4, 0, 0], [0, 5, 0], [6 * cos(radians(105)), 0, 6 * sin(radians(105))]])
        self.assertEqual(len(result.structure.atoms), 6)
        self.assertEqual([site["multiplicity"] for site in result.metadata["source_sites"]], [4, 2])
        self.assertEqual([atom.element for atom in result.structure.atoms], ["Na"] * 4 + ["Cl"] * 2)
        self.assert_vectors([atom.fractional for atom in result.structure.atoms],
                            [[.1, .2, .3], [.4, .7, .7], [.9, .2, .7], [.6, .7, .3], [0, .1, 0], [.5, .6, 0]])
        self.assertEqual([item["xyz"] for item in result.metadata["symmetry_operations"]],
                         ["x,y,z", "-x+1/2,y+1/2,-z", "-x,y,-z", "x+1/2,y+1/2,z"])

    def test_distinct_close_sites_are_preserved(self):
        result = read_ordered_cif(fixture("p1_distinct_close_sites"))
        self.assertEqual(len(result.structure.atoms), 2)
        self.assertEqual([atom.source_site_index for atom in result.structure.atoms], [0, 1])
        self.assertAlmostEqual(result.structure.atoms[1].cartesian[0] - result.structure.atoms[0].cartesian[0], 1e-6, places=12)

    def test_block_selection_is_explicit_and_does_not_mix_cells_or_sites(self):
        data = fixture("multiple_blocks")
        self.rejected(data)
        self.rejected(data, block="absent")
        alpha, beta = read_ordered_cif(data, block="alpha"), read_ordered_cif(data, block="beta")
        self.assertEqual([atom.element for atom in alpha.structure.atoms], ["H"])
        self.assertEqual([atom.element for atom in beta.structure.atoms], ["He", "He"])
        self.assert_vectors(alpha.structure.cell, [[3, 0, 0], [0, 3, 0], [0, 0, 3]])
        self.assert_vectors(beta.structure.cell, [[4, 0, 0], [0, 4, 0], [0, 0, 4]])
        self.assertEqual(beta.metadata["selected_block"], "beta")
        self.assertEqual({item["name"] for item in beta.metadata["available_blocks"] if item["structural_candidate"]}, {"alpha", "beta"})

    def test_disorder_partial_and_mixed_sites_are_rejected_separately(self):
        for name in ("partial_occupancy", "mixed_coincident_full_occupancy", "split_disorder_explicit_flags"):
            with self.subTest(name=name):
                self.rejected(fixture(name))
        partial = fixture("partial_occupancy")
        for occupancy in (b"0.50(2)", b"5e-1", b"?", b".", b"nan"):
            with self.subTest(occupancy=occupancy):
                self.rejected(partial.replace(b".3 0.5", b".3 " + occupancy))

    def test_absent_occupancy_has_explicit_default_evidence(self):
        data = fixture("p1_triclinic_outside")
        explicit = read_ordered_cif(data)
        omitted = data.replace(b"_atom_site_occupancy\n", b"").replace(b"0.5 1\n", b"0.5\n").replace(b"0.75 1\n", b"0.75\n")
        implicit = read_ordered_cif(omitted)
        self.assertEqual(implicit.structure.structure_id, explicit.structure.structure_id)
        for actual, baseline in zip(implicit.metadata["source_sites"], explicit.metadata["source_sites"]):
            self.assertEqual(actual["occupancy"], 1)
            self.assertTrue(actual["occupancy_evidence"])
            self.assertNotEqual(actual["occupancy_evidence"], baseline["occupancy_evidence"])

    def test_space_group_expansion_requires_unambiguous_symmetry_evidence(self):
        original = fixture("pminus1_general_special_mapping")
        inferred = read_ordered_cif(without_operations(original))
        self.assertEqual(inferred.structure.structure_id, read_ordered_cif(original).structure.structure_id)
        no_evidence = without_operations(original).replace(b"_space_group_name_H-M_alt 'P -1'\n", b"").replace(b"_space_group_IT_number 2\n", b"")
        self.rejected(no_evidence)
        self.rejected(original.replace(b"_space_group_IT_number 2", b"_space_group_IT_number 1"))
        self.rejected(original.replace(b"2 '-x,-y,-z'\n", b""))
        self.rejected(original.replace(b"'-x,-y,-z'", b"'x+1/3,y,z'"))
        self.rejected(original.replace(b"'-x,-y,-z'", b"'x/0,y,z'"))

    def test_duplicate_tags_labels_and_unknown_site_identity_are_rejected(self):
        data = fixture("p1_triclinic_outside")
        self.rejected(data.replace(b"_cell_length_a 4\n", b"_cell_length_a 4\n_cell_length_a 4\n"))
        self.rejected(data.replace(b"Oinside O", b"Coutside O"))
        self.rejected(data.replace(b"Coutside C", b"Coutside Xx"))
        self.rejected(data.replace(b"Coutside C", b"Coutside ?"))
        self.rejected(data.replace(b"Coutside C", b"Coutside ."))

    def test_explicit_oxidation_type_and_missing_type_label_fallback(self):
        original = fixture("p1_triclinic_outside")
        charged = read_ordered_cif(original.replace(b"Coutside C", b"site_A Fe3+"))
        self.assertEqual(charged.structure.atoms[0].element, "Fe")
        self.assertEqual(charged.metadata["source_sites"][0]["type_symbol"], "Fe3+")
        labelled = original.replace(b"_atom_site_type_symbol\n", b"").replace(b"Coutside C", b"Si1").replace(b"Oinside O", b"O2")
        fallback = read_ordered_cif(labelled)
        self.assertEqual([atom.element for atom in fallback.structure.atoms], ["Si", "O"])
        self.rejected(labelled.replace(b"Si1 ", b"site_unknown "))

    def test_missing_invalid_cell_and_magnetic_content_are_rejected(self):
        data = fixture("p1_distinct_close_sites")
        for bad in (data.replace(b"_cell_length_a 10\n", b""),
                    data.replace(b"_cell_angle_gamma 90\n", b""),
                    data.replace(b"_cell_length_a 10", b"_cell_length_a nan"),
                    data.replace(b"_cell_angle_alpha 90", b"_cell_angle_alpha 0"),
                    data + b"_space_group_symop_magn_operation.xyz 'x,y,z,+1'\n",
                    b"data_empty\n", b"\xff"):
            with self.subTest(data=bad):
                self.rejected(bad)

    def test_near_special_position_is_rejected_instead_of_guessed(self):
        data = fixture("pminus1_general_special_mapping")
        header = data.split(b"_atom_site_occupancy\n", 1)[0] + b"_atom_site_occupancy\n"
        self.rejected(header + b"Cnear C 0.000000002 0 0 1\n")
        exact = read_ordered_cif(header + b"Cexact C 0 0 0 1\n")
        self.assertEqual(len(exact.structure.atoms), 1)
        self.assertEqual(exact.metadata["source_sites"][0]["multiplicity"], 1)
        self.assertEqual(len(exact.structure.atoms[0].symmetry_operation_ids), 2)

    def test_comments_integer_images_and_operation_declaration_order_are_metamorphic(self):
        original = fixture("pminus1_general_special_mapping")
        reference = read_ordered_cif(original)
        variants = [b"# source comment changes content identity only\n" + original,
                    original.replace(b"Cgeneral C .1 .2 .3", b"Cgeneral C 1.1 -1.8 2.3"),
                    original.replace(b"1 'x,y,z'\n2 '-x,-y,-z'", b"2 '-x,-y,-z'\n1 'x,y,z'")]
        for changed in variants:
            with self.subTest(data=changed):
                result = read_ordered_cif(changed)
                self.assertNotEqual(result.structure.source_sha256, reference.structure.source_sha256)
                self.assertEqual(result.structure.structure_id, reference.structure.structure_id)
                self.assertEqual(result.poscar, reference.poscar)
                self.assertEqual([item["xyz"] for item in result.metadata["symmetry_operations"]], ["x,y,z", "-x,-y,-z"])
                self.assertEqual([atom.expanded_index for atom in result.structure.atoms], [0, 1, 3, 2])

    def test_duplicate_operations_collapse_without_losing_declaration_provenance(self):
        original = fixture("pminus1_general_special_mapping")
        baseline = read_ordered_cif(original)
        duplicate = original.replace(b"1 'x,y,z'\n", b"1 'x,y,z'\n99 'x,y,z'\n")
        result = read_ordered_cif(duplicate)
        self.assertEqual(result.structure.structure_id, baseline.structure.structure_id)
        self.assertEqual(result.poscar, baseline.poscar)
        self.assertNotEqual(result.structure.source_sha256, baseline.structure.source_sha256)
        operations = result.metadata["symmetry_operations"]
        self.assertEqual([item["xyz"] for item in operations], ["x,y,z", "-x,-y,-z"])
        self.assertEqual([item["source_id"] for item in operations[0]["declarations"]], ["1", "99"])
        self.assertEqual([item["row_index"] for item in operations[0]["declarations"]], [0, 1])
        self.assertEqual([site["multiplicity"] for site in result.metadata["source_sites"]], [2, 1, 1])

    def test_uncertainty_tokens_preserve_nominal_geometry_and_source_evidence(self):
        original = fixture("p1_triclinic_outside")
        uncertain = original.replace(b"_cell_length_a 4\n", b"_cell_length_a 4.0(1)\n")
        uncertain = uncertain.replace(b"1.25 -0.25 0.5 1\n", b"1.25(2) -0.25(3) 0.5(1) 1.0(0)\n")
        baseline, result = read_ordered_cif(original), read_ordered_cif(uncertain)
        self.assertEqual(result.structure.structure_id, baseline.structure.structure_id)
        self.assertEqual(result.poscar, baseline.poscar)
        site = result.metadata["source_sites"][0]
        self.assertEqual(site["fractional_tokens"], ["1.25(2)", "-0.25(3)", "0.5(1)"])
        self.assertEqual(site["occupancy_evidence"]["token"], "1.0(0)")
        self.assert_vectors([site["fractional"]], [[1.25, -.25, .5]])

    def test_explicit_unknown_occupancy_never_uses_the_absent_column_default(self):
        original = fixture("p1_triclinic_outside")
        for token in (b"?", b"."):
            with self.subTest(token=token):
                self.rejected(original.replace(b"1.25 -0.25 0.5 1\n", b"1.25 -0.25 0.5 " + token + b"\n"))

    def test_space_group_only_p1_is_supported_but_unqualified_r3_is_ambiguous(self):
        original = fixture("p1_triclinic_outside")
        declared = read_ordered_cif(without_operations(original))
        self.assertEqual(declared.poscar, read_ordered_cif(original).poscar)
        self.assertEqual([item["xyz"] for item in declared.metadata["symmetry_operations"]], ["x,y,z"])
        ambiguous = without_operations(original).replace(b"'P 1'", b"'R 3'")
        ambiguous = ambiguous.replace(b"_space_group_IT_number 1\n", b"_space_group_IT_number 146\n")
        ambiguous = ambiguous.replace(b"_cell_length_b 5\n", b"_cell_length_b 4\n")
        ambiguous = ambiguous.replace(b"_cell_angle_alpha 60\n", b"_cell_angle_alpha 90\n")
        ambiguous = ambiguous.replace(b"_cell_angle_gamma 60\n", b"_cell_angle_gamma 120\n")
        self.rejected(ambiguous)

    def test_explicit_operations_require_identity_closure_and_metric_compatibility(self):
        original = fixture("p1_triclinic_outside")
        unlabelled = original.replace(b"_space_group_name_H-M_alt 'P 1'\n", b"")
        unlabelled = unlabelled.replace(b"_space_group_IT_number 1\n", b"")
        self.assertEqual(read_ordered_cif(unlabelled).poscar, read_ordered_cif(original).poscar)
        cases = {
            "missing_identity": b"1 '-x,-y,-z'\n",
            "not_closed": b"1 'x,y,z'\n2 'x+1/3,y,z'\n",
            "non_metric": b"1 'x,y,z'\n2 'y,x,z'\n",
        }
        for case, operations in cases.items():
            with self.subTest(case=case):
                self.rejected(unlabelled.replace(b"1 'x,y,z'\n", operations))

    def test_attached_hydrogens_cannot_silently_change_the_imported_composition(self):
        original = fixture("p1_triclinic_outside")
        with_column = original.replace(b"_atom_site_occupancy\n", b"_atom_site_occupancy\n_atom_site_attached_hydrogens\n")
        with_zero = with_column.replace(b"0.5 1\n", b"0.5 1 0\n").replace(b"0.75 1\n", b"0.75 1 0\n")
        self.assertEqual(read_ordered_cif(with_zero).poscar, read_ordered_cif(original).poscar)
        for token in (b"4", b"?", b".", b"-1"):
            with self.subTest(token=token):
                self.rejected(with_zero.replace(b"0.5 1 0\n", b"0.5 1 " + token + b"\n"))

    def test_calculated_site_flags_and_magnetic_moments_are_bounded(self):
        original = fixture("p1_triclinic_outside")
        with_column = original.replace(b"_atom_site_occupancy\n", b"_atom_site_occupancy\n_atom_site_calc_flag\n")
        baseline = read_ordered_cif(original)
        for flag in (b"d", b"c", b"calc", b"."):
            with self.subTest(flag=flag):
                data = with_column.replace(b"0.5 1\n", b"0.5 1 " + flag + b"\n")
                data = data.replace(b"0.75 1\n", b"0.75 1 " + flag + b"\n")
                self.assertEqual(read_ordered_cif(data).poscar, baseline.poscar)
        dummy = with_column.replace(b"0.5 1\n", b"0.5 1 dum\n").replace(b"0.75 1\n", b"0.75 1 d\n")
        self.rejected(dummy)
        self.rejected(original + b"_atom_site_moment.crystalaxis_x 1\n")


if __name__ == "__main__":
    unittest.main()
