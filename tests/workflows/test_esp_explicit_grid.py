"""Explicit common ESP grids (issue #52) through the public workflow and units."""
from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import unittest

from cmw.molecular.multiwfn.automation import Operation, menu_stream
from cmw.molecular.workflows.cube import read_cube, validate_cube_grid
from cmw.molecular.workflows.esp_cubes import EspConfiguration, estimated_cube_bytes
from tests.workflows.test_multiwfn_cubes import ESP, MultiwfnCubeHarness

# target_id values produced by main before explicit grids existed (b6ba043 era).
PINNED_SPACING_ONLY = {
    "0.2": "a1dfe23f9351f8c3a9f3a6ba6eabf662ad8409cf227e3cde4703b54f8b259ab6",
    "0.3": "a40a8040bced9217447116295af5e217b4a4cf2c37ba2c62ec82485090608bb4",
}
GRID = ("--grid-origin-bohr", "-4", "-4", "-4", "--grid-dimensions", "17", "17", "17")


class ExplicitGridWorkflowTests(MultiwfnCubeHarness):
    def setUp(self) -> None:
        super().setUp()
        self.verified = {**self.env, "FAKE_MULTIWFN_VERSION": "2026.7.15"}

    def plan(self, *extra: str, spacing: float = 0.5, check: bool = True):
        completed = self.run_workflow(ESP, spacing=spacing, extra=("--plan", *extra), check=check)
        return completed, (json.loads(completed.stdout) if completed.stdout.strip().startswith("{") else None)

    def test_spacing_only_target_identity_is_unchanged(self) -> None:
        for spacing, pinned in PINNED_SPACING_ONLY.items():
            with self.subTest(spacing=spacing):
                _, plan = self.plan(spacing=float(spacing))
                self.assertEqual(plan["target_id"], pinned)
                self.assertNotIn("grid_origin_bohr", plan["configuration"])
                self.assertNotIn("explicit_grid", plan)

    def test_plan_reports_margins_size_and_stable_identity(self) -> None:
        _, plan = self.plan(*GRID)
        self.assertEqual(plan["configuration"]["grid_origin_bohr"], [-4.0, -4.0, -4.0])
        self.assertEqual(plan["configuration"]["grid_dimensions"], [17, 17, 17])
        report = plan["explicit_grid"]
        self.assertEqual(report["end_bohr"], [4.0, 4.0, 4.0])
        self.assertEqual(report["point_count"], 17 ** 3)
        hydrogen_x = 0.75 / 0.529177210903
        self.assertAlmostEqual(report["face_margins_bohr"]["x+"], 4.0 - hydrogen_x, places=9)
        self.assertAlmostEqual(report["face_margins_bohr"]["x-"], 4.0 - hydrogen_x, places=9)
        self.assertEqual(report["estimated_cube_pair_bytes"], 2 * estimated_cube_bytes((17, 17, 17), 3))
        self.assertNotEqual(plan["target_id"], self.plan()[1]["target_id"])
        self.assertEqual(plan["target_id"], self.plan(*GRID)[1]["target_id"])
        self.assertFalse(self.output.exists())

    def test_nuclei_outside_box_and_invalid_grids_are_refused(self) -> None:
        cases = {
            "outside": ("--grid-origin-bohr", "0", "-4", "-4", "--grid-dimensions", "17", "17", "17"),
            "one point": ("--grid-origin-bohr", "-4", "-4", "-4", "--grid-dimensions", "1", "17", "17"),
            "origin only": ("--grid-origin-bohr", "-4", "-4", "-4"),
            "nonfinite": ("--grid-origin-bohr", "nan", "-4", "-4", "--grid-dimensions", "17", "17", "17"),
        }
        messages = {"outside": "outside the box at faces: x-", "one point": "integers >= 2",
                    "origin only": "must be given together", "nonfinite": "three finite values"}
        for label, extra in cases.items():
            with self.subTest(label=label):
                completed, _ = self.plan(*extra, check=False)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(messages[label], completed.stdout)
        self.assertFalse(self.output.exists())

    def test_explicit_grid_runs_and_cubes_match_the_request(self) -> None:
        completed = self.run_workflow(ESP, spacing=0.5, extra=GRID, env=self.verified)
        result = json.loads(completed.stdout)
        self.assertEqual(result["target"]["calculation"]["grid_dimensions"], [17, 17, 17])
        cubes = {role: Path(path) for role, path in result["scientific_artifact"]["files"].items()
                 if role in {"density_cube", "esp_cube"}}
        self.assertEqual(set(cubes), {"density_cube", "esp_cube"})
        for path in cubes.values():
            cube = read_cube(path)
            self.assertEqual(cube.dimensions, (17, 17, 17))
            self.assertEqual(cube.origin_header, (-4.0, -4.0, -4.0))
        menu = next(self.output.rglob("menu.in")).read_text()
        self.assertEqual(menu.count("5\n-4.0,-4.0,-4.0\n0.5,0.5,0.5\n17,17,17\n"), 2)
        # Same request reuses without a second launch.
        self.run_workflow(ESP, spacing=0.5, extra=GRID, env=self.verified)
        self.assertEqual(self.log.read_text().splitlines().count("ESP"), 1)

    def test_unqualified_version_and_mismatching_cubes_fail_closed(self) -> None:
        unqualified = self.run_workflow(ESP, spacing=0.5, extra=GRID, check=False)
        self.assertNotEqual(unqualified.returncode, 0)
        self.assertEqual(self.results("esp"), [])
        shifted = self.run_workflow(ESP, spacing=0.5, extra=GRID, check=False,
                                    env={**self.verified, "FAKE_MULTIWFN_GRID_OFFSET": "0.01"})
        self.assertNotEqual(shifted.returncode, 0)
        self.assertIn("origin differs from the requested origin", shifted.stdout)
        self.assertEqual(self.results("esp"), [])

    def test_fmo_rejects_explicit_grid(self) -> None:
        from tests.workflows.test_multiwfn_cubes import FMO
        completed = self.run_workflow(FMO, extra=("--plan", *GRID), check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("only for ESP", completed.stdout)


class ExplicitGridUnitTests(unittest.TestCase):
    def test_configuration_validation(self) -> None:
        grid = EspConfiguration(0.25, (-3, -3, -3), (25, 25, 31))
        self.assertEqual(grid.end_bohr(), (3.0, 3.0, 4.5))
        self.assertEqual(EspConfiguration(0.25).grid_dict(), {})
        for origin, dimensions in (((0, 0), (2, 2, 2)), ((0, 0, math.inf), (2, 2, 2)),
                                   ((0, 0, 0), (2, 2, 2.0)), ((0, 0, 0), None), (None, (2, 2, 2))):
            with self.subTest(origin=origin, dimensions=dimensions), self.assertRaises(ValueError):
                EspConfiguration(0.25, origin, dimensions)

    def test_menu_matches_the_sandbox_confirmed_stream(self) -> None:
        stream = menu_stream(Operation.ESP, "2026.7.15", {
            "grid_spacing_bohr": 0.25, "grid_origin_bohr": [-3, -3, -3], "grid_dimensions": [25, 25, 31]})
        grid = "5\n-3.0,-3.0,-3.0\n0.25,0.25,0.25\n25,25,31"
        self.assertEqual(stream, f"5\n1\n{grid}\n2\n0\n5\n12\n{grid}\n2\n0\nq\n")
        legacy = menu_stream(Operation.ESP, "3.8", {"grid_spacing_bohr": 0.25})
        self.assertEqual(legacy, "5\n1\n4\n0.25\n2\n0\n5\n12\n4\n0.25\n2\n0\nq\n")
        with self.assertRaisesRegex(ValueError, "not natively qualified"):
            menu_stream(Operation.ESP, "3.8", {"grid_spacing_bohr": 0.25,
                                               "grid_origin_bohr": [0, 0, 0], "grid_dimensions": [2, 2, 2]})

    def test_cube_grid_check_uses_printed_bohr_precision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "density.cub"

            def write(origin: str, vector: str = "0.250000", count: int = 2, sign: int = 1) -> None:
                rows = ["t", "t", f"    1 {origin} -3.000000 -3.000000",
                        f"{sign * count:5d} {vector} 0.000000 0.000000",
                        f"{sign * 2:5d} 0.000000 0.250000 0.000000",
                        f"{sign * 2:5d} 0.000000 0.000000 0.250000",
                        "    1 1.000000 0.000000 0.000000 0.000000",
                        " ".join(["0.1"] * (count * 4))]
                path.write_text("\n".join(rows) + "\n")

            request = dict(origin_bohr=(-3.0, -3.0, -3.0), spacing_bohr=0.25, dimensions=(2, 2, 2))
            write("-3.000000")
            validate_cube_grid(read_cube(path), **request)
            write("-3.0000004")
            validate_cube_grid(read_cube(path), **request)
            for label, args, message in (("origin", ("-3.000010",), "origin differs"),
                                         ("spacing", ("-3.000000", "0.250010"), "vectors differ"),
                                         ("dimensions", ("-3.000000", "0.250000", 3), "dimensions"),
                                         ("angstrom", ("-3.000000", "0.250000", 2, -1), "not expressed in bohr")):
                with self.subTest(label=label):
                    write(*args)
                    with self.assertRaisesRegex(ValueError, message):
                        validate_cube_grid(read_cube(path), **request)

    def test_size_estimate_tracks_real_multiwfn_output(self) -> None:
        # Measured: Multiwfn 2026.7.15 wrote 275333 bytes for 25x25x31 with two atoms.
        self.assertLess(abs(estimated_cube_bytes((25, 25, 31), 2) - 275333) / 275333, 0.01)


if __name__ == "__main__":
    unittest.main()
