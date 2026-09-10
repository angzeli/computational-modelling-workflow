"""Independent synthetic arrays only; no calculation or licensed fixtures."""
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from cmw.analysis.electronic import aggregate_projections, prepare_dos, prepare_path, shift_energy


class ElectronicArraysTests(unittest.TestCase):
    def test_normalization_and_no_mutation(self):
        energy, density = np.array([-1., 0., 1.]), np.array([2., 4., 6.])
        for divisor in (1, 2, 3, 8):
            shifted, curves = prepare_dos(energy, {"physical": density}, reference_ev=0.3, divisor=divisor)
            np.testing.assert_allclose(shifted, [-1.3, -0.3, 0.7])
            np.testing.assert_allclose(curves["physical"], density / divisor)
            self.assertEqual(tuple(curves), ("physical",))
        np.testing.assert_array_equal(energy, [-1, 0, 1])
        np.testing.assert_array_equal(density, [2, 4, 6])
        np.testing.assert_allclose(shift_energy([[0, 1]], .3), [[-.3, .7]])

    def test_bad_dos(self):
        for energy, curves, divisor in (([0, 1], {}, 1), ([0, 0], {"a": [1, 2]}, 1),
                ([0, 1], {"a": [1]}, 1), ([0, 1], {"a": [1, np.nan]}, 1),
                ([0, 1], {"a": [-1, 2]}, 1), ([0, 1], {"a": [1, 2]}, 0)):
            with self.assertRaises(ValueError):
                prepare_dos(energy, curves, reference_ev=0, divisor=divisor)
        with self.assertRaises(ValueError):
            shift_energy([1], float("nan"))

    def test_order_independent_p_and_d_aggregation(self):
        groups = {"p": ("px", "py", "pz"), "d": ("d1", "d2", "d3", "d4", "d5")}
        channels = [{key: np.ones(3) * i for key in (*groups['p'], *groups['d'])} for i in (1, 2, 3)]
        a = aggregate_projections(["A", "B", "A"], channels, groups)
        b = aggregate_projections(["A", "A", "B"], [channels[2], channels[0], channels[1]], groups)
        for key in a:
            np.testing.assert_array_equal(a[key], b[key])
        np.testing.assert_array_equal(a["A-p"], [12] * 3)
        np.testing.assert_array_equal(a["B-d"], [10] * 3)
        with self.assertRaises(ValueError):
            aggregate_projections(["A"], [{}], groups)

    def test_nonorthogonal_path_wrapping_repeated_endpoints_and_breaks(self):
        lattice = np.array([[2., 0, 0], [-1, np.sqrt(3), 0], [0, 0, 5]])
        ends = np.array([[[0, 0, 0], [1.5, 0, 0]], [[1.5, 0, 0], [1.5, .5, 0]],
                         [[0, 0, .5], [0, 0, 0]]])
        requested = np.concatenate([np.linspace(a, b, 4) for a, b in ends])
        path = prepare_path(lattice, ends, requested % 1, 4)
        self.assertEqual(path.disconnected_before, (2,))
        self.assertEqual(path.distances[3], path.distances[4])
        self.assertEqual(path.distances[7], path.distances[8])
        self.assertAlmostEqual(path.distances[3], 1.5 * 2 * np.pi / np.sqrt(3))
        np.testing.assert_array_equal(path.intended_fractional, requested)
        with self.assertRaises(ValueError):
            prepare_path(lattice, ends, requested[:-1], 4)
        with self.assertRaises(ValueError):
            prepare_path(lattice, ends, requested + .1, 4)

    def test_import_has_no_plotting_or_file_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, "-B", "-c",
                "import pathlib,sys; import cmw.analysis.electronic; "
                "assert 'matplotlib' not in sys.modules; assert 'pymatgen' not in sys.modules; "
                "assert not list(pathlib.Path('.').iterdir())"], cwd=directory, check=True)


if __name__ == "__main__":
    unittest.main()
