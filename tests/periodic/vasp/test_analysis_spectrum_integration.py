"""Malformed optional spectra remain per-metric outcomes, never public crashes."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import re
import tempfile
import unittest

from cmw.cli import main
from cmw.periodic.vasp.electronic_levels import get_band_gap, get_fermi_level
from .test_analysis_api import electronic_run


class MalformedSpectrumIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        electronic_run(self.root)

    def replace_xml(self, transform):
        source = self.root / "vasprun.xml"
        original = source.read_text()
        replacement = transform(original)
        self.assertNotEqual(original, replacement)
        source.write_text(replacement)

    def test_truncated_eigenval_header_is_unavailable_without_exception(self):
        (self.root / "EIGENVAL").write_text("2 2 1 1\n")
        result = get_band_gap(self.root, eigenvalue_source="eigenval")
        self.assertNotEqual(result["assessment"], "PASS")
        self.assertIsNone(result["value"])
        self.assertEqual(get_band_gap(self.root)["assessment"], "PASS")

    def test_missing_xml_eigenvalues_fails_without_eigenval_fallback(self):
        self.replace_xml(lambda text: re.sub(r"<eigenvalues>.*?</eigenvalues>", "", text))
        result = get_band_gap(self.root)
        self.assertEqual(result["assessment"], "FAIL")
        self.assertIsNone(result["value"])
        self.assertEqual(get_band_gap(self.root, eigenvalue_source="eigenval")["assessment"], "PASS")

    def test_invalid_nonempty_xml_cell_fails_before_matrix_product(self):
        self.replace_xml(lambda text: re.sub(r'(<varray name="basis">)<v>[^<]+</v>', r"\1", text))
        result = get_band_gap(self.root)
        self.assertEqual(result["assessment"], "FAIL")
        self.assertIsNone(result["value"])
        self.assertIn("invalid_xml_geometry_dimensions", [item["code"] for item in result["findings"]])

    def test_missing_xml_geometry_is_unknown(self):
        self.replace_xml(lambda text: re.sub(r"<structure>.*?</structure>", "", text))
        result = get_band_gap(self.root)
        self.assertEqual(result["assessment"], "UNKNOWN")
        self.assertIsNone(result["value"])

    def test_coarse_xml_geometry_cannot_claim_exact_correspondence(self):
        self.replace_xml(lambda text: text.replace("4.00000000 0.00000000 0.00000000", "4 0 0"))
        result = get_band_gap(self.root)
        self.assertEqual(result["assessment"], "UNKNOWN")
        self.assertIn("xml_geometry_precision_insufficient", [f["code"] for f in result["findings"]])

    def test_nonfinite_xml_fermi_without_native_counterpart_is_failure(self):
        self.replace_xml(lambda text: text.replace('<i name="efermi">2.0000000000</i>', '<i name="efermi">NaN</i>'))
        source = self.root / "OUTCAR"
        source.write_text(re.sub(r"^.*(?:E-fermi|Fermi energy).*\n", "", source.read_text(), flags=re.M))
        result = get_fermi_level(self.root, fermi_source="xml")
        self.assertEqual(result["assessment"], "FAIL")
        self.assertIsNone(result["value"])
        self.assertIn("nonfinite_xml_fermi", [item["code"] for item in result["findings"]])
        json.dumps(result, allow_nan=False)

    def test_cli_keeps_valid_fermi_beside_malformed_spectrum(self):
        self.replace_xml(lambda text: re.sub(r"<eigenvalues>.*?</eigenvalues>", "", text))
        stream = StringIO()
        with redirect_stdout(stream):
            code = main(["vasp", "analyze-electronic", str(self.root), "--only", "fermi,gap", "--json"])
        result = json.loads(stream.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["metrics"]["fermi"]["value"], 2)
        self.assertEqual(result["metrics"]["gap"]["assessment"], "FAIL")
        self.assertIsNone(result["metrics"]["gap"]["value"])

    def test_incomplete_xml_band_array_retains_unknown_without_exception(self):
        self.replace_xml(lambda text: text.split('<eigenvalues>')[0] + '<eigenvalues>')
        result = get_band_gap(self.root)
        self.assertNotEqual(result["assessment"], "PASS")
        self.assertIsNone(result["value"])


if __name__ == "__main__":
    unittest.main()
