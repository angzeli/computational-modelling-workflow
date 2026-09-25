"""Shared input/result POSCAR dialect checks using invented H/He structures."""
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.inputs import parse_poscar
from cmw.periodic.vasp.results import inspect_result
from .result_fixtures import write_run


class SharedPoscarDialectTests(unittest.TestCase):
    def test_results_use_the_same_annotated_structure_and_zero_tail_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = write_run(Path(temporary).resolve(), flags=((True, False, True), (False, True, False)))
            for role in ("POSCAR", "CONTCAR"):
                path = run / role
                lines = path.read_text().splitlines()
                lines[9] += " H"
                lines[10] += " He"
                path.write_bytes(("\n".join(lines) + "\n\n0 -0.0 0D-4\n0 0 0\n").replace("\n", "\r\n").encode())
            evidence = inspect_result(run)
            for role in ("POSCAR", "CONTCAR"):
                expected = parse_poscar((run / role).read_bytes())
                expected.pop("raw_text")
                self.assertEqual(expected["status"], "valid")
                self.assertEqual(evidence["filesystem_inputs"][role], expected)
            self.assertEqual(evidence["endpoint"]["comparisons"]["endpoint_poscar"]["status"], "match")

    def test_result_inspection_does_not_underflow_nonzero_velocities_to_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = write_run(Path(temporary).resolve())
            for role in ("POSCAR", "CONTCAR"):
                path = run / role
                path.write_text(path.read_text() + "\n1e-999 0 0\n0 0 0\n")
            evidence = inspect_result(run)
            for role in ("POSCAR", "CONTCAR"):
                self.assertEqual(evidence["filesystem_inputs"][role]["status"], "unsupported")
