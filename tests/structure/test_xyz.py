from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.structure.xyz import Atom, XYZGeometry, geometry_hash, parse_xyz, read_xyz, write_xyz


class XYZTests(unittest.TestCase):
    def test_strict_parse_and_round_trip(self) -> None:
        geometry = parse_xyz("2\nsynthetic\nh 0 0 0\nO 0 0 1.0\n")
        self.assertEqual(geometry.elements, ("H", "O"))
        self.assertEqual(geometry.coordinates[1], (0.0, 0.0, 1.0))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "round trip.xyz"
            write_xyz(path, geometry)
            repeated = read_xyz(path)
        self.assertEqual(repeated, geometry)
        self.assertEqual(geometry_hash(repeated), geometry_hash(geometry))

    def test_comment_charge_and_multiplicity_do_not_change_geometry_identity(self) -> None:
        first = parse_xyz("1\ncharge 0 multiplicity 1\nHe 0 0 0\n")
        second = parse_xyz("1\ncharge 1 multiplicity 2\nHe 0 0 0\n")
        self.assertEqual(geometry_hash(first), geometry_hash(second))

    def test_atom_order_and_coordinates_define_identity(self) -> None:
        first = parse_xyz("2\na\nH 0 0 0\nO 0 0 1\n")
        moved = parse_xyz("2\nb\nH 0 0 0\nO 0 0 2\n")
        reordered = parse_xyz("2\nc\nO 0 0 1\nH 0 0 0\n")
        self.assertNotEqual(geometry_hash(first), geometry_hash(moved))
        self.assertNotEqual(geometry_hash(first), geometry_hash(reordered))

    def test_malformed_input_fails_loudly(self) -> None:
        cases = (
            "",
            "x\ncomment\nH 0 0 0\n",
            "2\ncomment\nH 0 0 0\n",
            "1\ncomment\nH 0 0\n",
            "1\ncomment\nH 0 0 nan\n",
            "1\ncomment\nQq 0 0 0\n",
            "1\ncomment\nH 0 0 0 extra\n",
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_xyz(text)

    def test_explicit_ghost_atom_is_represented(self) -> None:
        atom = Atom("Gh(C)", 0.0, 0.0, 0.0)
        self.assertTrue(atom.ghost)
        self.assertEqual(XYZGeometry((atom,)).atom_count, 1)
