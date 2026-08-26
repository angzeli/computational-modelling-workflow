from __future__ import annotations

import unittest

from cmw.molecular.stacking.torsion_search import (
    TorsionSearchProtocol,
    apply_torsion_angles,
    classify_interfragment_contacts,
    deterministic_torsion_search,
    identify_peripheral_rotatable_bonds,
)
from cmw.structure.xyz import Atom, XYZGeometry, format_xyz, geometry_hash


def monomer() -> XYZGeometry:
    return XYZGeometry(
        (
            Atom("C", 0.0, 0.0, 0.0),
            Atom("C", -1.4, 0.0, 0.0),
            Atom("C", -0.7, 1.2, 0.0),
            Atom("C", 1.4, 0.0, 0.0),
            Atom("H", 1.4, 1.0, 0.0),
        )
    )


class TorsionSearchTests(unittest.TestCase):
    def test_identifies_only_the_peripheral_bridge(self) -> None:
        torsions = identify_peripheral_rotatable_bonds(
            monomer(), core_indices=(0, 1, 2), fragment_id="A"
        )

        self.assertEqual(len(torsions), 1)
        self.assertEqual(torsions[0].bond, (0, 3))
        self.assertEqual(torsions[0].rotating_atoms, (3, 4))
        self.assertEqual(torsions[0].classification, "core_peripheral_aryl")

    def test_rotation_preserves_core_coordinates_exactly(self) -> None:
        geometry = monomer()
        torsion = identify_peripheral_rotatable_bonds(
            geometry, core_indices=(0, 1, 2), fragment_id="A"
        )[0]
        rotated = apply_torsion_angles(geometry, (torsion,), (120.0,))

        self.assertEqual(rotated.coordinates[:3], geometry.coordinates[:3])
        self.assertNotEqual(rotated.atoms[4].coordinates, geometry.atoms[4].coordinates)

    def test_element_aware_contact_classification(self) -> None:
        geometry = XYZGeometry(
            (
                Atom("C", 0.0, 0.0, 0.0),
                Atom("O", 5.0, 0.0, 0.0),
                Atom("C", 1.35, 0.0, 0.0),
                Atom("H", 6.7, 0.0, 0.0),
            )
        )
        records = classify_interfragment_contacts(
            geometry, fragments={"A": (0, 1), "B": (2, 3)}
        )
        by_pair = {(item.atom_a, item.atom_b): item for item in records}

        self.assertEqual(by_pair[(0, 2)].classification, "hard_clash")
        self.assertEqual(
            by_pair[(1, 3)].classification, "plausible_hydrogen_bond"
        )

    def test_search_is_deterministic_and_removes_a_hydrogen_clash(self) -> None:
        first = XYZGeometry(
            (
                Atom("C", 0.0, 0.0, 0.0),
                Atom("C", -1.4, 0.0, 0.0),
                Atom("C", -0.7, 1.2, 0.0),
                Atom("C", 1.4, 0.0, 0.0),
                Atom("C", 1.4, 1.4, 0.0),
                Atom("H", 1.4, 2.4, 0.0),
            )
        )
        second = (
            Atom("C", 0.0, 0.0, 3.4),
            Atom("C", -1.4, 0.0, 3.4),
            Atom("C", -0.7, 1.2, 3.4),
            Atom("H", 1.4, 1.7, 0.4),
        )
        geometry = XYZGeometry((*first.atoms, *second))
        torsions = identify_peripheral_rotatable_bonds(
            first, core_indices=(0, 1, 2), fragment_id="A"
        )[:1]
        protocol = TorsionSearchProtocol(
            coarse_candidate_count=24, fine_passes=1, pair_refinement_passes=0
        )
        arguments = {
            "fragments": {"A": tuple(range(6)), "B": tuple(range(6, 10))},
            "core_indices": {"A": (0, 1, 2), "B": (6, 7, 8)},
            "torsions": torsions,
            "protocol": protocol,
        }

        first_result = deterministic_torsion_search(geometry, **arguments)
        second_result = deterministic_torsion_search(geometry, **arguments)

        self.assertEqual(
            geometry_hash(first_result.geometry),
            geometry_hash(second_result.geometry),
        )
        self.assertEqual(format_xyz(first_result.geometry), format_xyz(second_result.geometry))
        self.assertFalse(
            any(
                item.classification == "hard_clash"
                for item in first_result.selected_contacts
            )
        )
        self.assertEqual(first_result.geometry.coordinates[:3], geometry.coordinates[:3])
        self.assertEqual(
            first_result.geometry.coordinates[6:9], geometry.coordinates[6:9]
        )


if __name__ == "__main__":
    unittest.main()
