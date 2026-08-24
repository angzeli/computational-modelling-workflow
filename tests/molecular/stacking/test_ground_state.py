from __future__ import annotations

import unittest

from cmw.molecular.stacking.ground_state import (
    HARTREE_TO_KJ_MOL,
    compare_stacking_geometries,
    cp_interaction_energy,
    energy_decomposition,
    heterostack_mixing_metric,
    stacking_geometry_metrics,
)
from cmw.structure.xyz import Atom, XYZGeometry


def geometry(*, shift_b: float = 0.0, break_a: bool = False) -> XYZGeometry:
    a2 = 4.0 if break_a else 1.4
    return XYZGeometry(
        (
            Atom("C", 0.0, 0.0, 0.0),
            Atom("C", 1.4, 0.0, 0.0),
            Atom("C", 0.0, a2, 0.0),
            Atom("C", shift_b, 0.0, 3.4),
            Atom("C", 1.4 + shift_b, 0.0, 3.4),
            Atom("C", shift_b, 1.4, 3.4),
        )
    )


FRAGMENTS = {"A": (0, 1, 2), "B": (3, 4, 5)}
CORES = {"A": (0, 1, 2), "B": (3, 4, 5)}


class StackingGroundStateTests(unittest.TestCase):
    def test_geometry_metrics_and_preservation_validation(self) -> None:
        initial = geometry()
        final = geometry(shift_b=0.2)

        metrics = stacking_geometry_metrics(
            final, fragments=FRAGMENTS, core_indices=CORES
        )
        comparison = compare_stacking_geometries(
            initial,
            final,
            fragments=FRAGMENTS,
            core_indices=CORES,
            fixed_atom_indices=(0, 1, 2),
        )

        self.assertAlmostEqual(metrics.core_plane_separation_angstrom, 3.4)
        self.assertEqual(comparison.validation_status, "PASSED")
        self.assertTrue(comparison.checks["fixed_atoms_preserved"])

    def test_connectivity_change_fails_closed(self) -> None:
        comparison = compare_stacking_geometries(
            geometry(),
            geometry(break_a=True),
            fragments=FRAGMENTS,
            core_indices=CORES,
        )
        self.assertEqual(comparison.validation_status, "FAILED")
        self.assertFalse(
            comparison.checks["intrafragment_connectivity_preserved"]
        )

    def test_energy_contracts_use_explicit_definitions(self) -> None:
        interaction = cp_interaction_energy(-10.0, -4.98, -4.99)
        result = energy_decomposition(
            interaction_hartree=interaction,
            distorted_fragment_energies_hartree=(-4.97, -4.98),
            relaxed_fragment_energies_hartree=(-4.99, -4.99),
        )
        self.assertAlmostEqual(interaction, -0.03)
        self.assertAlmostEqual(result.deformation_hartree, 0.03)
        self.assertAlmostEqual(result.association_hartree, 0.0)
        self.assertAlmostEqual(
            result.interaction_kj_mol, interaction * HARTREE_TO_KJ_MOL
        )
        self.assertAlmostEqual(heterostack_mixing_metric(-5.0, -4.0, -2.0), -2.0)


if __name__ == "__main__":
    unittest.main()
