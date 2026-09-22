from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.core.preparation_publication import inspect_publication
from cmw.periodic.vasp.inputs import check_inputs, parse_incar
from cmw.periodic.vasp.preparation import PreparationError, compare_inputs, prepare
from cmw.structure.embedding import embed_molecule
from tests.periodic.vasp.test_inputs import bundle, potential, structure


class VaspPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.library = self.root / "library"
        self.library.mkdir()
        for element in ("H", "He", "Li"):
            directory = self.library / element
            directory.mkdir()
            (directory / "POTCAR").write_bytes(potential((element,)))
        self.source = self.root / "source.POSCAR"
        self.source.write_text(structure(selective=True))
        self.spec_path = self.root / "spec.json"
        self.output = self.root / "inputs"
        self.data = {
            "schema_version": 1, "calculation": "static", "structure": "source.POSCAR",
            "profile": {"name": "caller-synthetic-profile", "incar": {
                "GGA": "PE", "ENCUT": 350, "PREC": "Accurate", "ISPIN": 1,
                "ISMEAR": 0, "SIGMA": .05, "EDIFF": 1e-6, "NELM": 60,
                "ALGO": "Normal", "ISTART": 0, "ICHARG": 2,
            }, "kpoints": {"mode": "Monkhorst-Pack", "mesh": [2, 3, 4], "shift": [.5, 0, -.25]},
                "potentials": {"root": "library", "variants": {"H": "H", "He": "He", "Li": "Li"},
                               "requirements": {"family": "FAKE"}}},
        }

    def save(self, data=None):
        self.spec_path.write_text(json.dumps(self.data if data is None else data))

    def call(self, *, output=None, record_directory="case", dry_run=False, scratch_root=None):
        return prepare(self.spec_path, output=output or self.output,
                       scratch_root=scratch_root or self.scratch,
                       record_directory=record_directory, dry_run=dry_run)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_deterministic_publication_exact_four_files_and_source_preservation(self):
        self.save()
        sources = self.snapshot()
        with patch("subprocess.Popen", side_effect=AssertionError("Preparation must not execute anything")):
            first = self.call()
            second_path = self.root / "inputs-second"
            second = self.call(output=second_path, record_directory="second")
        first_files = {path.name: path.read_bytes() for path in self.output.iterdir()}
        second_files = {path.name: path.read_bytes() for path in second_path.iterdir()}
        self.assertEqual(first_files, second_files)
        self.assertEqual(set(first_files), {"INCAR", "KPOINTS", "POSCAR", "POTCAR"})
        self.assertEqual(first_files["POSCAR"], sources["source.POSCAR"])
        self.assertEqual(first["preparation_content_id"], second["preparation_content_id"])
        self.assertNotEqual(first["preparation_id"], second["preparation_id"])
        self.assertEqual(first["structure"]["selective_dynamics"], [[True, False, True]] * 3)
        self.assertEqual(first["structure"]["species"], ["H", "He", "Li"])
        self.assertEqual(first["atom_mapping"], "identity; source POSCAR bytes preserved")
        self.assertEqual(first["effective_inputs"]["status"], "unobserved")
        self.assertEqual(first["publication"]["state"], "complete")
        record = self.scratch / "case" / "preparation.json"
        self.assertEqual(json.loads(record.read_text()), first)
        self.assertEqual({path.name for path in record.parent.iterdir()}, {"preparation.json"})
        self.assertEqual(check_inputs(self.output, record)["exit_code"], 0)
        for name, data in sources.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertFalse(any(path.is_symlink() for path in self.output.iterdir()))

    def test_repeated_species_blocks_and_constraints_are_not_regrouped(self):
        for blocks in (3, 12, 18):
            species = ("H", "He", "Li") * (blocks // 3)
            source = structure(species, selective=True).encode()
            self.source.write_bytes(source)
            self.save()
            output = self.root / f"blocks-{blocks}"
            result = self.call(output=output, record_directory=f"blocks-{blocks}")
            self.assertEqual((output / "POSCAR").read_bytes(), source)
            self.assertEqual(result["structure"]["species"], list(species))
            self.assertEqual(result["selected_potentials"]["species"], list(species))
            self.assertEqual((output / "POTCAR").read_bytes(), potential(species))
            self.assertEqual(result["structure"]["selective_dynamics"], [[True, False, True]] * blocks)

    def test_preview_is_write_free_and_changes_are_resolved_again(self):
        self.save()
        before = self.snapshot()
        preview = self.call(dry_run=True)
        self.assertEqual(preview["publication"]["state"], "preview")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])
        selected = self.library / "H" / "POTCAR"
        selected.write_bytes(selected.read_bytes().replace(b"synthetic", b"changed synthetic"))
        actual = self.call()
        self.assertNotEqual(preview["prepared_inputs"]["POTCAR"], actual["prepared_inputs"]["POTCAR"])
        self.assertIn(b"changed synthetic", (self.output / "POTCAR").read_bytes())

    def test_static_type_origins_and_explicit_overrides(self):
        self.data["overrides"] = {"incar": {"NELM": 120}, "kpoints": {"mesh": [4, 5, 6]}}
        self.save()
        result = self.call(dry_run=True)
        settings = result["resolved_settings"]["incar"]
        self.assertEqual((settings["NSW"], settings["IBRION"], settings["NELM"]), (0, -1, 120))
        origins = result["settings_origins"]
        self.assertEqual(origins["incar"]["NSW"], "type_invariant")
        self.assertEqual(origins["incar"]["ENCUT"], "profile:caller-synthetic-profile")
        self.assertEqual(origins["incar"]["NELM"], "case_override")
        self.assertEqual(origins["kpoints"]["mesh"], "case_override")
        self.assertEqual(result["resolved_settings"]["kpoints"]["mesh"], [4, 5, 6])

    def test_static_conflicts_and_missing_material_profile_context_are_refused(self):
        for key, value in (("NSW", 1), ("IBRION", 2)):
            data = copy.deepcopy(self.data)
            data["profile"]["incar"][key] = value
            self.save(data)
            with self.assertRaisesRegex(PreparationError, "invariant conflicts"):
                self.call(dry_run=True)
        for key in tuple(self.data["profile"]["incar"]):
            data = copy.deepcopy(self.data)
            del data["profile"]["incar"][key]
            self.save(data)
            with self.subTest(missing=key), self.assertRaisesRegex(PreparationError, "Missing explicit scientific context"):
                self.call(dry_run=True)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_fixed_cell_conventions_require_explicit_positive_potim(self):
        self.data["calculation"] = "fixed-cell-relaxation"
        self.data["profile"]["incar"].update(IBRION=2, NSW=20, EDIFFG=-.03, POTIM=.4)
        for ediffg, convention in ((-.03, "force (eV/Angstrom)"), (.0001, "energy (eV)")):
            self.data["profile"]["incar"]["EDIFFG"] = ediffg
            self.save()
            result = self.call(dry_run=True)
            self.assertEqual(result["resolved_settings"]["incar"]["ISIF"], 2)
            self.assertEqual(result["stopping_convention"], convention)
        for key, value in (("ISIF", 3), ("NSW", 0), ("IBRION", -1), ("EDIFFG", 0), ("POTIM", 0)):
            data = copy.deepcopy(self.data)
            data["profile"]["incar"][key] = value
            self.save(data)
            with self.subTest(key=key), self.assertRaises(PreparationError):
                self.call(dry_run=True)
        del self.data["profile"]["incar"]["POTIM"]
        self.save()
        with self.assertRaisesRegex(PreparationError, "explicit positive POTIM"):
            self.call(dry_run=True)

    def test_runtime_stays_out_of_scientific_inputs(self):
        self.data["runtime"] = {"mpi_ranks": 8, "threads_per_rank": 1, "cpus": 8,
                                "incar": {"NCORE": 4, "KPAR": 1}}
        self.save()
        result = self.call()
        settings = parse_incar((self.output / "INCAR").read_text())["settings"]
        self.assertNotIn("NCORE", settings)
        self.assertNotIn("KPAR", settings)
        self.assertEqual(result["expected_runtime"], self.data["runtime"])
        self.assertEqual(result["effective_inputs"], {"status": "unobserved", "evidence": None})
        self.assertEqual(result["settings_origins"]["runtime"]["incar"]["NCORE"], "declared_runtime_overlay")
        for runtime in ({"incar": {"ENCUT": 500}}, {"mpi_ranks": 8, "incar": {"NCORE": 3}},
                        {"mpi_ranks": 8, "threads_per_rank": 2, "cpus": 8}):
            data = copy.deepcopy(self.data)
            data["runtime"] = runtime
            self.save(data)
            with self.assertRaises(PreparationError):
                self.call(output=self.root / "other", record_directory="other", dry_run=True)

    def test_fresh_initialization_unknowns_duplicates_and_out_of_scope_modes(self):
        for key, value in (("ISTART", 1), ("ICHARG", 11), ("LHFCALC", True),
                           ("LSORBIT", True), ("LNONCOLLINEAR", True), ("METAGGA", "SCAN"),
                           ("UNSUPPORTED_TAG", 1), ("NCORE", 2)):
            data = copy.deepcopy(self.data)
            data["profile"]["incar"][key] = value
            self.save(data)
            with self.subTest(key=key), self.assertRaises(PreparationError):
                self.call(dry_run=True)
        data = copy.deepcopy(self.data)
        data["profile"]["incar"]["encut"] = 350
        self.save(data)
        with self.assertRaisesRegex(PreparationError, "case-insensitive"):
            self.call(dry_run=True)
        self.spec_path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(PreparationError, "Duplicate JSON"):
            self.call(dry_run=True)

    def test_external_profile_paths_are_relative_to_the_defining_file(self):
        profiles = self.root / "profiles"
        profiles.mkdir()
        profile = copy.deepcopy(self.data["profile"])
        profile["potentials"]["root"] = "../library"
        (profiles / "profile.json").write_text(json.dumps(profile))
        self.data["profile"] = "profiles/profile.json"
        self.save()
        with patch("pathlib.Path.cwd", return_value=self.root.parent):
            result = self.call(dry_run=True)
        self.assertEqual(result["resolved_settings"]["potentials"]["root"], str(self.library))
        profile_source = next(item for item in result["sources"] if item["role"] == "profile")
        self.assertEqual(profile_source["recorded_location"], "profiles/profile.json")
        self.assertEqual(profile_source["resolved_location"], str(profiles / "profile.json"))
        self.data["overrides"] = {"potentials": {"root": "library"}}
        self.save()
        self.assertEqual(self.call(dry_run=True)["resolved_settings"]["potentials"]["root"], str(self.library))

    def test_named_values_and_required_coupled_context_are_explicit(self):
        for changes in ({"GGA": "NOT_A_METHOD"}, {"PREC": "UNKNOWN"}, {"ALGO": "UNKNOWN"},
                        {"ISMEAR": 999}, {"NELECT": 0}, {"ISPIN": 2}, {"LDAU": True},
                        {"LDIPOL": True}, {"LDIPOL": True, "IDIPOL": 0, "DIPOL": [.5, .5, .5]},
                        {"LDAU": True, "LDAUTYPE": 3, "LDAUL": [-1, 0, 0], "LDAUU": [0, 1, 2], "LDAUJ": [0, 0, 0]}):
            data = copy.deepcopy(self.data)
            data["profile"]["incar"].update(changes)
            self.save(data)
            with self.subTest(changes=changes), self.assertRaises(PreparationError):
                self.call(dry_run=True)
        self.data["profile"]["incar"].update(
            ISPIN=2, MAGMOM=[1, -1, 0], LDAU=True, LDAUTYPE=2,
            LDAUL=[-1, 0, 0], LDAUU=[0, 1, 2], LDAUJ=[0, 0, 0],
            LDIPOL=True, IDIPOL=3, DIPOL=[.5, .5, .5])
        self.save()
        self.assertEqual(self.call(dry_run=True)["validation"]["status"], "valid")
        self.data["profile"]["incar"]["MAGMOM"] = [1, 1]
        self.save()
        with self.assertRaisesRegex(PreparationError, "Candidate input validation"):
            self.call(dry_run=True)

    def test_cli_preview_publication_and_error_json(self):
        self.save()
        arguments = ["vasp", "prepare", "--spec", str(self.spec_path), "--output", str(self.output),
                     "--scratch-root", str(self.scratch), "--record-directory", "case", "--json"]
        for preview in (True, False):
            stdout = io.StringIO()
            with redirect_stdout(stdout), patch("subprocess.Popen", side_effect=AssertionError("No engine execution")):
                code = main(arguments + (["--dry-run"] if preview else []))
            result = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["publication"]["state"], "preview" if preview else "complete")
            self.assertEqual(result["effective_inputs"]["status"], "unobserved")
            if preview:
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.scratch.iterdir()), [])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(arguments)
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "error")
        self.assertEqual({path.name for path in self.output.iterdir()}, {"INCAR", "KPOINTS", "POSCAR", "POTCAR"})

    def test_baseline_record_can_certify_only_nelm_changed_but_bundle_cannot_runtime(self):
        self.save()
        self.call()
        self.data["overrides"] = {"incar": {"NELM": 120}}
        self.data["baseline"] = {"record": "scratch/case/preparation.json"}
        self.save()
        result = self.call(output=self.root / "next", record_directory="next", dry_run=True)
        self.assertEqual(result["differences"]["only_incar_tags_changed"], ["NELM"])
        self.assertTrue(result["differences"]["complete"])
        self.data["baseline"] = "inputs"
        self.save()
        result = self.call(output=self.root / "next", record_directory="next", dry_run=True)
        self.assertIsNone(result["differences"]["only_incar_tags_changed"])
        self.assertFalse(result["differences"]["complete"])
        self.assertTrue(result["differences"]["prepared_input_comparison_complete"])

    def test_baseline_change_after_record_inspection_invalidates_complete_comparison(self):
        self.save()
        self.call()
        self.data["overrides"] = {"incar": {"NELM": 120}}
        self.data["baseline"] = {"record": "scratch/case/preparation.json"}
        self.save()
        baseline_incar = self.output / "INCAR"

        def inspect_then_change(path, **options):
            observed = inspect_publication(path, **options)
            self.assertEqual(observed["status"], "valid")
            # This remains parseable and changes no INCAR settings. A prior
            # successful record check cannot certify these later-read bytes.
            baseline_incar.write_bytes(baseline_incar.read_bytes() + b"# changed after inspection\n")
            return observed

        with patch("cmw.periodic.vasp.preparation.inspect_publication", side_effect=inspect_then_change):
            result = self.call(output=self.root / "next", record_directory="next", dry_run=True)
        difference = result["differences"]
        self.assertFalse(difference["complete"])
        self.assertFalse(difference["prepared_input_comparison_complete"])
        self.assertIsNone(difference["only_incar_tags_changed"])
        self.assertIn("missing_evidence", [item["category"] for item in difference["changes"]])
        self.assertFalse((self.root / "next").exists())
        self.assertFalse((self.scratch / "next").exists())

    def test_compatible_baseline_record_missing_locator_is_structured_error(self):
        self.save()
        self.call()
        record_path = self.scratch / "case" / "preparation.json"
        record = json.loads(record_path.read_text())
        del record["publication"]["input_path"]
        record_path.write_text(json.dumps(record))
        self.data["baseline"] = {"record": "scratch/case/preparation.json"}
        self.save()
        with self.assertRaisesRegex(PreparationError, "input_path"):
            self.call(output=self.root / "next", record_directory="next", dry_run=True)
        self.assertFalse((self.root / "next").exists())
        self.assertFalse((self.scratch / "next").exists())

    def test_comparisons_classify_material_changes_and_missing_evidence(self):
        baseline = bundle("NSW=0; IBRION=-1; ISTART=0; ICHARG=2; NELM=60")
        baseline["POSCAR"] = structure(selective=True).encode()
        target = dict(baseline, INCAR=baseline["INCAR"].replace(b"NELM=60", b"NELM=120"))
        changed = compare_inputs(baseline, target, baseline_runtime={}, prepared_runtime={})
        self.assertEqual(changed["only_incar_tags_changed"], ["NELM"])
        for filename, replacement, category in (
            ("POSCAR", baseline["POSCAR"].replace(b"0.50000000", b"0.51000000"), "coordinates"),
            ("POSCAR", baseline["POSCAR"].replace(b"2 0 0", b"2.1 0 0"), "cell"),
            ("POSCAR", baseline["POSCAR"].replace(b"T F T", b"F F T", 1), "constraints"),
            ("POTCAR", baseline["POTCAR"].replace(b"synthetic", b"different synthetic", 1), "potential_content"),
            ("KPOINTS", baseline["KPOINTS"].replace(b"2 3 4", b"3 3 4"), "kpoint_sampling"),
        ):
            actual = dict(target)
            actual[filename] = replacement
            difference = compare_inputs(baseline, actual, baseline_runtime={}, prepared_runtime={})
            with self.subTest(category=category):
                self.assertIn(category, [item["category"] for item in difference["changes"]])
                self.assertIsNone(difference["only_incar_tags_changed"])
        for missing in (dict(target, INCAR=target["INCAR"] + b"; NEW_TAG=1"),
                        {name: data for name, data in target.items() if name != "POTCAR"}):
            difference = compare_inputs(baseline, missing, baseline_runtime={}, prepared_runtime={})
            self.assertFalse(difference["complete"])
            self.assertIsNone(difference["only_incar_tags_changed"])
            self.assertIn("missing_evidence", [item["category"] for item in difference["changes"]])

    def test_title_format_and_equivalent_coordinates_are_not_model_changes(self):
        baseline = bundle()
        original = baseline["POSCAR"]
        target = dict(baseline)
        header = original.replace(b"synthetic only", b"renamed").replace(b"Direct", b"Cartesian")
        target["POSCAR"] = b"\n".join(header.splitlines()[:8]) + b"\n0 0 0\n.5 0 0\n1 0 0\n"
        difference = compare_inputs(baseline, target, baseline_runtime={}, prepared_runtime={})
        self.assertTrue(difference["complete"])
        self.assertEqual({item["category"] for item in difference["changes"]}, {"title_comment_format"})

    def test_embedding_record_lineage_is_referenced_and_content_must_match(self):
        xyz = self.root / "molecule.xyz"
        xyz.write_text("3\nsynthetic molecule\nH 0 0 0\nHe 1 1 0\nH 2 0 1\n")
        embedded = self.root / "embedded"
        embedded.mkdir()
        poscar = embedded / "POSCAR"
        embed_molecule(xyz, poscar, scratch_root=self.scratch, record_directory="embedding")
        self.data["structure"] = "embedded/POSCAR"
        self.data["source_record"] = "scratch/embedding/preparation.json"
        self.data["profile"]["potentials"]["variants"] = {"H": "H", "He": "He"}
        self.save()
        result = self.call()
        self.assertEqual((self.output / "POSCAR").read_bytes(), poscar.read_bytes())
        self.assertIn("embedding_record", [item["role"] for item in result["sources"]])
        poscar.write_bytes(b"changed title\n" + poscar.read_bytes().split(b"\n", 1)[1])
        with self.assertRaisesRegex(PreparationError, "matching molecular embedding"):
            self.call(output=self.root / "other", record_directory="other", dry_run=True)

    def test_missing_scratch_existing_destinations_and_source_aliases_are_safe(self):
        self.save()
        original = self.snapshot()
        with self.assertRaises(ValueError):
            self.call(scratch_root=self.root / "missing", dry_run=True)
        self.assertEqual(self.snapshot(), original)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        with self.assertRaises(ValueError):
            self.call()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.scratch.iterdir()), [])
        for output in (self.source, self.library / "new-inputs", self.spec_path):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.call(output=output)
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(list(self.scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
