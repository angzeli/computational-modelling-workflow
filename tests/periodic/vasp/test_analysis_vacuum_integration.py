"""Synthetic native/LOCPOT compatibility and independent surface regressions."""
from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET

from cmw.cli import main
from cmw.periodic.vasp.analysis_context import AnalysisContext
from cmw.periodic.vasp.electronic_levels import (get_vacuum_level, get_work_function,
    get_ionization_energy, get_electron_affinity)
from .result_fixtures import native_texts
from .test_analysis_api import electronic_run
from .test_analysis_vacuum import INTERVALS, locpot_text


class VacuumIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name).resolve()

    def write(self, *, settings=None, warning="", native_vacuum=True, shift=0, potential_shift=None):
        values = {"LVHAR": "T", "LVTOT": "F", "LVACPOTAV": "T", "IDIPOL": "3", "EFERMI": "LEGACY"}
        values.update(settings or {})
        texts = native_texts(cell=((3, 0, 0), (0, 4, 0), (0, 0, 30)),
                             positions=((1.5, 2, 8), (1.5, 2, 12)), settings=values)
        extra = f" E-fermi : {2 + shift:.10f}\n" + warning
        if native_vacuum:
            extra += f" vacuum level on the upper side and lower side of the slab {6 + shift:.10f} {4 + shift:.10f}\n"
        texts["OUTCAR"] = texts["OUTCAR"].replace(" General timing", extra + " General timing")
        for role in ("OUTCAR", "OSZICAR"):
            (self.run / role).write_text(texts[role])
        offset = shift if potential_shift is None else potential_shift
        profile = [(4 if i / 2 < 8 or i / 2 > 25 else 6) + offset for i in range(60)]
        (self.run / "LOCPOT").write_text(locpot_text(profiles=[profile]).replace("H\n2\n", "H He\n1 1\n"))

    def vacuum(self, **options):
        options.setdefault("vacuum_source", "crosscheck")
        options.setdefault("intervals", INTERVALS)
        return get_vacuum_level(self.run, **options)

    def work(self, **options):
        options.setdefault("vacuum_source", "crosscheck")
        options.setdefault("intervals", INTERVALS)
        return get_work_function(self.run, **options)

    def test_native_and_locpot_values_are_bound_per_side(self):
        self.write()
        result = self.vacuum()
        self.assertEqual(result["assessment"], "PASS", result)
        self.assertEqual(result["value"], {"lower": 4, "upper": 6})
        self.assertEqual(result["details"]["generation_binding"], {
            "upper": "unestablished", "lower": "unestablished"})
        self.assertEqual(result["surfaces"]["upper"]["native_corroboration"]["assessment"], "PASS")
        self.assertEqual(result["surfaces"]["upper"]["source_consistency"], "CONSISTENT_UNAUTHENTICATED")
        self.assertAlmostEqual(result["details"]["sides"]["upper"]["half_last_place_ev"], 5e-11)
        self.assertLess(result["details"]["sides"]["upper"]["native_correspondence"]["tolerance_ev"], .00002)
        self.assertEqual(self.work()["value"], {"lower": 2, "upper": 4})
        strict = self.vacuum(vacuum_source="locpot", locpot_policy="strict")
        self.assertEqual(strict["value"], result["value"])
        self.assertEqual(strict["surfaces"]["upper"]["source_consistency"], "CONSISTENT_UNAUTHENTICATED")

    def test_native_xc_semantics_not_mislabeled_or_derived(self):
        self.write(settings={"LVHAR": "F", "LVTOT": "T"})
        native = self.vacuum(vacuum_source="native")
        self.assertEqual(native["assessment"], "UNSUPPORTED", native)
        self.assertEqual(native["details"]["potential_semantics"], "xc_containing")
        self.assertIsNone(native["value"])
        self.assertIsNone(self.work(vacuum_source="native", diagnostic=True)["value"])
        self.assertIsNone(self.vacuum()["surfaces"]["upper"]["value"])

    def test_multiline_missing_vacuum_warning_retains_side(self):
        warning = (" | Did not find any points to average over |\n"
                   " | for the lower side of this synthetic fixture. |\n"
                   " | Please inspect that surface independently. |\n")
        self.write(warning=warning)
        result = self.vacuum(vacuum_source="native")
        self.assertEqual(result["assessment"], "UNKNOWN", result)
        self.assertEqual(result["surfaces"]["upper"], {"value": 6, "assessment": "PASS"})
        self.assertIsNone(result["surfaces"]["lower"]["value"])
        warning = result["details"]["sides"]["lower"]["findings"][0]
        self.assertIn("lower side", warning["message"])
        self.assertGreater(warning["reference"]["line_end"], warning["reference"]["line_start"])

    def test_multiline_charge_density_warning_is_not_silently_ignored(self):
        self.write(warning=(" | The minimum charge density times volume of the cell |\n"
                            " | is too large in this synthetic fixture. |\n"))
        result = self.vacuum(vacuum_source="native")
        self.assertEqual(result["assessment"], "UNKNOWN", result)
        self.assertTrue(all(side["value"] is None for side in result["surfaces"].values()))

    def test_healthy_surface_survives_other_native_warning(self):
        self.write(warning=" WARNING: no field-free vacuum region on lower side\n")
        result = self.vacuum()
        self.assertEqual(result["assessment"], "UNKNOWN", result)
        self.assertEqual(result["surfaces"]["upper"]["value"], 6)
        self.assertIsNone(result["surfaces"]["lower"]["value"])
        self.assertEqual(self.work()["value"], {"lower": None, "upper": 4})

    def test_healthy_surface_survives_sibling_plateau_failure(self):
        self.write()
        intervals = {"lower": [6, 7], "upper": INTERVALS["upper"]}
        result = self.vacuum(intervals=intervals)
        self.assertEqual(result["assessment"], "FAIL", result)
        self.assertEqual(result["surfaces"]["upper"]["value"], 6)
        self.assertIsNone(result["surfaces"]["lower"]["value"])
        self.assertEqual(self.work(intervals=intervals)["surfaces"]["upper"]["value"], 4)

    def test_axis_mismatch_is_explicit_conflict(self):
        self.write()
        result = self.vacuum(axis=0)
        self.assertEqual(result["assessment"], "FAIL", result)
        self.assertIn("native_locpot_axis_conflict", [f["code"] for f in result["findings"]])
        self.assertTrue(all(side["value"] is None for side in result["surfaces"].values()))

    def test_flat_gauge_shift_not_hidden_by_plateau_ceiling(self):
        self.write(potential_shift=.04)
        result = self.vacuum(thresholds={"max_variation_ev": 100})
        self.assertEqual(result["assessment"], "FAIL", result)
        self.assertTrue(all(side["value"] is None for side in result["surfaces"].values()))
        self.assertIn("native_locpot_vacuum_conflict", [f["code"] for f in result["findings"]])

    def test_crosscheck_and_strict_unbound_potential_remain_provisional(self):
        self.write(native_vacuum=False)
        result = self.vacuum()
        self.assertEqual(result["assessment"], "UNKNOWN", result)
        self.assertIsNone(result["value"])
        self.assertTrue(all(side["value"] is None for side in result["surfaces"].values()))
        diagnostic = self.vacuum(diagnostic=True)
        self.assertTrue(diagnostic["provisional"])
        self.assertEqual(diagnostic["value"], {"lower": 4, "upper": 6})
        self.assertIsNone(self.work(diagnostic=True)["value"])
        strict = self.vacuum(vacuum_source="locpot", locpot_policy="strict")
        self.assertEqual(strict["assessment"], "UNKNOWN", strict)
        self.assertIsNone(strict["value"])

    def test_work_function_is_gauge_invariant(self):
        self.write()
        first = self.work()
        self.write(shift=8)
        second = self.work()
        self.assertEqual(first["assessment"], "PASS", first)
        self.assertEqual(second["assessment"], "PASS", second)
        self.assertEqual(first["value"], second["value"])

    def write_rounded(self):
        # Six-decimal fractional output in a long cell can exceed 2e-5 A
        # without establishing a different geometry (J26.1 regression).
        length = 42.06664896431752
        positions = ((1.5, 2, length * .30000049), (1.5, 2, 31.077704481677))
        texts = native_texts(cell=((3, 0, 0), (0, 4, 0), (0, 0, length)),
                             positions=positions, settings={"LVHAR": "T", "LVTOT": "F", "IDIPOL": "3"})
        for role in ("OUTCAR", "OSZICAR"):
            (self.run / role).write_text(texts[role])
        lines = locpot_text(profiles=[[4] * 60]).splitlines()
        lines[2:5] = ["3.000000 0.000000 0.000000", "0.000000 4.000000 0.000000",
                      f"0.000000 0.000000 {length:.6f}"]
        lines[5:7] = ["H He", "1 1"]
        lines[8:10] = [f"0.500000 0.500000 {p[2] / length:.6f}" for p in positions]
        (self.run / "LOCPOT").write_text("\n".join(lines) + "\n")
        return length, positions, lines

    def test_rounded_locpot_header_is_unresolved_not_a_geometry_conflict(self):
        length, positions, lines = self.write_rounded()
        result = self.vacuum(vacuum_source="locpot", intervals={"lower": [1, 3], "upper": [39, 41]})
        self.assertEqual(result["assessment"], "UNKNOWN", result)
        self.assertIn("locpot_geometry_precision_insufficient", [f["code"] for f in result["findings"]])
        self.assertNotIn("locpot_geometry_conflict", [f["code"] for f in result["findings"]])
        self.assertIsNone(result["value"])
        # A discrepancy well beyond the same printed bounds still fails.
        lines[8] = f"0.500000 0.500000 {positions[0][2] / length + .001:.6f}"
        (self.run / "LOCPOT").write_text("\n".join(lines) + "\n")
        conflict = self.vacuum(intervals={"lower": [1, 3], "upper": [39, 41]})
        self.assertEqual(conflict["assessment"], "FAIL", conflict)
        self.assertIn("locpot_geometry_conflict", [f["code"] for f in conflict["findings"]])

    def write_electronic(self):
        self.write(native_vacuum=False)
        electronic_run(self.run, vacuum=False, xml=False,
                       cell=((3, 0, 0), (0, 4, 0), (0, 0, 30)),
                       positions=((1.5, 2, 8), (1.5, 2, 12)))

    def test_standard_returns_qualified_vacuum_and_all_derived_values(self):
        self.write_electronic()
        ctx = AnalysisContext(self.run, vacuum_source="locpot", intervals=INTERVALS)
        for getter, expected in [(get_vacuum_level, {"lower": 4, "upper": 6}),
                                 (get_work_function, {"lower": 2, "upper": 4}),
                                 (get_ionization_energy, {"lower": 3, "upper": 5}),
                                 (get_electron_affinity, {"lower": 1, "upper": 3})]:
            result = getter(ctx)
            self.assertEqual(result["assessment"], "PASS", result)
            self.assertEqual(result["value"], expected)
            self.assertTrue(any("CONSISTENT_UNAUTHENTICATED" in q for q in result["qualifications"]))
            for side in result["surfaces"].values():
                self.assertEqual(side["source_consistency"], "CONSISTENT_UNAUTHENTICATED")
                self.assertFalse(side["native_corroboration"]["available"])
                self.assertTrue(side["qualifications"])
            json.dumps(result, allow_nan=False)
        for policy, source in [("strict", "locpot"), ("standard", "crosscheck"), ("standard", "native")]:
            ctx = AnalysisContext(self.run, vacuum_source=source, locpot_policy=policy, intervals=INTERVALS)
            for getter in (get_vacuum_level, get_work_function, get_ionization_energy, get_electron_affinity):
                result = getter(ctx)
                self.assertEqual(result["assessment"], "UNKNOWN", result)
                self.assertIsNone(result["value"])
            if source == "native":
                self.assertNotIn("LOCPOT", ctx._sources)

    def test_standard_does_not_ignore_native_conflicts_or_foreign_geometry(self):
        self.write(potential_shift=.04)
        result = self.vacuum(vacuum_source="locpot")
        self.assertEqual(result["assessment"], "FAIL", result)
        self.assertEqual(result["surfaces"]["upper"]["source_consistency"], "CONFLICTING")
        self.assertIsNone(result["value"])
        self.write(native_vacuum=False)
        path = self.run / "LOCPOT"
        path.write_text(path.read_text().replace("H He\n", "He H\n"))
        result = self.vacuum(vacuum_source="locpot")
        self.assertEqual(result["assessment"], "FAIL", result)
        self.assertIsNone(result["value"])

    def test_standard_keeps_surface_independence_and_unreported_native_warnings(self):
        self.write(native_vacuum=False)
        result = self.vacuum(vacuum_source="locpot", intervals={"lower": [6, 7], "upper": [15, 19]})
        self.assertEqual(result["assessment"], "FAIL", result)
        self.assertIsNone(result["surfaces"]["lower"]["value"])
        self.assertEqual(result["surfaces"]["upper"]["value"], 6)
        self.write(native_vacuum=False, warning=" WARNING: no field-free vacuum region on lower side\n")
        result = self.vacuum(vacuum_source="locpot")
        self.assertEqual(result["assessment"], "UNKNOWN", result)
        self.assertIsNone(result["surfaces"]["lower"]["value"])
        self.assertEqual(result["surfaces"]["upper"]["value"], 6)

    def test_standard_cannot_use_a_historical_endpoint(self):
        self.write(native_vacuum=False)
        texts = native_texts(evaluations=2, cell=((3, 0, 0), (0, 4, 0), (0, 0, 30)),
                             positions=((1.5, 2, 8), (1.5, 2, 12)),
                             settings={"LVHAR": "T", "LVTOT": "F", "IDIPOL": "3"})
        for role in ("OUTCAR", "OSZICAR"):
            (self.run / role).write_text(texts[role])
        result = self.vacuum(vacuum_source="locpot", evaluation=0)
        self.assertEqual(result["assessment"], "UNKNOWN", result)
        self.assertIsNone(result["value"])
        self.assertIn("locpot_historical_endpoint_unresolved", [f["code"] for f in result["findings"]])

    def test_policy_and_source_changes_invalidate_the_request(self):
        self.write(native_vacuum=False)
        ctx = AnalysisContext(self.run, vacuum_source="locpot", intervals=INTERVALS)
        self.assertEqual(get_vacuum_level(ctx)["assessment"], "PASS")
        ctx.locpot_policy = "strict"
        self.assertEqual(get_vacuum_level(ctx)["findings"][0]["code"], "request_invalidated")
        ctx = AnalysisContext(self.run, vacuum_source="locpot", intervals=INTERVALS)
        self.assertEqual(get_vacuum_level(ctx)["assessment"], "PASS")
        path = self.run / "LOCPOT"
        path.write_text(path.read_text() + "\n")
        self.assertEqual(get_vacuum_level(ctx)["findings"][0]["code"], "request_invalidated")
        self.assertEqual(self.vacuum(locpot_policy="other")["findings"][0]["code"], "invalid_locpot_policy")

    def test_standard_rejects_unknown_potential_and_truncated_grid(self):
        self.write(settings={"LVHAR": "F"}, native_vacuum=False)
        self.assertEqual(self.vacuum(vacuum_source="locpot")["assessment"], "UNSUPPORTED")
        self.write(native_vacuum=False)
        path = self.run / "LOCPOT"
        path.write_text(path.read_text()[:-100])
        self.assertEqual(self.vacuum(vacuum_source="locpot")["assessment"], "FAIL")

    def test_endpoint_xml_can_establish_exact_header_rounding_not_just_overlap(self):
        length, positions, _ = self.write_rounded()
        cell = ((3, 0, 0), (0, 4, 0), (0, 0, length))
        electronic_run(self.run, vacuum=False, cell=cell, positions=positions)
        path = self.run / "vasprun.xml"
        root = ET.fromstring(path.read_text())
        for node, row in zip(root.findall(".//structure/crystal/varray/v"), cell):
            node.text = " ".join(f"{x:.12f}" for x in row)
        for node, row in zip(root.findall(".//structure/varray/v"), positions):
            node.text = " ".join(f"{x / cell[i][i]:.12f}" for i, x in enumerate(row))
        content = ET.tostring(root, encoding="unicode") + "\n"
        path.write_text(content)
        options = {"vacuum_source": "locpot", "intervals": {"lower": [1, 3], "upper": [39, 41]}}
        result = self.vacuum(**options)
        self.assertEqual(result["assessment"], "PASS", result)
        self.assertEqual(result["value"], {"lower": 4, "upper": 4})
        self.assertIn("locpot_geometry_consistent_at_source_precision", [f["code"] for f in result["findings"]])
        path.write_text(content.replace("-3.00000000", "-4.00000000"))
        self.assertEqual(self.vacuum(**options)["assessment"], "FAIL")
        path.write_text(content.replace("0.300000490000", "0.300000500000"))
        result = self.vacuum(**options)
        self.assertEqual(result["assessment"], "UNKNOWN", result)
        self.assertIsNone(result["value"])

    def test_cli_policy_numbers_qualifications_and_exits_match_api(self):
        self.write_electronic()
        getters = {"vacuum": get_vacuum_level, "work-function": get_work_function,
                   "ionization-energy": get_ionization_energy, "electron-affinity": get_electron_affinity}
        for policy, expected in [("standard", 0), ("strict", 2)]:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["vasp", "analyze-electronic", str(self.run), "--only", ",".join(getters),
                             "--vacuum-source", "locpot", "--locpot-policy", policy, "--axis", "2",
                             "--lower-interval", "1", "5", "--upper-interval", "15", "19", "--json"])
            self.assertEqual(code, expected)
            cli = json.loads(output.getvalue())
            ctx = AnalysisContext(self.run, vacuum_source="locpot", locpot_policy=policy, axis=2, intervals=INTERVALS)
            for name, getter in getters.items():
                api = getter(ctx)
                for key in ("value", "assessment", "qualifications", "surfaces"):
                    self.assertEqual(cli["metrics"][name][key], api[key], (name, key))


if __name__ == "__main__":
    unittest.main()
