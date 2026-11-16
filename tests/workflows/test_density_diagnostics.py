"""Advisory density diagnostics (issue #52): analytic grids and the ESP workflow."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cmw.molecular.workflows.cube import read_cube
from cmw.molecular.workflows.density_diagnostics import (
    cube_metrics, diagnose, evaluate, wavefunction_electrons,
)
from tests.workflows.test_multiwfn_cubes import ESP, ROOT, MultiwfnCubeHarness


def write_cube(path: Path, values, *, origin=(-1.0, -1.0, -1.0), spacing=0.5, dims=(5, 5, 5)) -> None:
    rows = ["density", "synthetic", f"    1 {origin[0]:.6f} {origin[1]:.6f} {origin[2]:.6f}"]
    for axis, count in enumerate(dims):
        rows.append(f"{count:5d}" + "".join(f" {spacing if i == axis else 0.0:.6f}" for i in range(3)))
    rows.append("    1 1.000000 0.000000 0.000000 0.000000")
    flat = [f"{value:.8E}" if isinstance(value, float) else str(value) for value in values]
    rows.extend(" ".join(flat[start:start + 6]) for start in range(0, len(flat), 6))
    path.write_text("\n".join(rows) + "\n")


def grid_values(function, *, origin=(-1.0, -1.0, -1.0), spacing=0.5, dims=(5, 5, 5)):
    nx, ny, nz = dims
    return [function(origin[0] + ix * spacing, origin[1] + iy * spacing, origin[2] + iz * spacing, ix, iy, iz)
            for ix in range(nx) for iy in range(ny) for iz in range(nz)]


class DensityMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cube = self.root / "density.cube"

    def test_constant_field_integral_and_boundary(self) -> None:
        write_cube(self.cube, grid_values(lambda *_: 0.002))
        metrics = cube_metrics(self.cube)
        self.assertAlmostEqual(metrics["box_volume_bohr3"], 8.0)
        self.assertAlmostEqual(metrics["trapezoidal_integral"], 0.016, places=12)
        self.assertTrue(metrics["isosurface_reaches_boundary"])
        self.assertEqual(metrics["isosurface_boundary_faces"], ["x+", "x-", "y+", "y-", "z+", "z-"])
        self.assertAlmostEqual(metrics["min_density"], 0.002)

    def test_gaussian_integral_and_contained_isosurface(self) -> None:
        exponent, spacing, dims, origin = 1.5, 0.2, (61, 61, 61), (-6.0, -6.0, -6.0)
        values = grid_values(lambda x, y, z, *_: math.exp(-exponent * (x * x + y * y + z * z)),
                             origin=origin, spacing=spacing, dims=dims)
        write_cube(self.cube, values, origin=origin, spacing=spacing, dims=dims)
        metrics = cube_metrics(self.cube)
        self.assertAlmostEqual(metrics["trapezoidal_integral"], (math.pi / exponent) ** 1.5, places=6)
        self.assertFalse(metrics["isosurface_reaches_boundary"])
        self.assertLess(metrics["max_boundary_density"], 1e-12)
        self.assertAlmostEqual(metrics["max_density"], 1.0)

    def test_outer_two_layers_identify_the_face(self) -> None:
        def field(x, y, z, ix, iy, iz):
            return 0.05 if ix == 3 else 1e-6  # second layer from the x+ face
        write_cube(self.cube, grid_values(field))
        metrics = cube_metrics(self.cube)
        self.assertEqual(metrics["max_boundary_face"], "x+")
        self.assertAlmostEqual(metrics["max_boundary_density"], 0.05)
        self.assertEqual(metrics["outer_two_layer_max_by_face"]["x-"], 1e-6)
        self.assertFalse("x+" in metrics["isosurface_boundary_faces"])  # outermost layer stays low

    def test_malformed_truncated_and_nonfinite_cubes_fail_hard(self) -> None:
        cases = {"truncated": grid_values(lambda *_: 0.1)[:-1],
                 "nonfinite": ["nan"] + grid_values(lambda *_: 0.1)[1:],
                 "text": ["density"] + grid_values(lambda *_: 0.1)[1:]}
        messages = {"truncated": "count mismatch", "nonfinite": "non-finite", "text": "non-numeric"}
        for label, values in cases.items():
            with self.subTest(label=label):
                write_cube(self.cube, values)
                with self.assertRaisesRegex(ValueError, messages[label]):
                    cube_metrics(self.cube)
        self.cube.write_text("density\nsynthetic\n")
        with self.assertRaisesRegex(ValueError, "truncated cube header"):
            cube_metrics(self.cube)

    def test_streaming_reader_keeps_existing_metadata(self) -> None:
        write_cube(self.cube, grid_values(lambda *_: 0.1))
        cube = read_cube(self.cube)
        self.assertEqual((cube.dimensions, cube.value_count, cube.header_unit), ((5, 5, 5), 125, "bohr"))


class DensityEvaluationTests(unittest.TestCase):
    metrics = {"trapezoidal_integral": 9.98, "max_boundary_density": 2e-4, "max_boundary_face": "z+"}

    def test_advisory_by_default_and_explicit_gates(self) -> None:
        advisory = evaluate(self.metrics, 10.0)
        self.assertEqual((advisory["status"], advisory["gates"]), ("ADVISORY", {}))
        self.assertAlmostEqual(advisory["electron_discrepancy"], -0.02)
        self.assertEqual(evaluate(self.metrics, 10.0, max_boundary_density=1e-3,
                                  max_electron_discrepancy=0.05)["status"], "PASS")
        failed = evaluate(self.metrics, 10.0, max_boundary_density=1e-4, max_electron_discrepancy=0.01)
        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(len(failed["reasons"]), 2)

    def test_requested_gate_without_electron_evidence_fails_closed(self) -> None:
        self.assertEqual(evaluate(self.metrics, None)["status"], "ADVISORY")
        missing = evaluate(self.metrics, None, max_electron_discrepancy=0.1)
        self.assertEqual(missing["status"], "FAIL")
        self.assertIn("missing", missing["reasons"][0])

    def test_wavefunction_electron_count_from_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "multiwfn.log"
            log.write_text(" Total/Alpha/Beta electrons:     10.0000      5.0000      5.0000\n")
            self.assertEqual(wavefunction_electrons(log), 10.0)
            log.write_text("no electron line\n")
            self.assertIsNone(wavefunction_electrons(log))
            self.assertIsNone(wavefunction_electrons(None))

    def test_diagnosis_is_bound_to_the_recorded_cube_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cube = Path(directory) / "density.cube"
            write_cube(cube, grid_values(lambda *_: 0.001))
            digest = hashlib.sha256(cube.read_bytes()).hexdigest()
            self.assertEqual(diagnose(cube, expected_sha256=digest, log_path=None)["status"], "ADVISORY")
            with self.assertRaisesRegex(ValueError, "differs from the recorded result"):
                diagnose(cube, expected_sha256="0" * 64, log_path=None)


class DensityDiagnosticsWorkflowTests(MultiwfnCubeHarness):
    def test_workflow_writes_advisory_file_beside_unchanged_result(self) -> None:
        env = {**self.env, "FAKE_MULTIWFN_ELECTRONS": "10.0"}
        completed = self.run_workflow(ESP, env=env)
        result = json.loads(completed.stdout)
        self.assertIn("[DIAGNOSTICS] ADVISORY", completed.stderr)
        result_path = next(self.output.rglob("result.json"))
        stored = result_path.read_text()
        diagnostics = json.loads((result_path.parent / "density-diagnostics.json").read_text())
        self.assertEqual(diagnostics["status"], "ADVISORY")
        self.assertEqual(diagnostics["wavefunction_electrons"], 10.0)
        self.assertEqual(diagnostics["result"]["target_id"], result["target"]["target_id"])
        self.assertNotIn("diagnostics", result)
        # Re-evaluating with a policy reuses the result and never relaunches Multiwfn.
        gated = self.run_workflow(ESP, env=env, extra=("--max-boundary-density", "1e-9"), check=False)
        self.assertEqual(gated.returncode, 3, gated.stderr)
        self.assertIn("exceeds", gated.stderr)
        self.assertEqual(self.log.read_text().splitlines().count("ESP"), 1)
        self.assertEqual(result_path.read_text(), stored)
        direct = subprocess.run(
            [sys.executable, "-m", "cmw.molecular.workflows.downstream_cli", "diagnose",
             "--result", str(result_path), "--max-electron-discrepancy", "1000"],
            cwd=ROOT, env=self.env, capture_output=True, text=True)
        self.assertEqual(direct.returncode, 0, direct.stdout)
        self.assertEqual(json.loads(direct.stdout)["status"], "PASS")

    def test_missing_electron_evidence_fails_a_requested_gate(self) -> None:
        completed = self.run_workflow(ESP, extra=("--max-electron-discrepancy", "0.5"), check=False)
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertIn("electron-count evidence is missing", completed.stderr)


if __name__ == "__main__":
    unittest.main()
