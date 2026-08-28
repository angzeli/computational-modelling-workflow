from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ase.io import read as ase_read

from cmw.structure.conversion import inspect_xyz_to_poscar, xyz_to_poscar


class XYZToPoscarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_xyz(self, name: str, rows: list[str]) -> Path:
        path = self.root / name
        path.write_text(
            f"{len(rows)}\nsynthetic molecule\n" + "\n".join(rows) + "\n",
            encoding="utf-8",
        )
        return path

    def convert(
        self, rows: list[str], **options: object
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        source = self.write_xyz("molecule.xyz", rows)
        output = self.root / "POSCAR"
        metadata = xyz_to_poscar(source, output, **options)
        mapping = json.loads(
            (self.root / "atom_mapping.json").read_text(encoding="utf-8")
        )
        return output, metadata, mapping

    def test_water_uses_orthorhombic_cell_and_ten_angstrom_per_face(self) -> None:
        output, metadata, _ = self.convert(
            ["O 0 0 0", "H 0.96 0 0", "H -0.24 0.93 0"]
        )
        atoms = ase_read(output, format="vasp")

        self.assertEqual(len(atoms), 3)
        self.assertEqual(metadata["composition"], {"O": 1, "H": 2})
        self.assertEqual(metadata["species_order"], ["O", "H"])
        self.assertEqual(metadata["net_charge"], 0)
        self.assertEqual(metadata["requested_cell_mode"], "auto")
        self.assertEqual(metadata["resolved_cell_mode"], "orthorhombic")
        self.assertEqual(metadata["cell_selection_reason"], "neutral molecule")
        self.assertEqual(metadata["warnings"], [])
        self.assertTrue(output.read_text(encoding="utf-8").splitlines()[7].startswith("Cartesian"))
        self.assertEqual(
            tuple(round(value, 8) for value in atoms.cell.lengths()),
            (21.2, 20.93, 20.0),
        )
        for clearance in metadata["actual_vacuum_clearances_angstrom"].values():
            self.assertGreaterEqual(clearance, 10.0 - 1.0e-8)

    def test_non_contiguous_species_are_grouped_with_correct_mapping(self) -> None:
        output, metadata, mapping = self.convert(
            [
                "C 0 0 0",
                "H 1 0 0",
                "C 2 0 0",
                "O 3 0 0",
                "H 4 0 0",
                "Cu 5 0 0",
            ]
        )
        atoms = ase_read(output, format="vasp")
        poscar_lines = output.read_text(encoding="utf-8").splitlines()

        self.assertEqual(atoms.get_chemical_symbols(), ["C", "C", "H", "H", "O", "Cu"])
        self.assertEqual(poscar_lines[5].split(), ["C", "H", "O", "Cu"])
        self.assertEqual(poscar_lines[6].split(), ["2", "2", "1", "1"])
        self.assertEqual(metadata["species_order"], ["C", "H", "O", "Cu"])
        self.assertTrue(metadata["atom_reordering_performed"])
        self.assertEqual(
            mapping["original_to_poscar"],
            {"0": 0, "1": 2, "2": 1, "3": 4, "4": 3, "5": 5},
        )
        self.assertEqual(
            mapping["poscar_to_original"],
            {"0": 0, "1": 2, "2": 1, "3": 4, "4": 3, "5": 5},
        )
        self.assertEqual(mapping["atoms"][1]["original_xyz_one_based_atom_number"], 3)
        self.assertEqual(mapping["atoms"][1]["poscar_one_based_atom_number"], 2)

    def test_cu_structure_round_trips_without_atom_loss(self) -> None:
        output, metadata, _ = self.convert(
            ["Cu 0 0 0", "N 2 0 0", "N -2 0 0", "H 0 1 0"]
        )
        atoms = ase_read(output, format="vasp")

        self.assertEqual(len(atoms), 4)
        self.assertEqual(atoms.get_chemical_symbols(), ["Cu", "N", "N", "H"])
        self.assertEqual(metadata["validation"]["status"], "PASSED")
        self.assertTrue(all(atoms.get_pbc()))

    def test_cubic_cell_uses_largest_span_and_custom_vacuum(self) -> None:
        output, metadata, _ = self.convert(
            ["C 0 0 0", "H 4 1 2"], vacuum=12.0, cell_mode="cubic"
        )
        atoms = ase_read(output, format="vasp")

        self.assertEqual(tuple(atoms.cell.lengths()), (28.0, 28.0, 28.0))
        self.assertEqual(metadata["vacuum_requested_angstrom"], 12.0)
        self.assertEqual(metadata["requested_cell_mode"], "cubic")
        self.assertEqual(metadata["resolved_cell_mode"], "cubic")
        self.assertEqual(metadata["cell_selection_reason"], "explicit cubic selection")
        clearances = metadata["actual_vacuum_clearances_angstrom"]
        self.assertAlmostEqual(clearances["x_minus_vacuum"], 12.0)
        self.assertAlmostEqual(clearances["x_plus_vacuum"], 12.0)
        self.assertGreater(clearances["y_minus_vacuum"], 12.0)

    def test_nonzero_charge_auto_selects_cubic_cell(self) -> None:
        source = self.write_xyz("charged.xyz", ["C 0 0 0", "H 4 1 2"])

        for net_charge in (1, 2, -1):
            with self.subTest(net_charge=net_charge):
                plan = inspect_xyz_to_poscar(source, net_charge=net_charge)
                self.assertEqual(plan["net_charge"], net_charge)
                self.assertEqual(plan["requested_cell_mode"], "auto")
                self.assertEqual(plan["resolved_cell_mode"], "cubic")
                self.assertEqual(plan["cell_mode"], "cubic")
                self.assertEqual(plan["cell_selection_reason"], "nonzero net charge")
                self.assertEqual(
                    plan["proposed_cell_dimensions_angstrom"], [24.0, 24.0, 24.0]
                )

    def test_charged_orthorhombic_override_is_allowed_but_warns(self) -> None:
        rows = ["C 0 0 0", "H 4 1 2"]
        output, metadata, _ = self.convert(
            rows, net_charge=2, cell_mode="orthorhombic"
        )
        dry_run = inspect_xyz_to_poscar(
            self.root / "molecule.xyz", net_charge=2, cell_mode="orthorhombic"
        )
        stored = json.loads(
            (self.root / "conversion_metadata.json").read_text(encoding="utf-8")
        )

        self.assertEqual(tuple(ase_read(output, format="vasp").cell.lengths()), (24.0, 21.0, 22.0))
        self.assertEqual(metadata["requested_cell_mode"], "orthorhombic")
        self.assertEqual(metadata["resolved_cell_mode"], "orthorhombic")
        self.assertIn("explicit orthorhombic override", metadata["cell_selection_reason"])
        self.assertEqual(len(metadata["warnings"]), 1)
        self.assertIn("Charged structure (net charge +2)", metadata["warnings"][0])
        self.assertIn("Cubic is the recommended CMW default", metadata["warnings"][0])
        self.assertEqual(dry_run["warnings"], metadata["warnings"])
        self.assertEqual(stored["warnings"], metadata["warnings"])

    def test_explicit_orthorhombic_neutral_behavior_is_unchanged(self) -> None:
        source = self.write_xyz("neutral-explicit.xyz", ["C 0 0 0", "H 4 1 2"])
        plan = inspect_xyz_to_poscar(
            source, net_charge=0, cell_mode="orthorhombic", vacuum=12.0
        )

        self.assertEqual(plan["requested_cell_mode"], "orthorhombic")
        self.assertEqual(plan["resolved_cell_mode"], "orthorhombic")
        self.assertEqual(plan["proposed_cell_dimensions_angstrom"], [28.0, 25.0, 26.0])
        self.assertEqual(plan["warnings"], [])

    def test_explicit_species_order_is_complete_and_controls_groups(self) -> None:
        output, metadata, mapping = self.convert(
            ["H 0 0 0", "O 0 0 1", "H 0 1 0"], species_order=["O", "H"]
        )

        self.assertEqual(ase_read(output, format="vasp").get_chemical_symbols(), ["O", "H", "H"])
        self.assertEqual(metadata["species_order"], ["O", "H"])
        self.assertEqual(mapping["poscar_to_original"], {"0": 1, "1": 0, "2": 2})

    def test_invalid_species_orders_fail_closed(self) -> None:
        source = self.write_xyz("order.xyz", ["C 0 0 0", "H 1 0 0"])
        cases = (["C"], ["C", "H", "Cu"], ["C", "H", "C"], ["Qq", "H"])
        for species_order in cases:
            with self.subTest(species_order=species_order), self.assertRaises(ValueError):
                inspect_xyz_to_poscar(source, species_order=species_order)

    def test_invalid_conversion_inputs_fail_closed(self) -> None:
        source = self.write_xyz("invalid-options.xyz", ["He 0 0 0"])
        cases = (
            {"vacuum": 0.0},
            {"vacuum": float("nan")},
            {"cell_mode": "triclinic"},
            {"net_charge": True},
            {"net_charge": 1.5},
        )
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                inspect_xyz_to_poscar(source, **options)
        with self.assertRaises(FileNotFoundError):
            inspect_xyz_to_poscar(self.root / "missing.xyz")

    def test_repeated_conversion_is_byte_deterministic(self) -> None:
        source = self.write_xyz("repeat.xyz", ["H 0 0 0", "O 0 0 1"])
        output = self.root / "POSCAR"
        xyz_to_poscar(source, output, net_charge=2)
        first = {
            path.name: path.read_bytes()
            for path in (
                output,
                self.root / "atom_mapping.json",
                self.root / "conversion_metadata.json",
            )
        }

        xyz_to_poscar(source, output, net_charge=2)
        second = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
            if path.name in first
        }
        self.assertEqual(first, second)

    def test_dry_run_reports_plan_without_creating_outputs(self) -> None:
        source = self.write_xyz("preview.xyz", ["O 0 0 0", "H 0 0 1"])
        plan = inspect_xyz_to_poscar(source, vacuum=11.0, cell_mode="cubic")

        self.assertEqual(plan["composition"], {"O": 1, "H": 1})
        self.assertEqual(plan["proposed_cell_dimensions_angstrom"], [23.0, 23.0, 23.0])
        self.assertFalse((self.root / "POSCAR").exists())
        self.assertFalse((self.root / "atom_mapping.json").exists())

    def test_cli_dry_run_reports_charge_aware_resolution(self) -> None:
        source = self.write_xyz("charged-preview.xyz", ["C 0 0 0", "H 4 1 2"])
        repository = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHON_BIN"] = str(repository / ".venv/bin/python")
        completed = subprocess.run(
            (
                str(repository / "scripts/workflows/xyz_to_poscar.sh"),
                str(source),
                "--dry-run",
                "--vacuum",
                "12",
                "--net-charge",
                "-1",
            ),
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        report = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(report["net_charge"], -1)
        self.assertEqual(report["requested_cell_mode"], "auto")
        self.assertEqual(report["resolved_cell_mode"], "cubic")
        self.assertEqual(report["cell_selection_reason"], "nonzero net charge")
        self.assertEqual(report["vacuum_requested_angstrom"], 12.0)
        self.assertFalse((self.root / "POSCAR").exists())

    def test_shell_command_runs_real_conversion(self) -> None:
        source = self.write_xyz("cli.xyz", ["He 0 0 0"])
        output = self.root / "CLI_POSCAR"
        repository = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHON_BIN"] = str(repository / ".venv/bin/python")
        completed = subprocess.run(
            (
                str(repository / "scripts/workflows/xyz_to_poscar.sh"),
                str(source),
                "--output",
                str(output),
                "--vacuum",
                "10",
                "--net-charge",
                "1",
            ),
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(output.is_file())
        report = json.loads(completed.stdout)
        self.assertEqual(report["atom_count"], 1)
        self.assertEqual(report["net_charge"], 1)
        self.assertEqual(report["resolved_cell_mode"], "cubic")


if __name__ == "__main__":
    unittest.main()
