"""Public analysis regression tests with invented, mutually coherent evidence."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.periodic.vasp.analysis_context import AnalysisContext
from cmw.periodic.vasp.scf_analysis import get_scf_history, get_scf_convergence, get_scf_timing, get_scf_summary
from cmw.periodic.vasp.electronic_levels import (get_fermi_level, get_vbm, get_cbm, get_band_gap,
    get_vacuum_level, get_work_function, get_ionization_energy, get_electron_affinity)
from cmw.periodic.vasp.result_sources import SnapshotReader
from .result_fixtures import native_texts, CELL, POSITIONS


def electronic_run(root, *, shift=0, vacuum=True, xml=True, **options):
    settings = {"NELECT": "2", "NBANDS": "2", "NKPTS": "1", "EFERMI": "MIDGAP", "LVHAR": "T", "LVTOT": "F", "LVACPOTAV": "T", "IDIPOL": "3"}
    settings.update(options.pop("settings", {}))
    texts = native_texts(settings=settings, **options)
    vbm, cbm, fermi = 1 + shift, 3 + shift, 2 + shift
    extra = f" E-fermi : {fermi:.10f}\n Fermi energy: {fermi:.10f}\n k-point 1 : 0.00000000 0.00000000 0.00000000\n band No. band energies occupation\n 1 {vbm:.8f} 2.00000000\n 2 {cbm:.8f} 0.00000000\n\n"
    if vacuum:
        extra += f" vacuum level on the upper side and lower side of the slab {4+shift:.8f} {2.5+shift:.8f}\n"
    out = texts["OUTCAR"].replace(" POSITION", extra + " POSITION")
    out = re.sub(r"( free energy    TOTEN = .* eV\n)", r"\1 LOOP: cpu time 1.0000: real time 2.0000\n", out)
    out = out.replace("energy(sigma->0) = -2.99500000\n", "energy(sigma->0) = -2.99500000\n LOOP+: cpu time 4.0000: real time 8.0000\n")
    (root / "OUTCAR").write_text(out)
    (root / "OSZICAR").write_text(texts["OSZICAR"])
    (root / "EIGENVAL").write_text(f"2 2 1 1\n0 0 0 0 0\n0\nCAR\nSynthetic\n2 1 2\n\n0.00000000 0.00000000 0.00000000 1.00000000\n1 {vbm:.8f} 1.00000000\n2 {cbm:.8f} 0.00000000\n")
    if xml:
        parameters = ''.join(f'<i name="{k}" type="{kind}">{v}</i>' for k, kind, v in (
            ("ISPIN", "int", 1), ("NELECT", "float", 2), ("NBANDS", "int", 2), ("ISMEAR", "int", 0)))
        steps = ''.join(f'<scstep><energy><i name="e_fr_energy">{e}</i></energy></scstep>' for e in ("-2.000000000000", "-2.100000000000", "-2.100000050000"))
        cell = ''.join('<v>' + ' '.join(f'{x:.8f}' for x in row) + '</v>' for row in CELL)
        positions = ''.join('<v>' + ' '.join(f'{x / CELL[i][i]:.10f}' for i, x in enumerate(row)) + '</v>' for row in POSITIONS)
        content = f'''<modeling><generator><i name="version" type="string">6.6.1</i></generator>
<parameters>{parameters}</parameters>
<atominfo><array name="atoms"><set><rc><c>H</c><c>1</c></rc><rc><c>He</c><c>2</c></rc></set></array></atominfo>
<kpoints><varray name="kpointlist"><v>0.00000000 0.00000000 0.00000000</v></varray><varray name="weights"><v>1.00000000</v></varray></kpoints>
<calculation>{steps}<energy><i name="e_fr_energy">-3.00000000</i></energy>
<structure><crystal><varray name="basis">{cell}</varray></crystal><varray name="positions">{positions}</varray></structure>
<eigenvalues><array><set><set comment="spin 1"><set comment="kpoint 1"><r>{vbm:.8f} 1.00000000</r><r>{cbm:.8f} 0.00000000</r></set></set></set></array></eigenvalues>
<dos><i name="efermi">{fermi:.10f}</i></dos></calculation></modeling>\n'''
        (root / "vasprun.xml").write_text(content)
    return root


class AnalysisAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def mutate(self, role, old, new):
        p = self.root / role
        self.assertIn(old, p.read_text())
        p.write_text(p.read_text().replace(old, new))

    def test_import_and_context_construction_have_no_io(self):
        with patch.object(SnapshotReader, "__enter__", side_effect=AssertionError("unexpected I/O")), patch.object(Path, "stat", side_effect=AssertionError("unexpected stat")):
            AnalysisContext("/does/not/exist")

    def test_independent_calls_and_one_parse_per_shared_source(self):
        electronic_run(self.root)
        opened = []
        original = SnapshotReader.__enter__
        def enter(reader):
            opened.append(reader.record["role"])
            return original(reader)
        ctx = AnalysisContext(self.root)
        with patch.object(SnapshotReader, "__enter__", enter):
            self.assertEqual(get_fermi_level(ctx)["value"], 2)
            self.assertEqual(opened, ["OUTCAR", "OSZICAR"])
            self.assertEqual(get_vbm(ctx)["value"], 1)
            self.assertEqual(get_cbm(ctx)["value"], 3)
            self.assertEqual(get_band_gap(ctx)["value"], 2)
        self.assertEqual(opened, ["OUTCAR", "OSZICAR", "vasprun.xml"])

    def test_xml_and_eigenval_native_correspondence(self):
        electronic_run(self.root)
        self.assertEqual(get_band_gap(self.root, eigenvalue_source="eigenval")["value"], 2)
        self.mutate("EIGENVAL", "3.00000000", "4.00000000")
        self.assertEqual(get_band_gap(self.root, eigenvalue_source="eigenval")["assessment"], "FAIL")
        self.assertEqual(get_band_gap(self.root)["assessment"], "PASS")  # unread source is not a dependency
        self.mutate("vasprun.xml", "-3.00000000", "-4.00000000")
        self.assertEqual(get_band_gap(self.root)["assessment"], "FAIL")

    def test_partial_xml_never_falls_back_to_present_eigenval(self):
        electronic_run(self.root)
        self.mutate("vasprun.xml", "</modeling>", "")
        self.assertNotEqual(get_band_gap(self.root)["assessment"], "PASS")
        self.assertEqual(get_band_gap(self.root, eigenvalue_source="eigenval")["assessment"], "PASS")

    def test_gauge_invariant_derived_values_negative_ea_and_gap_identity(self):
        for shift in (0, 17.25):
            electronic_run(self.root, shift=shift)
            ctx = AnalysisContext(self.root)
            ie, ea = get_ionization_energy(ctx), get_electron_affinity(ctx)
            self.assertEqual(ie["assessment"], "PASS", ie)
            self.assertEqual(ie["value"], {"upper": 3, "lower": 1.5})
            self.assertEqual(ea["value"], {"upper": 1, "lower": -.5})
            for side in ("upper", "lower"):
                self.assertEqual(ie["value"][side] - ea["value"][side], get_band_gap(ctx)["value"])
            self.assertEqual(get_work_function(ctx)["value"], {"upper": 2, "lower": .5})

    def test_missing_vacuum_does_not_read_locpot_or_imply_bulk(self):
        electronic_run(self.root, vacuum=False)
        (self.root / "LOCPOT").write_text("intentionally invalid, must not be opened")
        ctx = AnalysisContext(self.root)
        self.assertEqual(get_vacuum_level(ctx)["assessment"], "UNKNOWN")
        self.assertNotIn("LOCPOT", ctx._sources)
        self.assertEqual(get_work_function(ctx)["assessment"], "UNKNOWN")
        self.assertEqual(get_vacuum_level(self.root, no_vacuum=True)["assessment"], "NOT_APPLICABLE")

    def test_source_mutation_and_option_mutation_invalidate_request(self):
        electronic_run(self.root)
        ctx = AnalysisContext(self.root)
        self.assertEqual(get_fermi_level(ctx)["assessment"], "PASS")
        self.mutate("OUTCAR", "2.0000000000", "2.0000000001")
        self.assertEqual(get_vbm(ctx)["findings"][0]["code"], "request_invalidated")
        ctx2 = AnalysisContext(self.root)
        get_scf_history(ctx2)
        ctx2.evaluation = 0
        self.assertEqual(get_scf_history(ctx2)["findings"][0]["code"], "request_invalidated")

    def test_invalid_options_stay_invalid(self):
        electronic_run(self.root)
        ctx = AnalysisContext(self.root, occupation_tolerance=-1)
        for _ in range(2):
            self.assertEqual(get_fermi_level(ctx)["findings"][0]["code"], "invalid_option")

    def test_invalid_optional_source_does_not_poison_independent_fermi(self):
        electronic_run(self.root)
        ctx = AnalysisContext(self.root, vacuum_source="locpot", axis=2)
        self.assertNotEqual(get_vacuum_level(ctx)["assessment"], "PASS")
        self.assertEqual(get_fermi_level(ctx)["assessment"], "PASS")

    def test_diagnostic_nonconverged_values_do_not_promote_derived(self):
        electronic_run(self.root, native=False, terminal_de="-0.10000000E-02")
        self.assertIsNone(get_fermi_level(self.root)["value"])
        ctx = AnalysisContext(self.root, diagnostic=True)
        self.assertEqual(get_fermi_level(ctx)["value"], 2)
        self.assertIsNone(get_work_function(ctx)["value"])

    def test_hse_is_bounded_and_does_not_change_legacy_scope(self):
        from cmw.periodic.vasp.results import inspect_result
        settings = {"LHFCALC": "T", "GGA": "PE", "AEXX": ".25", "HFSCREEN": ".2"}
        electronic_run(self.root, settings=settings)
        self.assertEqual(get_scf_summary(self.root)["assessment"], "PASS")
        self.assertFalse(inspect_result(self.root)["mode"]["supported"])
        electronic_run(self.root, settings={**settings, "IALGO": "38"}, algorithm="RMM")
        self.assertEqual(get_scf_summary(self.root)["assessment"], "UNSUPPORTED")

    def test_boxed_performance_recommendations_are_not_effective_settings(self):
        electronic_run(self.root)
        self.mutate("OUTCAR", " NIONS = 2", " NIONS = 2\n NCORE=1\n| NCORE=2 is recommended |\n| default NCORE=1 |")
        self.assertEqual(get_scf_summary(self.root)["assessment"], "PASS")

    def test_multi_segments_require_selection_and_latest_incomplete_is_not_skipped(self):
        electronic_run(self.root)
        first = (self.root / "OUTCAR").read_text()
        (self.root / "OUTCAR").write_text(first * 2)
        self.assertEqual(get_scf_summary(self.root)["findings"][0]["code"], "select_segment")
        (self.root / "OUTCAR").write_text(first.split(" General timing")[0] + " Iteration 2( 1)\n EDDAV:\n free energy    TOTEN = -1.000000 eV\n")
        result = get_scf_summary(self.root)
        self.assertEqual(result["value"]["selected_evaluation"], 1)
        self.assertEqual(result["assessment"], "UNKNOWN")

    def test_partial_lines_preserve_prefix_without_final_pass(self):
        electronic_run(self.root)
        with (self.root / "OSZICAR").open("a") as stream:
            stream.write("DAV: 1 -")
        history = get_scf_history(self.root)
        self.assertEqual(len(history["value"][0]["electronic_table"]["matched_rows"]), 3)
        self.assertEqual(get_scf_summary(self.root)["assessment"], "UNKNOWN")

    def test_empty_oszicar_and_explicit_stdout_and_conflicting_peer(self):
        electronic_run(self.root)
        stdout = self.root / "stdout.log"
        stdout.write_text((self.root / "OSZICAR").read_text())
        (self.root / "OSZICAR").write_text("")
        self.assertEqual(get_scf_summary(self.root, stdout=stdout)["assessment"], "PASS")
        (self.root / "OSZICAR").write_text(stdout.read_text().replace("-2.000000000000", "-9.000000000000"))
        self.assertEqual(get_scf_summary(self.root, stdout=stdout)["assessment"], "FAIL")
        (self.root / "OSZICAR").write_text("DAV: 1 bad\n")
        self.assertNotEqual(get_scf_summary(self.root, stdout=stdout)["assessment"], "PASS")

    def test_buffered_native_history_retains_source_only_rows(self):
        electronic_run(self.root)
        out = (self.root / "OUTCAR").read_text()
        # OUTCAR has only the first electronic step, while OSZICAR has advanced.
        (self.root / "OUTCAR").write_text(out[:out.index("--------------------------------------- Iteration      1(   2)")])
        table = get_scf_history(self.root)["value"][0]["electronic_table"]
        self.assertEqual(table["correspondence"], "pending")
        self.assertEqual(len(table["matched_rows"]), 1)
        self.assertEqual(len(table["source_rows"]["OSZICAR"]), 3)

    def test_surplus_tables_and_native_stop_conflict(self):
        electronic_run(self.root)
        osz = self.root / "OSZICAR"
        osz.write_text(osz.read_text() * 2)
        self.assertEqual(get_scf_summary(self.root)["assessment"], "FAIL")
        electronic_run(self.root)
        self.mutate("OUTCAR", "aborting loop because EDIFF is reached", "EDIFF was not reached\n aborting loop because EDIFF is reached")
        self.assertEqual(get_scf_convergence(self.root)["assessment"], "FAIL")

    def test_fixed_count_rounding_and_nelm(self):
        electronic_run(self.root, ediff="0", native=None)
        self.assertEqual(get_scf_convergence(self.root)["assessment"], "NOT_APPLICABLE")
        electronic_run(self.root, terminal_de="0.1000E-05")
        self.assertEqual(get_scf_convergence(self.root)["value"]["numeric_checks"]["dE"], "UNKNOWN")
        electronic_run(self.root)
        result = get_scf_convergence(self.root)
        self.assertTrue(result["value"]["nelm_reached"])
        self.assertEqual(result["assessment"], "PASS")
        self.assertNotIn("rms_c", result["value"]["final_electronic_row"])

    def test_complete_history_exceeds_live_evaluation_and_iteration_limits(self):
        texts = native_texts(relaxation=True, evaluations=20)
        for role in ("OUTCAR", "OSZICAR"):
            (self.root / role).write_text(texts[role])
        self.assertEqual(len(get_scf_history(self.root)["value"]), 20)
        electronic_run(self.root, nelm=2000)
        out = (self.root / "OUTCAR").read_text()
        start = out.index("--------------------------------------- Iteration")
        end = out.index("------------------------ aborting loop")
        rows = ''.join(f" Iteration 1( {i})\n EDDAV:\n free energy    TOTEN = -2.100000050000 eV\n" for i in range(1, 1101))
        (self.root / "OUTCAR").write_text(out[:start] + rows + out[end:])
        osz = ''.join(f"DAV: {i} -2.100000050000 -0.00000005 -0.00000004 12 0.001\n" for i in range(1, 1101))
        (self.root / "OSZICAR").write_text(osz + "1 F= -3.00000000 E0= -2.99500000 d E = 0.0\n")
        self.assertEqual(len(get_scf_history(self.root)["value"][0]["iterations"]), 1100)
        self.assertEqual(get_scf_summary(self.root)["assessment"], "PASS")

    def test_timers_separate_nested_scopes_and_invalid_derived_sum(self):
        electronic_run(self.root)
        value = get_scf_timing(self.root)
        self.assertEqual(value["assessment"], "PASS")
        self.assertEqual(value["value"]["electronic_loop_sum"]["wall_seconds"], 6)
        self.assertEqual(value["value"]["outer_loop_sum"]["wall_seconds"], 8)
        self.mutate("OUTCAR", "LOOP: cpu time 1.0000: real time 2.0000", "LOOP: cpu time 1.0E308: real time 1.0E308")
        result = get_scf_timing(self.root)
        self.assertEqual(result["assessment"], "FAIL")
        self.assertIsNone(result["value"]["electronic_loop_sum"]["wall_seconds"])
        json.dumps(result, allow_nan=False)

    def test_nonfinite_derived_energy_is_null_not_infinity(self):
        electronic_run(self.root)
        self.mutate("OUTCAR", "E-fermi : 2.0000000000", "E-fermi : -1.0E308")
        self.mutate("OUTCAR", "Fermi energy: 2.0000000000", "Fermi energy: -1.0E308")
        self.mutate("OUTCAR", "slab 4.00000000 2.50000000", "slab 1.0E308 1.0E308")
        result = get_work_function(self.root)
        self.assertEqual(result["assessment"], "FAIL")
        self.assertIsNone(result["value"])
        self.assertIn("nonfinite_derived_energy", [f["code"] for f in result["findings"]])
        json.dumps(result, allow_nan=False)

    def test_cli_all_requested_metrics_and_exit_codes(self):
        electronic_run(self.root, vacuum=False)
        stream = StringIO()
        with redirect_stdout(stream):
            code = main(["vasp", "analyze-electronic", str(self.root), "--only", "fermi,vbm,cbm,gap,work-function", "--json"])
        result = json.loads(stream.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(len(result["metrics"]), 5)
        self.assertEqual(result["metrics"]["gap"]["value"], 2)
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(["vasp", "analyze-scf", str(self.root), "--json"]), 0)
        json.loads(stream.getvalue())


if __name__ == "__main__":
    unittest.main()
