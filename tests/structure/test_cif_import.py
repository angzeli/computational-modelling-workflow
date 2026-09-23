from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.core import preparation_publication as publication
from cmw.periodic.vasp.inputs import check_inputs
from cmw.periodic.vasp.preparation import PreparationError, prepare
from cmw.structure.cif_import import import_cif
from tests.periodic.vasp.test_inputs import potential


CIF = """data_ordered
_cell_length_a 4
_cell_length_b 5
_cell_length_c 6
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_space_group_name_H-M_alt 'P 1'
loop_
_space_group_symop_operation_xyz
'x,y,z'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
H1 H .1 .2 .3 1
He1 He .4 .5 .6 1
H2 H .7 .8 .9 1
"""


class CifImportPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.inputs = self.root / "repository-inputs"
        self.scratch = self.root / "scratch"
        self.inputs.mkdir()
        self.scratch.mkdir()
        self.source = self.root / "ordered.cif"
        self.source.write_text(CIF)
        self.original = self.source.read_bytes()
        self.output = self.inputs / "POSCAR"
        self.record = self.scratch / "import" / "preparation.json"
        self.jobs_state = self.root / "unused-jobs-state"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"CMW_JOBS_STATE": str(self.jobs_state)}).start()
        patch("subprocess.Popen", side_effect=AssertionError("Import/preparation must not execute anything")).start()

    def call(self, **options):
        return import_cif(self.source, self.output, scratch_root=self.scratch,
                          record_directory="import", **options)

    def arguments(self, *extra):
        return ["structure", "import-cif", "--input", str(self.source),
                "--output", str(self.output), "--scratch-root", str(self.scratch),
                "--record-directory", "import", "--json", *extra]

    def assert_no_publication(self):
        self.assertEqual(list(self.inputs.iterdir()), [])
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse(self.jobs_state.exists())

    def test_canonical_preview_has_complete_provenance_and_no_writes(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(self.arguments("--dry-run")), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["record_kind"], "periodic-structure-import")
        self.assertEqual(report["downstream_use"], "periodic_structure_import")
        self.assertEqual(report["structure"]["selected_block"], "ordered")
        self.assertEqual(report["structure"]["source_sha256"], publication.content_identity(self.original)["sha256"])
        self.assertEqual(report["effective_inputs"]["status"], "unobserved")
        self.assertEqual(report["validation"]["status"], "valid")
        self.assert_no_publication()

    def test_publication_writes_only_poscar_and_scratch_record(self):
        from cmw.structure.cif import read_ordered_cif

        expected = read_ordered_cif(self.original)
        with patch("cmw.structure.cif.read_ordered_cif", wraps=read_ordered_cif) as parser:
            report = self.call()
        parser.assert_called_once_with(self.original, block=None)
        self.assertEqual(self.output.read_bytes(), expected.poscar)
        self.assertEqual(report["structure"], expected.structure.to_dict())
        self.assertEqual(report["conversion"], expected.metadata)
        self.assertEqual(report["sources"][0]["sha256"], publication.content_identity(self.original)["sha256"])
        self.assertEqual(report["sources"][0]["size_bytes"], len(self.original))
        self.assertEqual(json.loads(self.record.read_text()), report)
        self.assertEqual(report["publication"]["state"], "complete")
        self.assertEqual({path.name for path in self.inputs.iterdir()}, {"POSCAR"})
        self.assertEqual({path.name for path in self.record.parent.iterdir()}, {"preparation.json"})
        self.assertEqual(publication.inspect_publication(self.record)["status"], "valid")
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(self.jobs_state.exists())

    def test_preview_identity_ignores_destination_and_rechecks_changed_source(self):
        first = self.call(dry_run=True)
        elsewhere = import_cif(self.source, self.inputs / "another-poscar", scratch_root=self.scratch,
                               record_directory="another-import", dry_run=True)
        self.assertEqual(first["preparation_content_id"], elsewhere["preparation_content_id"])
        self.assert_no_publication()
        self.source.write_text(CIF.replace("_cell_length_a 4", "_cell_length_a 7"))
        actual = self.call()
        self.assertNotEqual(first["preparation_content_id"], actual["preparation_content_id"])
        self.assertNotEqual(first["prepared_inputs"], actual["prepared_inputs"])
        self.assertEqual(actual["structure"]["cell"][0][0], 7.)

    def test_multiblock_error_exposes_choices_and_explicit_selection_works(self):
        self.source.write_text(CIF + CIF.replace("data_ordered", "data_second"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(self.arguments()), 2)
        report = json.loads(output.getvalue())
        self.assertEqual([item["name"] for item in report["details"]["available_blocks"]], ["ordered", "second"])
        self.assertTrue(all(item["structural_candidate"] for item in report["details"]["available_blocks"]))
        output = io.StringIO()
        arguments = self.arguments()
        arguments.remove("--json")
        with redirect_stdout(output):
            self.assertEqual(main(arguments), 2)
        self.assertIn("Available blocks: ordered, second", output.getvalue())
        self.assert_no_publication()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(self.arguments("--block", "second", "--dry-run")), 0)
        self.assertEqual(json.loads(output.getvalue())["resolved_settings"]["block"], "second")
        self.assert_no_publication()

    def test_rejected_cif_does_not_create_outputs(self):
        self.source.write_text(CIF.replace("H1 H .1 .2 .3 1", "H1 H .1 .2 .3 .5"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(self.arguments()), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "unsupported_cif")
        self.assert_no_publication()

    def test_single_structural_block_is_resolved_among_metadata_blocks(self):
        self.source.write_text("data_metadata\n_audit_creation_method 'synthetic fixture'\n" + CIF)
        report = self.call(dry_run=True)
        self.assertEqual(report["requested_settings"]["block"], None)
        self.assertEqual(report["resolved_settings"]["block"], "ordered")
        self.assert_no_publication()

    def test_existing_destinations_and_missing_scratch_are_refused(self):
        self.output.write_bytes(b"keep existing output")
        with self.assertRaises(ValueError):
            self.call()
        self.assertEqual(self.output.read_bytes(), b"keep existing output")
        self.output.unlink()
        self.record.parent.mkdir()
        with self.assertRaises(ValueError):
            self.call()
        self.assertEqual(list(self.record.parent.iterdir()), [])
        self.record.parent.rmdir()
        with self.assertRaises(ValueError):
            import_cif(self.source, self.output, scratch_root=self.root / "absent", record_directory="import")
        self.assert_no_publication()

    def test_source_alias_and_symlink_are_refused(self):
        with self.assertRaises(ValueError):
            import_cif(self.source, self.source, scratch_root=self.scratch, record_directory="import")
        alias = self.root / "alias.cif"
        alias.symlink_to(self.source)
        with self.assertRaises(ValueError):
            import_cif(alias, self.output, scratch_root=self.scratch, record_directory="import")
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assert_no_publication()

    def test_staging_failure_keeps_source_and_records_failed_intent(self):
        with patch.object(publication, "_write_candidate", side_effect=OSError("synthetic staging failure")):
            with self.assertRaises(publication.PublicationError) as caught:
                self.call()
        self.assertFalse(caught.exception.incomplete)
        self.assertEqual(list(self.inputs.iterdir()), [])
        self.assertEqual(json.loads(self.record.read_text())["publication"]["state"], "failed")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_finalization_failure_surfaces_incomplete_and_never_claims_success(self):
        original = publication._write_record

        def fail_complete(path, record):
            if record["publication"]["state"] == "complete":
                raise OSError("synthetic finalization failure")
            original(path, record)

        output = io.StringIO()
        with patch.object(publication, "_write_record", side_effect=fail_complete), redirect_stdout(output):
            self.assertEqual(main(self.arguments()), 2)
        failure = json.loads(output.getvalue())
        self.assertTrue(failure["incomplete"])
        self.assertEqual(failure["record_path"], str(self.record))
        self.assertTrue(self.output.is_file())
        self.assertEqual(json.loads(self.record.read_text())["publication"]["state"], "incomplete")
        self.assertEqual(publication.inspect_publication(self.record)["status"], "invalid")

    def preparation_spec(self):
        library = self.root / "synthetic-library"
        library.mkdir()
        for element in ("H", "He"):
            directory = library / element
            directory.mkdir()
            (directory / "POTCAR").write_bytes(potential((element,)))
        data = {"schema_version": 1, "calculation": "static",
                "structure": "repository-inputs/POSCAR", "source_record": "scratch/import/preparation.json",
                "profile": {"name": "caller-synthetic-profile", "incar": {
                    "GGA": "PE", "ENCUT": 350, "PREC": "Normal", "ISPIN": 1,
                    "ISMEAR": 0, "SIGMA": .05, "EDIFF": 1e-6, "NELM": 60,
                    "ALGO": "Normal", "ISTART": 0, "ICHARG": 2,
                }, "kpoints": {"mode": "Gamma", "mesh": [1, 1, 1], "shift": [0, 0, 0]},
                    "potentials": {"root": "synthetic-library", "variants": {"H": "H", "He": "He"},
                                   "requirements": {"family": "FAKE"}}}}
        spec = self.root / "spec.json"
        spec.write_text(json.dumps(data))
        return spec

    def test_vasp_preparation_preserves_imported_bytes_and_exact_lineage(self):
        imported = self.call()
        imported_bytes = self.output.read_bytes()
        record_bytes = self.record.read_bytes()
        spec = self.preparation_spec()
        output = self.root / "vasp-inputs"
        prepared = prepare(spec, output=output, scratch_root=self.scratch, record_directory="vasp")
        self.assertEqual({path.name for path in output.iterdir()}, {"INCAR", "KPOINTS", "POSCAR", "POTCAR"})
        self.assertEqual((output / "POSCAR").read_bytes(), imported_bytes)
        lineage = [item for item in prepared["sources"] if item["role"] == "periodic_structure_import"]
        self.assertEqual(len(lineage), 1)
        self.assertEqual(lineage[0]["sha256"], publication.content_identity(record_bytes)["sha256"])
        self.assertEqual(lineage[0]["resolved_location"], str(self.record))
        self.assertEqual(prepared["prepared_inputs"]["POSCAR"], imported["prepared_inputs"]["POSCAR"])
        self.assertEqual(check_inputs(output, self.scratch / "vasp" / "preparation.json")["exit_code"], 0)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.record.read_bytes(), record_bytes)
        self.assertFalse(self.jobs_state.exists())

    def test_vasp_lineage_refuses_changed_poscar_and_incomplete_import(self):
        self.call()
        spec = self.preparation_spec()
        original_poscar = self.output.read_bytes()
        # A comment change leaves a valid POSCAR but breaks its exact import identity.
        self.output.write_bytes(b"changed comment\n" + original_poscar.split(b"\n", 1)[1])
        output = self.root / "vasp-inputs"
        with self.assertRaises(PreparationError):
            prepare(spec, output=output, scratch_root=self.scratch, record_directory="vasp")
        self.assertFalse(output.exists())
        self.assertFalse((self.scratch / "vasp").exists())
        self.output.write_bytes(original_poscar)
        record = json.loads(self.record.read_text())
        record["publication"]["state"] = "incomplete"
        self.record.write_text(json.dumps(record))
        with self.assertRaises(PreparationError):
            prepare(spec, output=output, scratch_root=self.scratch, record_directory="vasp")
        self.assertFalse(output.exists())
        self.assertFalse((self.scratch / "vasp").exists())


if __name__ == "__main__":
    unittest.main()
