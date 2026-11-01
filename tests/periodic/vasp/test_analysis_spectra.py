"""Synthetic spectra only: no engine, scientific directory, or licensed data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.analysis_spectra import analyze_spectrum, read_eigenval, read_xml
from cmw.periodic.vasp.results import number


def band(energy, occupation, label):
    return {"energy": number(f"{energy:.8f}"), "occupation": number(f"{occupation:.8f}"),
            "native_band_label": label}


def spectrum(energies=None, occupations=None, weights=None, nspin=1, format="eigenval"):
    energies = energies or [[[0.0, 2.0], [0.5, 2.5]] for _ in range(nspin)]
    occupations = occupations or [[[1.0, 0.0], [1.0, 0.0]] for _ in range(nspin)]
    weights = weights or [0.5, 0.5]
    return {"format": format, "complete": True, "findings": [], "nspin": nspin,
            "nkpoints": len(weights), "nbands": len(energies[0][0]), "nelect": 2.0,
            "kpoints": [{"fractional": [i / 2, 0, 0], "weight": weight} for i, weight in enumerate(weights)],
            "eigenvalues": [[[band(e, o, b + 1) for b, (e, o) in enumerate(zip(es, os))]
                              for es, os in zip(espin, ospin)] for espin, ospin in zip(energies, occupations)]}


def xml_calculation(energy=-1.0, scsteps=2):
    step = '<scstep><energy><i name="e_fr_energy">-1.00000000</i></energy></scstep>'
    return ("<calculation>" + step * scsteps +
            f'<energy><i name="e_fr_energy">{energy:.8f}</i><i name="e_0_energy">{energy:.8f}</i></energy>'
            '<structure><crystal><varray name="basis"><v>1 0 0</v><v>0 1 0</v><v>0 0 1</v></varray></crystal>'
            '<varray name="positions"><v>0 0 0</v></varray></structure>'
            '<eigenvalues><array><set><set comment="spin 1"><set comment="kpoint 1">'
            '<r>0.00000000 1.00000000</r><r>2.00000000 0.00000000</r>'
            '</set></set></set></array></eigenvalues><dos><i name="efermi">1.00000000</i>'
            '<total><array><set><set><r>0 0 0</r></set></set></array></total></dos></calculation>')


def xml_document(calculations=None):
    return ('<?xml version="1.0"?><modeling><generator><i name="version" type="string">6.6.1</i></generator>'
            '<incar><i name="ISPIN" type="int">2</i></incar>'
            '<parameters><separator><i name="ISPIN" type="int">1</i><i name="NBANDS" type="int">2</i>'
            '<i name="NELECT">2.00000000</i><i name="ISMEAR" type="int">0</i></separator></parameters>'
            '<kpoints><varray name="kpointlist"><v>0 0 0</v></varray>'
            '<varray name="weights"><v>1.00000000</v></varray></kpoints>' +
            (calculations if calculations is not None else xml_calculation()) + '</modeling>\n')


class SpectrumTests(unittest.TestCase):
    def assess(self, value, **kwargs):
        settings = {"ISPIN": value["nspin"], "ISMEAR": 0, "NELECT": value["nelect"]}
        return analyze_spectrum(value, settings, **kwargs)

    def test_indirect_gap_signed_energies_and_native_indices(self):
        value = spectrum(energies=[[[-3, -1], [-2.5, -0.5]]])
        result = self.assess(value)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["classification"], "semiconductor")
        self.assertEqual(result["gap"], 1.5)
        self.assertEqual(result["vbm"]["kpoint_index"], 1)
        self.assertEqual(result["cbm"]["native_band_label"], 2)
        self.assertEqual(result["electron_count"]["observed"], 2)

    def test_format_specific_normalization(self):
        standard = self.assess(spectrum())
        outcar = spectrum(format="outcar", occupations=[[[2, 0], [2, 0]]])
        self.assertEqual(self.assess(outcar)["gap"], standard["gap"])
        self.assertEqual(self.assess(outcar)["occupation_normalization"]["raw_full_occupation"], 2)
        self.assertEqual(standard["occupation_normalization"]["raw_full_occupation"], 1)
        self.assertEqual(self.assess(spectrum(occupations=[[[2, 0], [2, 0]]]))["status"], "FAIL")

    def test_zero_weight_default_exclusion_and_all_sampled(self):
        value = spectrum(energies=[[[0, 2], [1.9, 2.5]]], weights=[1, 0])
        self.assertEqual(self.assess(value)["gap"], 2)
        self.assertAlmostEqual(self.assess(value, all_points=True)["gap"], 0.1)
        self.assertEqual(self.assess(value)["excluded_zero_weight_indices"], [1])

    def test_missing_empty_bands_never_positive_gap(self):
        value = spectrum(energies=[[[0], [1]]], occupations=[[[1], [1]]])
        result = self.assess(value)
        self.assertEqual(result["classification"], "insufficient")
        self.assertIsNone(result["gap"])
        self.assertIsNone(result["cbm"])

    def test_intermediate_occupations_and_crossing(self):
        value = spectrum(occupations=[[[0.5, 0.5], [0.5, 0.5]]])
        self.assertNotEqual(self.assess(value)["classification"], "semiconductor")
        self.assertFalse((self.assess(value)["gap"] or 0) > 0)
        crossing = spectrum(occupations=[[[1, 0], [0, 1]]])
        self.assertEqual(self.assess(crossing)["classification"], "metallic")

    def test_collinear_half_metal_and_normalization(self):
        value = spectrum(nspin=2, occupations=[[[1, 0], [1, 0]], [[0.5, 0.5], [0.5, 0.5]]])
        result = self.assess(value)
        self.assertEqual(result["classification"], "half-metallic")
        self.assertEqual(result["gap"], 0)
        self.assertEqual(result["occupation_normalization"]["spin_degeneracy"], 1)

    def test_cross_spin_overlap(self):
        value = spectrum(nspin=2, energies=[[[0, 1], [0, 1]], [[2, 3], [2, 3]]])
        self.assertEqual(self.assess(value)["classification"], "metallic")
        self.assertEqual(self.assess(value)["gap"], 0)

    def test_semimetal_and_printed_precision(self):
        value = spectrum(energies=[[[0, 1e-6], [0, 1e-6]]])
        self.assertEqual(self.assess(value)["classification"], "unresolved-semimetal")
        self.assertIsNone(self.assess(value)["gap"])
        value = spectrum()
        value["eigenvalues"][0][0][0]["energy"] = number("0.0")
        value["eigenvalues"][0][1][0]["energy"] = number("0.0")
        value["eigenvalues"][0][0][1]["energy"] = number("0.1")
        value["eigenvalues"][0][1][1]["energy"] = number("0.1")
        self.assertIsNone(self.assess(value)["gap"])

    def test_occupation_tolerance_boundary_is_ambiguous(self):
        value = spectrum(occupations=[[[0.999, 0.001], [0.999, 0.001]]])
        result = self.assess(value)
        self.assertEqual(result["classification"], "ambiguous-occupations")
        self.assertIsNone(result["gap"])

    def test_invalid_nonfinite_inconsistent_dimensions_counts_and_smearing(self):
        for mutation in (lambda s: s["eigenvalues"][0][0][0].update(energy=number("NaN")),
                         lambda s: s["eigenvalues"][0][0].pop(),
                         lambda s: s.update(nelect=3),
                         lambda s: s["kpoints"][0].update(weight=-1)):
            value = spectrum()
            mutation(value)
            self.assertEqual(self.assess(value)["status"], "FAIL")
        value = spectrum()
        for setting in ({"ISMEAR": 1}, {"LSORBIT": True}, {"LNONCOLLINEAR": True}):
            settings = {"ISPIN": 1, "ISMEAR": 0, "NELECT": 2, **setting}
            self.assertEqual(analyze_spectrum(value, settings)["status"], "UNSUPPORTED")

    def test_gauge_invariance_and_no_mutation(self):
        original = spectrum()
        saved = deepcopy(original)
        result = self.assess(original)
        shifted = deepcopy(original)
        for channel in shifted["eigenvalues"]:
            for rows in channel:
                for row in rows:
                    row["energy"] = number(f'{row["energy"]["value"] + 13:.8f}')
        self.assertEqual(result["gap"], self.assess(shifted)["gap"])
        self.assertEqual(original, saved)
        json.dumps(result, allow_nan=False)

    def test_nonfinite_derived_separation_never_leaks_into_json(self):
        value = spectrum()
        for rows in value["eigenvalues"][0]:
            rows[0]["energy"] = number("-1e308")
            rows[1]["energy"] = number("1e308")
        result = self.assess(value)
        self.assertEqual(result["status"], "FAIL")
        self.assertIsNone(result["gap"])
        json.dumps(result, allow_nan=False)

    def test_tied_extrema_are_not_uniquely_identified(self):
        value = spectrum(energies=[[[0, 2], [0, 2]]])
        self.assertEqual(len(self.assess(value)["vbm"]["precision_tied_states"]), 2)
        result = self.assess(spectrum(nspin=2))
        self.assertEqual(len(result["vbm"]["precision_tied_states"]), 2)
        self.assertEqual(len(result["per_spin"][0]["vbm"]["precision_tied_states"]), 1)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.addCleanup(self.directory.cleanup)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def test_xml_metadata_evidence_and_selective_payload(self):
        result = read_xml(self.write("vasprun.xml", xml_document()))
        self.assertTrue(result["complete"])
        self.assertEqual(result["settings"]["ISPIN"], 1)
        self.assertEqual(result["selected_calculation"]["scstep_count"], 2)
        self.assertEqual(result["selected_calculation"]["energies"]["e_fr_energy"]["value"], -1)
        self.assertEqual(result["selected_calculation"]["fermi"]["value"], 1)
        self.assertEqual(result["selected_calculation"]["geometry"]["cell"][2], [0, 0, 1])
        self.assertEqual(analyze_spectrum(result["spectrum"], result["settings"])["gap"], 2)
        self.assertEqual(result["source"]["coverage"], "complete")
        fermi_only = read_xml(self.root / "vasprun.xml", include_spectrum=False)
        self.assertIsNone(fermi_only["spectrum"])
        self.assertEqual(fermi_only["selected_calculation"]["fermi"]["value"], 1)

    def test_xml_latest_and_explicit_calculation_selection(self):
        path = self.write("vasprun.xml", xml_document(xml_calculation(-1) + xml_calculation(-2)))
        result = read_xml(path)
        self.assertEqual(result["selected_calculation"]["index"], 1)
        self.assertIsNone(result["calculations"][0]["spectrum"])
        selected = read_xml(path, 0)
        self.assertEqual(selected["selected_calculation"]["index"], 0)
        self.assertIsNone(selected["calculations"][1]["spectrum"])
        self.assertIsNone(read_xml(path, 7)["selected_calculation"])

    def test_xml_partial_latest_never_falls_back(self):
        document = xml_document(xml_calculation() + "<calculation><scstep>").replace("</modeling>", "")
        result = read_xml(self.write("vasprun.xml", document))
        self.assertFalse(result["complete"])
        self.assertEqual(result["selected_calculation"]["index"], 1)
        self.assertFalse(result["selected_calculation"]["complete"])

    def test_xml_ordered_species_and_precision(self):
        atominfo = '<atominfo><array name="atoms"><set><rc><c>Zn</c><c>1</c></rc></set></array></atominfo>'
        document = xml_document().replace('<calculation>', atominfo + '<calculation>')
        result = read_xml(self.write("vasprun.xml", document))
        geometry = result["selected_calculation"]["geometry"]
        self.assertEqual(geometry["elements"], ["Zn"])
        self.assertEqual(len(geometry["fractional_tokens"][0]), 3)
        self.assertEqual(len(result["kpoints"][0]["fractional_tokens"]), 3)

    def test_xml_projected_duplicate_eigenvalues_are_not_parsed(self):
        projected = ('<projected><eigenvalues><array><set><set comment="spin 1">'
                     '<set comment="kpoint 1"><r>99 99</r></set></set></set></array></eigenvalues></projected>')
        document = xml_document().replace('</calculation>', projected + '</calculation>')
        result = read_xml(self.write("vasprun.xml", document))
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["spectrum"]["eigenvalues"][0][0]), 2)
        self.assertEqual(analyze_spectrum(result["spectrum"], result["settings"])["gap"], 2)

    def test_xml_entities_and_nonfinite_rows_fail_closed(self):
        for text in ('<!DOCTYPE modeling [<!ENTITY x "payload">]>' + xml_document(),
                     xml_document().replace("<v>0 0 0</v>", "<v>NaN 0 0</v>")):
            result = read_xml(self.write("vasprun.xml", text))
            self.assertFalse(result["complete"])
            self.assertTrue(result["findings"])

    def test_eigenval_restricted_and_collinear(self):
        for nspin, rows in ((1, "1 0.000000 1.000000\n2 2.000000 0.000000\n"),
                            (2, "1 0.000000 0.500000 1.000000 1.000000\n2 2.000000 2.500000 0.000000 0.000000\n")):
            text = f"1 1 1 {nspin}\nignored\nignored\nCAR\nsynthetic\n2 1 2\n\n0 0 0 1\n" + rows
            result = read_eigenval(self.write("EIGENVAL", text))
            self.assertTrue(result["complete"])
            self.assertEqual(len(result["eigenvalues"]), nspin)
            self.assertEqual(result["eigenvalues"][0][0][1]["occupation"]["value"], 0)
            self.assertEqual(analyze_spectrum(result, {"ISPIN": nspin, "ISMEAR": 0, "NELECT": 2})["status"], "PASS")

    def test_eigenval_partial_wrong_dimensions_and_extra_data(self):
        base = "1 1 1 1\nx\nx\nCAR\nsynthetic\n2 1 2\n\n0 0 0 1\n1 0.000000 1.000000\n2 2.000000 0.000000\n"
        for text in (base.rstrip(), base.replace("2 1 2", "2 1 3"), base + "extra\n", base.replace("2 2.000000", "3 2.000000")):
            self.assertFalse(read_eigenval(self.write("EIGENVAL", text))["complete"])


if __name__ == "__main__":
    unittest.main()
