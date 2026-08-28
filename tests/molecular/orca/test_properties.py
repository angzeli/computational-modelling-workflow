from __future__ import annotations

import unittest

from cmw.molecular.orca.properties import (
    parse_ground_state_frontier_orbitals,
    parse_ground_state_properties,
)


OUTPUT = """
SCF CONVERGED AFTER 8 CYCLES
FINAL SINGLE POINT ENERGY      -100.250000000000
ORBITAL ENERGIES
----------------

  NO   OCC          E(Eh)            E(eV)
   0   2.0000      -0.700000       -19.0479
   1   2.0000      -0.250000        -6.8028
   2   0.0000      -0.050000        -1.3606
   3   0.0000       0.020000         0.5442

FRONTIER MOLECULAR ORBITAL POPULATION ANALYSIS
ANALYZING ORBITALS: HOMO=   1 LUMO=   2
-------------------------------------------------------------------------
   Atom        Q(Mulliken)     Q(Loewdin)     Q(Mulliken)     Q(Loewdin)
              <<<<<<<<<<<<HOMO>>>>>>>>>>>>   <<<<<<<<<<<<LUMO>>>>>>>>>>>>
-------------------------------------------------------------------------
   0-C         0.600000        0.550000       0.200000        0.250000
   1-H         0.400000        0.450000       0.800000        0.750000
-------------------------------------------------------------------------
LOEWDIN FRAGMENT POPULATIONS PER MO
-----------------------------------
SPIN UP
   0  -0.700000  1.0000: 80.0 20.0
   1  -0.250000  1.0000: 70.0 30.0
   2  -0.050000  0.0000: 25.0 75.0
   3   0.020000  0.0000: 10.0 90.0
HIRSHFELD ANALYSIS
------------------
  ATOM     CHARGE      SPIN
   0 C   -0.100000    0.000000
   1 H    0.100000    0.000000
  TOTAL   0.000000    0.000000
DIPOLE MOMENT
-------------
Total Dipole Moment    :      0.100000000       0.200000000       0.300000000
Magnitude (Debye)      :      0.949000000
ORCA TERMINATED NORMALLY
"""


class OrcaGroundStatePropertyTests(unittest.TestCase):
    def test_derives_result_backed_one_based_frontier_semantics(self) -> None:
        result = parse_ground_state_frontier_orbitals(
            OUTPUT, spin_mode="restricted"
        )

        self.assertEqual((result.homo.index, result.lumo.index), (2, 3))
        self.assertEqual(
            (result.homo.source_index, result.lumo.source_index), (1, 2)
        )
        self.assertEqual(result.indexing, "one_based")
        self.assertEqual(result.source_indexing, "orca_output")

    def test_frontier_parser_fails_on_partial_occupation(self) -> None:
        partial = OUTPUT.replace("1   2.0000", "1   1.5000")
        with self.assertRaisesRegex(ValueError, "partial occupation"):
            parse_ground_state_frontier_orbitals(
                partial, spin_mode="restricted"
            )

    def test_frontier_parser_requires_valid_result_and_spin_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "normal termination"):
            parse_ground_state_frontier_orbitals(
                OUTPUT.replace("ORCA TERMINATED NORMALLY", ""),
                spin_mode="restricted",
            )
        with self.assertRaisesRegex(ValueError, "alpha/beta"):
            parse_ground_state_frontier_orbitals(
                OUTPUT, spin_mode="unrestricted"
            )

    def test_extracts_energy_frontiers_dipole_charge_and_populations(self) -> None:
        result = parse_ground_state_properties(OUTPUT)

        self.assertEqual((result.homo_index, result.lumo_index), (1, 2))
        self.assertAlmostEqual(result.gap_ev, 5.4422, places=4)
        self.assertAlmostEqual(result.dipole_debye, 0.949)
        self.assertEqual(
            result.fragment_charges({"A": (0,), "B": (1,)}),
            {"A": -0.1, "B": 0.1},
        )
        populations = result.frontier_fragment_populations(
            {"A": (0,), "B": (1,)}, scheme="loewdin"
        )
        self.assertEqual(populations["HOMO"], {"A": 0.55, "B": 0.45})
        self.assertEqual(populations["LUMO"], {"A": 0.25, "B": 0.75})
        selected = result.orbital_fragment_populations(
            ("A", "B"), (0, 1, 2, 3)
        )
        self.assertEqual(selected["MO_0"], {"A": 0.8, "B": 0.2})
        self.assertEqual(selected["MO_3"], {"A": 0.1, "B": 0.9})

    def test_missing_hirshfeld_analysis_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Hirshfeld"):
            parse_ground_state_properties(OUTPUT.replace("HIRSHFELD ANALYSIS", "OMITTED"))

    def test_incomplete_fragment_partition_fails_closed(self) -> None:
        result = parse_ground_state_properties(OUTPUT)
        with self.assertRaisesRegex(ValueError, "cover every atom"):
            result.fragment_charges({"A": (0,)})

    def test_fragment_orbital_column_mismatch_fails_closed(self) -> None:
        result = parse_ground_state_properties(OUTPUT)
        with self.assertRaisesRegex(ValueError, "columns"):
            result.orbital_fragment_populations(("only_one",), (1,))


if __name__ == "__main__":
    unittest.main()
