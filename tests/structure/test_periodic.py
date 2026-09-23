from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import unittest

from cmw.structure.ordering import group_species, normalize_species_order
from cmw.structure.periodic import PeriodicAtom, PeriodicStructure


CELL = ((4.0, 0.0, 0.0), (1.0, 5.0, 0.0), (0.5, 0.25, 6.0))


def atom(element="H", fractional=(0.125, 0.25, 0.5), *, index=0, source=0, cell=CELL):
    cartesian = tuple(sum(fractional[row] * cell[row][column] for row in range(3))
                      for column in range(3))
    return PeriodicAtom(element, fractional, cartesian, source, index, ("identity",))


def structure(atoms=None, cell=CELL):
    return PeriodicStructure(cell, (atom(cell=cell),) if atoms is None else atoms,
                             "a" * 64, "synthetic")


class PeriodicStructureTests(unittest.TestCase):
    def test_species_grouping_is_stable_and_explicit_order_is_complete(self):
        symbols = ("C", "H", "C", "O", "H")
        self.assertEqual(group_species(symbols), (("C", "H", "O"), (0, 2, 1, 4, 3)))
        self.assertEqual(group_species(symbols, (" o ", "h", "C")),
                         (("O", "H", "C"), (3, 1, 4, 0, 2)))
        self.assertEqual(normalize_species_order(symbols), ("C", "H", "O"))
        for requested in (("C", "H"), ("C", "H", "O", "He"),
                          ("C", "H", "O", "C"), ("C", "H", "Qq"), ("",)):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                group_species(symbols, requested)

    def test_model_and_nested_state_are_immutable(self):
        value = structure()
        with self.assertRaises(FrozenInstanceError):
            value.selected_block = "changed"
        with self.assertRaises(FrozenInstanceError):
            value.atoms[0].element = "He"
        with self.assertRaises(TypeError):
            value.cell[0][0] = 20
        record = value.to_dict()
        record["cell"][0][0] = 20
        record["atoms"][0]["fractional"][0] = 0.75
        record["atoms"][0]["symmetry_operation_ids"].append("other")
        self.assertEqual(value.cell, CELL)
        self.assertEqual(value.atoms[0].fractional, (0.125, 0.25, 0.5))
        self.assertEqual(value.atoms[0].symmetry_operation_ids, ("identity",))
        self.assertEqual(value.to_dict()["structure_id"], value.structure_id)

    def test_mutable_constructor_inputs_are_rejected(self):
        original = atom()
        for field, value in (("fractional", [0.125, 0.25, 0.5]),
                             ("cartesian", list(original.cartesian)),
                             ("symmetry_operation_ids", ["identity"])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(original, **{field: value})
        for change in ({"cell": list(CELL)}, {"cell": (list(CELL[0]), *CELL[1:])},
                       {"atoms": [original]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(structure(), **change)

    def test_cell_and_coordinate_contracts_are_checked(self):
        for cell in (((1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
                     ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                     ((float("nan"), 0.0, 0.0), *CELL[1:]),
                     ((1.0, 0.0), *CELL[1:])):
            with self.subTest(cell=cell), self.assertRaises(ValueError):
                replace(structure(), cell=cell)
        invalid = replace(atom(), cartesian=(0.0, 0.0, 0.0))
        with self.assertRaisesRegex(ValueError, "Cartesian"):
            structure((invalid,))
        with self.assertRaises(ValueError):
            replace(atom(), fractional=(float("inf"), 0.0, 0.0))
        with self.assertRaises(ValueError):
            replace(atom(), fractional=(True, 0.0, 0.0))

    def test_provenance_indices_form_only_the_expanded_atom_bijection(self):
        # One asymmetric source site may generate several output atoms.
        first = atom(index=0, source=7)
        second = atom(fractional=(0.75, 0.5, 0.25), index=1, source=7)
        value = structure((second, first))
        self.assertEqual([item["expanded_index"] for item in value.to_dict()["atoms"]], [1, 0])
        for atoms in ((first, first), (replace(first, expanded_index=1),), ()):
            with self.subTest(atoms=atoms), self.assertRaises(ValueError):
                structure(atoms)
        for change in ({"source_site_index": -1}, {"expanded_index": True},
                       {"element": "Qq"}, {"symmetry_operation_ids": ()},
                       {"symmetry_operation_ids": ("identity", "identity")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(first, **change)

    def test_identity_ignores_source_metadata_and_integer_lattice_images(self):
        original = structure()
        provenance_only = replace(original, source_sha256="b" * 64, selected_block="renamed",
                                  atoms=(replace(original.atoms[0], source_site_index=12,
                                                 symmetry_operation_ids=("op-7",)),))
        translated_images = structure((atom(fractional=(1.125, -1.75, 3.5)),))
        self.assertEqual(original.structure_id, provenance_only.structure_id)
        self.assertEqual(original.structure_id, translated_images.structure_id)
        self.assertEqual(translated_images.atoms[0].fractional, (1.125, -1.75, 3.5))

    def test_identity_preserves_basis_origin_species_and_atom_order(self):
        original = structure((atom(), atom("He", (0.25, 0.5, 0.75), index=1)))
        alternatives = [replace(original, atoms=tuple(reversed(original.atoms))),
                        structure((atom("Li"), original.atoms[1])),
                        structure((atom(fractional=(0.225, 0.25, 0.5)), original.atoms[1]))]
        changed_cell = (CELL[1], CELL[2], CELL[0])
        changed_atoms = tuple(atom(item.element,
                                  (item.fractional[1], item.fractional[2], item.fractional[0]),
                                  index=item.expanded_index, cell=changed_cell)
                              for item in original.atoms)
        alternatives.append(structure(changed_atoms, changed_cell))
        for changed in alternatives:
            self.assertNotEqual(original.structure_id, changed.structure_id)

    def test_identity_precision_is_documented_decimal_rounding(self):
        original = structure()
        below_precision = structure((atom(fractional=(0.125 + 1e-13, 0.25, 0.5)),))
        above_precision = structure((atom(fractional=(0.125 + 2e-12, 0.25, 0.5)),))
        self.assertEqual(original.structure_id, below_precision.structure_id)
        self.assertNotEqual(original.structure_id, above_precision.structure_id)
        edge = structure((atom(fractional=(1.0 - 1e-13, 0.25, 0.5)),))
        zero = structure((atom(fractional=(0.0, 0.25, 0.5)),))
        self.assertEqual(edge.structure_id, zero.structure_id)

    def test_source_identity_and_block_are_required(self):
        for change in ({"source_sha256": "not-a-digest"}, {"selected_block": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(structure(), **change)


if __name__ == "__main__":
    unittest.main()
