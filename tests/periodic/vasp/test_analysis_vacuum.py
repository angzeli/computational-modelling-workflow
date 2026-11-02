from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.analysis_vacuum import DEFAULT_THRESHOLDS, read_locpot


SETTINGS = {"ISPIN": 1, "LVHAR": True, "LVTOT": False,
            "LSORBIT": False, "LNONCOLLINEAR": False, "IDIPOL": 3}
INTERVALS = {"lower": [1, 5], "upper": [15, 19]}


def locpot_text(*, axis=2, atoms=(8, 12), cell=None, dimensions=None, profiles=None,
                skew=False, point_offset=None):
    cell = cell or ([[3, 0, 0], [1 if skew else 0, 4, 0], [0, 0, 30]] if axis == 2
                    else [[30, 0, 0], [0, 3, 0], [0, 0, 4]])
    dimensions = dimensions or ((2, 3, 60) if axis == 2 else (60, 2, 3))
    profiles = profiles or [[4 if i / 2 < 8 or i / 2 > 25 else 6 for i in range(60)]]
    header = ["synthetic LOCPOT", "1", *[" ".join(map(str, row)) for row in cell],
              "H", str(len(atoms)), "Direct"]
    for position in atoms:
        row = [.5, .5, .5]
        row[axis] = position / 30
        header.append(" ".join(map(str, row)))
    for channel, profile in enumerate(profiles):
        header.extend(["", " ".join(map(str, dimensions))])
        values = []
        for z in range(dimensions[2]):
            for y in range(dimensions[1]):
                for x in range(dimensions[0]):
                    value = profile[(x, y, z)[axis]]
                    if point_offset:
                        value += point_offset(channel, x, y, z)
                    values.append(f"{value:.10f}")
        header.extend(" ".join(values[start:start + 5]) for start in range(0, len(values), 5))
    return "\n".join(header) + "\n"


class VacuumAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "LOCPOT"

    def read(self, text=None, *, settings=None, intervals=None, **options):
        self.path.write_text(text or locpot_text())
        return read_locpot(self.path, settings=SETTINGS if settings is None else settings,
                           intervals=INTERVALS if intervals is None else intervals, **options)

    def test_asymmetric_sides_remain_separate(self):
        result = self.read()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["sides"]["lower"]["value"], 4)
        self.assertEqual(result["sides"]["upper"]["value"], 6)
        self.assertEqual(result["sides"]["upper"]["slope_ev_per_angstrom"], 0)
        self.assertEqual(result["generation_binding"], "unestablished")
        self.assertEqual(result["source"]["coverage"], "complete")
        self.assertEqual(result["thresholds"], DEFAULT_THRESHOLDS)
        json.dumps(result, allow_nan=False)

    def test_x_fastest_grid_order_and_no_density_normalization(self):
        result = self.read(locpot_text(axis=0, point_offset=lambda channel, x, y, z: y * .01 + z * .02), axis=0)
        self.assertEqual(result["status"], "PASS", result)
        self.assertAlmostEqual(result["sides"]["upper"]["value"], 6.025)
        self.assertAlmostEqual(result["sides"]["lower"]["value"], 4.025)
        self.assertEqual(result["grid"], [60, 2, 3])

    def test_in_plane_skew_is_supported(self):
        self.assertEqual(self.read(locpot_text(skew=True))["status"], "PASS")

    def test_skew_normal_axis_is_unsupported(self):
        result = self.read(locpot_text(cell=[[3, 0, 0], [0, 4, 0], [1, 0, 30]]))
        self.assertEqual(result["status"], "UNSUPPORTED")

    def test_periodically_wrapped_interval(self):
        profile = [4 if i / 2 < 5 or i / 2 > 22 else 6 for i in range(60)]
        result = self.read(locpot_text(atoms=(5, 9), profiles=[profile]),
                           intervals={"lower": [28, 2], "upper": [12, 16]})
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["sides"]["lower"]["width_angstrom"], 4)
        self.assertEqual(result["sides"]["lower"]["value"], 4)

    def test_wrapped_slab(self):
        profile = [6 if i / 2 < 15 else 4 for i in range(60)]
        result = self.read(locpot_text(atoms=(29, 1), profiles=[profile]),
                           intervals={"lower": [22, 26], "upper": [4, 8]})
        self.assertEqual(result["status"], "PASS", result)

    def test_interval_crossing_vacuum_midpoint_is_ambiguous(self):
        result = self.read(locpot_text(profiles=[[6] * 60]), intervals={"upper": [22, 28]})
        self.assertEqual(result["status"], "UNKNOWN", result)
        self.assertIn("vacuum.side_ambiguous", [item["code"] for item in result["sides"]["upper"]["findings"]])

    def test_reversed_labels_do_not_pass(self):
        result = self.read(intervals={"upper": [1, 5], "lower": [15, 19]})
        self.assertEqual(result["status"], "UNKNOWN")

    def test_ambiguous_atom_projection(self):
        result = self.read(locpot_text(atoms=(0, 15), profiles=[[6] * 60]))
        self.assertEqual(result["status"], "UNKNOWN")

    def test_width_atomic_and_jump_guards(self):
        result = self.read(intervals={"upper": [15, 16]})
        self.assertEqual(result["status"], "FAIL")
        result = self.read(intervals={"upper": [13, 16]})
        self.assertEqual(result["status"], "FAIL")
        profile = [6 + (.6 if i == 39 else 0) for i in range(60)]
        result = self.read(locpot_text(profiles=[profile]), intervals={"upper": [15, 19]})
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("vacuum.discontinuity", [item["code"] for item in result["sides"]["upper"]["findings"]])

    def test_nonflat_and_sloped_plateau(self):
        profile = [6 + .02 * i / 2 for i in range(60)]
        result = self.read(locpot_text(profiles=[profile]), intervals={"upper": [15, 19]})
        self.assertEqual(result["status"], "FAIL")
        self.assertAlmostEqual(result["sides"]["upper"]["slope_ev_per_angstrom"], .02)
        self.assertAlmostEqual(result["sides"]["upper"]["variation_ev"], .08)

    def test_explicit_thresholds_recorded(self):
        profile = [6 + .02 * i / 2 for i in range(60)]
        result = self.read(locpot_text(profiles=[profile]), intervals={"upper": [15, 19]},
                           thresholds={"max_abs_slope_ev_per_angstrom": .03, "max_variation_ev": .1})
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["thresholds"]["max_variation_ev"], .1)

    def test_partial_grid_or_partial_trailing_line(self):
        text = locpot_text()
        for truncated in ("\n".join(text.splitlines()[:-1]) + "\n", text[:-3]):
            with self.subTest(truncated=truncated[-30:]):
                self.assertEqual(self.read(truncated)["status"], "FAIL")

    def test_nonfinite_grid(self):
        self.assertEqual(self.read(locpot_text().replace("6.0000000000", "NaN", 1))["status"], "FAIL")

    def test_missing_file(self):
        result = read_locpot(self.path, settings=SETTINGS, intervals=INTERVALS)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_missing_axis_or_intervals_never_opens_source(self):
        result = read_locpot(self.path, settings={}, intervals=INTERVALS)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["source"]["status"], "unread")
        result = read_locpot(self.path, settings=SETTINGS, intervals={})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["source"]["status"], "unread")

    def test_invalid_request_options(self):
        for kwargs in ({"axis": True}, {"axis": 3}, {"axis": 1.0}, {"thresholds": {"unknown": 1}},
                       {"thresholds": {"max_variation_ev": float("nan")}},
                       {"thresholds": {"min_width_angstrom": 0}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)
        with self.assertRaises(ValueError):
            self.read(intervals={"middle": [0, 2]})
        with self.assertRaises(ValueError):
            self.read(intervals={"upper": [float("inf"), 2]})
        with self.assertRaises(ValueError):
            self.read(intervals={"upper": [-1e308, 1e308]})

    def test_effective_potential_and_mode_evidence_required(self):
        for settings in ({**SETTINGS, "LVHAR": False, "LVTOT": True},
                         {**SETTINGS, "LVHAR": None}, {**SETTINGS, "LSORBIT": True},
                         {**SETTINGS, "LNONCOLLINEAR": True},
                         {"ISPIN": 1, "LVHAR": True}):
            with self.subTest(settings=settings):
                result = self.read(settings=settings, axis=2)
                self.assertEqual(result["status"], "UNSUPPORTED")
                self.assertIsNotNone(result["profile"])

    def test_identical_spin_channels_supported_without_averaging(self):
        profile = [4 if i / 2 < 8 or i / 2 > 25 else 6 for i in range(60)]
        result = self.read(locpot_text(profiles=[profile, profile]), settings={**SETTINGS, "ISPIN": 2})
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["channel_compatibility"], "identical")

    def test_planar_equivalence_cannot_hide_spin_mismatch(self):
        profile = [6] * 60
        result = self.read(locpot_text(profiles=[profile, profile],
                                       point_offset=lambda channel, x, y, z: channel * (.01 if x == 0 else -.01)),
                           settings={**SETTINGS, "ISPIN": 2})
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertEqual(result["channel_compatibility"], "different")

    def test_spin_block_count_and_grid_count_checked(self):
        self.assertEqual(self.read(settings={**SETTINGS, "ISPIN": 2})["status"], "UNSUPPORTED")
        profile = [6] * 60
        text = locpot_text(profiles=[profile, profile])
        split = text.rsplit("2 3 60", 1)
        self.assertEqual(self.read("2 3 59".join(split), settings={**SETTINGS, "ISPIN": 2})["status"], "FAIL")

    def test_full_cell_interval_is_not_a_vacuum_window(self):
        result = self.read(intervals={"upper": [0, 30]})
        self.assertEqual(result["status"], "UNKNOWN")

    def test_declared_grid_larger_than_file_is_rejected_before_allocation(self):
        text = locpot_text().replace("2 3 60", "2 3 9999999999999999999")
        self.assertEqual(self.read(text)["status"], "FAIL")

    def test_missing_coordinate_block_is_structured_failure(self):
        self.assertEqual(self.read("\n".join(locpot_text().splitlines()[:8]) + "\n")["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
