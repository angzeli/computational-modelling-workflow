from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from cmw.cli import main
from cmw.periodic.vasp.inputs import (
    check_input_bytes, check_inputs, parse_incar, parse_kpoints, parse_poscar,
)
from cmw.periodic.vasp.potentials import inspect_potcar


def structure(species=("H", "He", "Li"), counts=None, selective=False):
    counts = counts or [1] * len(species)
    flags = " T F T" if selective else ""
    return ("synthetic only\n1\n2 0 0\n0 3 0\n0 0 4\n"
            + " ".join(species) + "\n" + " ".join(map(str, counts)) + "\n"
            + ("Selective dynamics\n" if selective else "") + "Direct\n"
            + "".join(f"{index / (sum(counts) + 1):.8f} 0 0{flags}\n" for index in range(sum(counts))))


def potential(species=("H", "He", "Li")):
    # Invented metadata, never extracted from a licensed potential dataset.
    return "".join(f"TITEL = FAKE {symbol} 01Jan2099\nVRHFIN = {symbol}: synthetic\nEnd of Dataset\n" for symbol in species).encode()


def bundle(incar=None, species=("H", "He", "Li"), counts=None):
    return {
        "POSCAR": structure(species, counts).encode(),
        "POTCAR": potential(species),
        "INCAR": (incar or "NSW=0; IBRION=-1; ISTART=0; ICHARG=2\nENCUT=400; EDIFF=1e-6; ISPIN=1\n").encode(),
        "KPOINTS": b"synthetic mesh\n0\nGamma\n2 3 4\n0 0 0\n",
    }


class InputParserTests(unittest.TestCase):
    def assert_code(self, result, code):
        self.assertIn(code, [item["code"] for item in result["findings"]], result)

    def test_raw_text_blocks_and_atom_sequence_preserved(self):
        for blocks in (3, 12, 18):
            species = ("H", "He", "Li") * (blocks // 3)
            text = structure(species, selective=True)
            parsed = parse_poscar(text.encode())
            self.assertEqual(parsed["status"], "valid")
            self.assertEqual(parsed["raw_text"], text)
            self.assertEqual(parsed["species"], list(species))
            self.assertEqual(parsed["counts"], [1] * blocks)
            self.assertEqual(parsed["atom_species"], list(species))
            self.assertEqual(parsed["selective_dynamics"], [[True, False, True]] * blocks)
            self.assertEqual(check_input_bytes(bundle(species=species))["exit_code"], 0)

    def test_scaling_and_modes(self):
        text = structure()
        cases = [("2", [[4, 0, 0], [0, 6, 0], [0, 0, 8]]),
                 ("-192", [[4, 0, 0], [0, 6, 0], [0, 0, 8]]),
                 ("2 3 4", [[4, 0, 0], [0, 9, 0], [0, 0, 16]])]
        for scale, cell in cases:
            with self.subTest(scale=scale):
                parsed = parse_poscar(text.replace("\n1\n", f"\n{scale}\n").replace("Direct", "Cartesian"))
                self.assertEqual(parsed["exit_code"], 0)
                self.assertEqual(parsed["cell"], cell)
                self.assertEqual(parsed["coordinate_mode"], "Cartesian")

    def test_malformed_cells_counts_coordinates_flags(self):
        text = structure()
        invalid = [text.replace("2 0 0", "nan 0 0"),
                   text.replace("0 3 0", "4 0 0"),
                   text.replace("\n1\n", "\n0\n"),
                   text.replace("\n1\n", "\n1 -2 3\n"),
                   text.replace("1 1 1", "1 1"),
                   text.replace("1 1 1", "1 -1 1"),
                   "\n".join(text.splitlines()[:-1]),
                   text.replace("0.50000000", "inf"),
                   structure(selective=True).replace("T F T", "T X T", 1),
                   structure(selective=True).replace("T F T", "T F", 1)]
        for value in invalid:
            with self.subTest(text=value):
                self.assertEqual(parse_poscar(value)["status"], "invalid")

    def test_direct_cartesian_equivalence_preserves_raw_representation(self):
        for scale in ("2", "-192", "2 3 4"):
            header = f"synthetic\n{scale}\n2 0 0\n.5 3 0\n.1 .2 4\nH\n1\n"
            direct = parse_poscar(header + "Direct\n.2 .3 .4\n")
            cartesian = parse_poscar(header + "Cartesian\n.59 .98 1.6\n")
            self.assertEqual(direct["status"], "valid")
            self.assertEqual(cartesian["status"], "valid")
            self.assertEqual(direct["cell"], cartesian["cell"])
            for actual, expected in zip(direct["cartesian_coordinates"][0], cartesian["cartesian_coordinates"][0]):
                self.assertAlmostEqual(actual, expected)
            self.assertEqual(direct["coordinates"], [[.2, .3, .4]])
            self.assertEqual(cartesian["coordinates"], [[.59, .98, 1.6]])

    def test_additional_sections_and_implicit_species_are_unsupported(self):
        self.assert_code(parse_poscar(structure() + "\nCartesian\n0 0 0\n"), "poscar.extra_sections")
        self.assertEqual(parse_poscar(structure() + "0 0 0\n")["status"], "unsupported")
        legacy = structure().replace("H He Li\n", "")
        self.assertEqual(parse_poscar(legacy)["status"], "unsupported")

    def test_incar_comments_case_booleans_arrays_duplicates(self):
        parsed = parse_incar('encut=4D2; LREAL=.FALSE. # comment\n'
                             'MAGMOM=2*1.5 -2; ISPIN=2 ! comment\n'
                             'ENCUT=400.0\nSYSTEM="static; # example"\nLDAUL=3*-1\n')
        self.assertEqual(parsed["status"], "valid")
        self.assertEqual(parsed["settings"], {"ENCUT": 400.0, "LREAL": False,
                         "MAGMOM": [1.5, 1.5, -2], "ISPIN": 2,
                         "SYSTEM": "static; # example", "LDAUL": [-1, -1, -1]})
        self.assert_code(parsed, "incar.duplicate_identical")
        conflict = parse_incar("ENCUT=400; encut=500")
        self.assertEqual(conflict["status"], "invalid")
        self.assert_code(conflict, "incar.duplicate_conflict")

    def test_incar_unknown_retained_unassessed(self):
        parsed = parse_incar("UNRECOGNIZED_FUTURE_TAG = 3*2")
        self.assertEqual(parsed["settings"]["UNRECOGNIZED_FUTURE_TAG"], [2, 2, 2])
        self.assertEqual(parsed["unknown_tags"], ["UNRECOGNIZED_FUTURE_TAG"])
        self.assertEqual(parsed["status"], "valid")
        self.assertEqual(parsed["findings"][0]["severity"], "unassessed")

    def test_incar_numeric_named_settings_remain_identifiers(self):
        parsed = parse_incar("GGA=91; ALGO=normal; PREC=accurate")
        self.assertEqual(parsed["status"], "valid")
        self.assertEqual(parsed["settings"], {"GGA": "91", "ALGO": "NORMAL", "PREC": "ACCURATE"})
        self.assertEqual(check_input_bytes(bundle("NSW=0; ISTART=0; ICHARG=2; GGA=91"))["exit_code"], 0)

    def test_incar_continuations_are_unsupported_not_malformed(self):
        text = "NSW=0\nMAGMOM=1 \\\n 2 \\ # continued\n# comment\n 3\nISTART=0; ICHARG=2\n"
        parsed = parse_incar(text)
        self.assertEqual(parsed["status"], "unsupported")
        self.assertEqual(parsed["settings"], {"NSW": 0, "ISTART": 0, "ICHARG": 2})
        self.assertEqual([item["code"] for item in parsed["findings"]], ["incar.continuation"])
        self.assertEqual(check_input_bytes(bundle(text))["exit_code"], 2)
        self.assertEqual(parse_incar('SYSTEM="literal \\"\nNSW=0')["status"], "valid")

    def test_bad_incar_values_and_ambiguity_are_invalid(self):
        for text in ("ENCUT=nan", "NELM=2.5", "LWAVE=maybe", "DIPOL=1 nope 0",
                     "MAGMOM=0*1", "EDIFF=", "ENCUT=300 ENCUT=400", "SYSTEM=\"unfinished"):
            with self.subTest(text=text):
                self.assertEqual(parse_incar(text)["status"], "invalid")

    def test_regular_meshes_and_shift(self):
        for mode in ("Gamma", "Monkhorst-Pack"):
            parsed = parse_kpoints(f"test\n0\n{mode}\n2 3 4\n0.5 -0.25 0\n")
            self.assertEqual(parsed["status"], "valid")
            self.assertEqual(parsed["mode"], mode)
            self.assertEqual(parsed["mesh"], [2, 3, 4])
            self.assertEqual(parsed["shift"], [.5, -.25, 0])
        self.assertEqual(parse_kpoints("mesh\n0\nG\n1 1 1\n")["shift"], [0, 0, 0])

    def test_mesh_invalid_and_unsupported_differ(self):
        for mesh in ("0 1 1", "1.5 1 1", "-1 1 1", "1 2"):
            self.assertEqual(parse_kpoints(f"test\n0\nGamma\n{mesh}\n")["exit_code"], 1)
        self.assertEqual(parse_kpoints("test\n0\nGamma\n1 1 1\nnan 0 0")["status"], "invalid")
        for text in ("line\n20\nLine-mode\nReciprocal\n", "explicit\n1\nReciprocal\n0 0 0 1\n", "auto\n0\nAuto\n10\n"):
            self.assertEqual(parse_kpoints(text)["exit_code"], 2)

    def test_static_and_fixed_cell_relaxation(self):
        self.assertEqual(check_input_bytes(bundle())["exit_code"], 0)
        for ibrion in (1, 2, 3):
            result = check_input_bytes(bundle(f"NSW=10; IBRION={ibrion}; ISIF=2; EDIFFG=-.03; ISTART=0; ICHARG=2"))
            self.assertEqual(result["exit_code"], 0, result)
        result = check_input_bytes(bundle("NSW=10; IBRION=-1"))
        self.assert_code(result, "cross.ionic_mode")
        self.assertEqual(result["exit_code"], 1)
        result = check_input_bytes(bundle("NSW=10; IBRION=2; ISIF=3"))
        self.assertEqual(result["exit_code"], 2)

    def test_title_does_not_define_mode(self):
        result = check_input_bytes(bundle("SYSTEM=relaxation; NSW=0; IBRION=-1; ISTART=0; ICHARG=2"))
        self.assertEqual(result["exit_code"], 0)
        result = check_input_bytes(bundle("SYSTEM=static"))
        self.assert_code(result, "incar.calculation_mode_unassessed")
        self.assert_code(result, "incar.initialization_unassessed")

    def test_potential_full_sequence_count(self):
        for species in (("H", "Li", "He"), ("H", "He"), ("H", "He", "Li", "Li")):
            files = bundle()
            files["POTCAR"] = potential(species)
            self.assertEqual(check_input_bytes(files)["exit_code"], 1)

    def test_array_lengths_use_atoms_or_all_blocks(self):
        files = bundle("NSW=0; IBRION=-1; MAGMOM=3*1; LDAUL=3*-1; LDAUU=3*0; LDAUJ=3*0", counts=[2, 1, 1])
        result = check_input_bytes(files)
        self.assert_code(result, "cross.magmom_length")
        files["INCAR"] = b"MAGMOM=4*1; LDAUU=2*0"
        self.assert_code(check_input_bytes(files), "cross.species_array_length")
        species = ("H", "He", "Li") * 4
        result = check_input_bytes(bundle("LDAUU=3*0", species=species))
        self.assert_code(result, "cross.species_array_length")
        result = check_input_bytes(bundle("MAGMOM=4*1; LDAUU=3*0", counts=[2, 1, 1]))
        self.assertEqual(result["exit_code"], 0)

    def test_noncollinear_and_restart_are_unsupported(self):
        for setting in ("LNONCOLLINEAR=T; MAGMOM=9*0", "LSORBIT=T", "ISTART=1", "ICHARG=11"):
            result = check_input_bytes(bundle(setting))
            self.assertEqual(result["exit_code"], 2, result)

    def test_exact_expected_selection_and_requirements(self):
        files = bundle()
        selected = inspect_potcar(files["POTCAR"])
        self.assertEqual(check_input_bytes(files, selected)["exit_code"], 0)
        selected["datasets"][0]["label"] = "H_fake"
        self.assert_code(check_input_bytes(files, selected), "selection.identity")
        result = check_input_bytes(files, {"variants": {"H": "H_fake"}})
        self.assert_code(result, "selection.requirements")
        result = check_input_bytes(files, {"library_release": "2099"})
        self.assertEqual(result["status"], "invalid")
        result = check_input_bytes(files)
        self.assert_code(result, "selection.unassessed")

    def test_missing_file_and_nonutf8_are_structured(self):
        files = bundle()
        del files["INCAR"]
        self.assert_code(check_input_bytes(files), "bundle.missing_input")
        files["INCAR"] = b"\xff"
        self.assert_code(check_input_bytes(files), "bundle.encoding")

    def test_findings_json_and_exit_consistency(self):
        for files in (bundle(), bundle("ISTART=1"), bundle("ENCUT=-1")):
            result = check_input_bytes(files)
            self.assertEqual(json.loads(json.dumps(result)), result)
            self.assertEqual(result["exit_code"], {"valid": 0, "invalid": 1, "unsupported": 2}[result["status"]])
            for item in result["findings"]:
                self.assertEqual(set(item), {"code", "severity", "scope", "message", "evidence"})

    def test_exact_four_files_no_sidecar_and_explicit_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inputs = root / "inputs"
            inputs.mkdir()
            files = bundle()
            for name, data in files.items():
                (inputs / name).write_bytes(data)
            before = {path.name: path.read_bytes() for path in inputs.iterdir()}
            result = check_inputs(inputs)
            self.assertEqual(result["exit_code"], 0)
            self.assert_code(result, "record.unassessed")
            record = root / "preparation.json"
            record.write_text(json.dumps({"schema_version": 1, "record_kind": "vasp-input-preparation", "publication": {
                "state": "complete", "kind": "directory", "input_path": str(inputs)},
                "prepared_inputs": result["identities"], "selected_potentials": inspect_potcar(files["POTCAR"])}))
            verified = check_inputs(inputs, record)
            self.assertEqual(verified["exit_code"], 0)
            self.assertNotIn("selection.unassessed", [item["code"] for item in verified["findings"]])
            self.assertEqual({path.name: path.read_bytes() for path in inputs.iterdir()}, before)
            self.assertEqual(set(before), {"INCAR", "KPOINTS", "POSCAR", "POTCAR"})
            (inputs / "INCAR").write_bytes(files["INCAR"] + b"# changed\n")
            changed = check_inputs(inputs, record)
            self.assert_code(changed, "PUBLICATION_CONTENT_MISMATCH")
            self.assertEqual(changed["exit_code"], 1)
            saved = json.loads(record.read_text())
            saved["publication"]["state"] = "intent"
            record.write_text(json.dumps(saved))
            self.assert_code(check_inputs(inputs, record), "PUBLICATION_INCOMPLETE")

    def test_cli_json_exit_codes_and_external_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inputs = root / "inputs"
            inputs.mkdir()
            for name, data in bundle().items():
                (inputs / name).write_bytes(data)
            record = root / "preparation.json"
            record.write_text(json.dumps({"schema_version": 1, "record_kind": "vasp-input-preparation", "publication": {
                "state": "complete", "kind": "directory", "input_path": str(inputs)},
                "prepared_inputs": check_inputs(inputs)["identities"]}))
            for expected, value in ((0, None), (2, "ISTART=1"), (1, "ENCUT=-1")):
                if value is not None:
                    (inputs / "INCAR").write_text(value)
                stdout, stderr = io.StringIO(), io.StringIO()
                arguments = ["vasp", "check-inputs", str(inputs), "--json"]
                if value is None:
                    arguments += ["--preparation-record", str(record)]
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(arguments)
                parsed = json.loads(stdout.getvalue())
                self.assertEqual(code, expected, stderr.getvalue())
                self.assertEqual(parsed["exit_code"], code)
                self.assertEqual({path.name for path in inputs.iterdir()}, set(bundle()))

    def test_malformed_record_and_expected_selection_are_structured(self):
        for selection in (["not an object"], {"datasets": ["not a dataset"]},
                          {"datasets": [], "library_release": "not an object"}):
            self.assert_code(check_input_bytes(bundle(), selection), "selection.specification")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inputs = root / "inputs"
            inputs.mkdir()
            for name, data in bundle().items():
                (inputs / name).write_bytes(data)
            record = root / "preparation.json"
            for value in ([], {"schema_version": 2}, {"schema_version": 1, "record_kind": "other"}):
                record.write_text(json.dumps(value))
                result = check_inputs(inputs, record)
                self.assert_code(result, "record.schema")
                self.assertEqual(result["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
