from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ase.io import read as ase_read

from cmw.cli import main
from cmw.core import preparation_publication as publication
from cmw.structure.conversion import render_xyz_to_poscar, xyz_to_poscar
from cmw.structure.embedding import embed_molecule


class MolecularEmbeddingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.inputs = self.root / "repository-inputs"
        self.scratch = self.root / "scratch"
        self.inputs.mkdir()
        self.scratch.mkdir()
        self.source = self.root / "molecule.xyz"
        self.source.write_text("4\nsynthetic molecule\nH 0 0 0\nO 2 1 0\nH 1 0 0\nC 3 2 1\n")
        self.original = self.source.read_bytes()
        self.output = self.inputs / "POSCAR"
        self.record = self.scratch / "embedding" / "preparation.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def embed(self, **options):
        return embed_molecule(self.source, self.output, scratch_root=self.scratch,
                              record_directory="embedding", **options)

    def arguments(self, *extra):
        return ["structure", "embed-molecule", "--input", str(self.source),
                "--output", str(self.output), "--scratch-root", str(self.scratch),
                "--record-directory", "embedding", "--json", *extra]

    def test_canonical_cli_preview_is_registered_explicit_and_has_no_writes(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("subprocess.Popen", side_effect=AssertionError("no execution")):
            self.assertEqual(main(self.arguments("--dry-run", "--net-charge", "1")), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["record_kind"], "molecular-embedding")
        self.assertEqual(report["conversion"]["resolved_cell_mode"], "cubic")
        self.assertEqual(report["effective_inputs"]["status"], "unobserved")
        self.assertEqual(list(self.inputs.iterdir()), [])
        self.assertEqual(list(self.scratch.iterdir()), [])
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main(["structure", "embed-molecule", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("not CIF or periodic/extxyz import", output.getvalue())

    def test_publication_keeps_metadata_and_mapping_only_in_scratch(self) -> None:
        with patch("subprocess.Popen", side_effect=AssertionError("no execution")):
            report = self.embed(species_order=["O", "C", "H"], vacuum=12, net_charge=1)
        stored = json.loads(self.record.read_text())
        self.assertEqual(stored, report)
        self.assertEqual({p.name for p in self.inputs.iterdir()}, {"POSCAR"})
        self.assertEqual({p.name for p in self.record.parent.iterdir()}, {"preparation.json"})
        self.assertEqual(report["publication"]["state"], "complete")
        self.assertEqual(report["mapping"]["poscar_to_original"], {"0": 1, "1": 3, "2": 0, "3": 2})
        for old, new in report["mapping"]["original_to_poscar"].items():
            self.assertEqual(report["mapping"]["poscar_to_original"][str(new)], int(old))
        atoms = ase_read(self.output, format="vasp")
        self.assertEqual(atoms.get_chemical_symbols(), ["O", "C", "H", "H"])
        self.assertEqual(tuple(atoms.cell.lengths()), (27., 27., 27.))
        self.assertEqual(report["conversion"]["coordinate_mode"], "Cartesian")
        self.assertTrue(all(v >= 12 - 1e-8 for v in report["conversion"]["actual_vacuum_clearances_angstrom"].values()))
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(publication.inspect_publication(self.record)["status"], "valid")

    def test_one_shared_renderer_preserves_legacy_bytes_mapping_and_overwrite(self) -> None:
        legacy = self.root / "legacy"
        legacy.mkdir()
        self.embed(vacuum=11, cell_mode="orthorhombic", net_charge=-1)
        xyz_to_poscar(self.source, legacy / "POSCAR", vacuum=11, cell_mode="orthorhombic", net_charge=-1)
        first = (legacy / "POSCAR").read_bytes()
        self.assertEqual(first, self.output.read_bytes())
        self.assertEqual(json.loads((legacy / "atom_mapping.json").read_text()),
                         json.loads(self.record.read_text())["mapping"])
        (legacy / "POSCAR").write_text("old output")
        xyz_to_poscar(self.source, legacy / "POSCAR", vacuum=11, cell_mode="orthorhombic", net_charge=-1)
        self.assertEqual((legacy / "POSCAR").read_bytes(), first)
        self.assertTrue((legacy / "conversion_metadata.json").is_file())

    def test_renderer_and_preview_are_deterministic_without_output_side_effects(self) -> None:
        first = render_xyz_to_poscar(self.source, ordinary_xyz_only=True)
        self.assertEqual(render_xyz_to_poscar(self.source, ordinary_xyz_only=True), first)
        plan = self.embed(dry_run=True)
        elsewhere = embed_molecule(self.source, self.inputs / "another-name", scratch_root=self.scratch,
                                    record_directory="elsewhere", dry_run=True)
        self.assertEqual(plan["preparation_content_id"], elsewhere["preparation_content_id"])
        self.assertEqual(list(self.inputs.iterdir()), [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_cif_extxyz_metadata_and_extra_columns_are_rejected_without_writes(self) -> None:
        for name, text in (
            ("structure.cif", "data_test\n_cell_length_a 10\n"),
            ("molecule.extxyz", "1\nordinary text\nH 0 0 0\n"),
            ("periodic.xyz", '1\nLattice="10 0 0 0 10 0 0 0 10" pbc="T T T"\nH 0 0 0\n'),
            ("properties.xyz", '1\nProperties=species:S:1:pos:R:3\nH 0 0 0\n'),
            ("extra.xyz", "1\nsynthetic molecule\nH 0 0 0 0.5\n"),
        ):
            with self.subTest(name=name):
                source = self.root / name
                source.write_text(text)
                with self.assertRaises(ValueError):
                    embed_molecule(source, self.output, scratch_root=self.scratch,
                                   record_directory="embedding")
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_no_clobber_missing_scratch_and_aliases_fail_before_writes(self) -> None:
        for existing in (self.output, self.record.parent):
            with self.subTest(existing=existing):
                if existing == self.output:
                    existing.write_text("keep me")
                else:
                    existing.mkdir()
                with self.assertRaises(ValueError):
                    self.embed()
                if existing == self.output:
                    self.assertEqual(existing.read_text(), "keep me")
                    existing.unlink()
                else:
                    self.assertEqual(list(existing.iterdir()), [])
                    existing.rmdir()
        with self.assertRaises(ValueError):
            embed_molecule(self.source, self.output, scratch_root=self.root / "absent", record_directory="embedding")
        with self.assertRaises(ValueError):
            embed_molecule(self.source, self.source, scratch_root=self.scratch, record_directory="embedding")
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_staging_failure_cleans_temporary_file_and_records_failed_intent(self) -> None:
        with patch.object(publication, "_write_candidate", side_effect=OSError("synthetic staging failure")):
            with self.assertRaises(publication.PublicationError) as caught:
                self.embed()
        self.assertFalse(caught.exception.incomplete)
        self.assertEqual(list(self.inputs.iterdir()), [])
        self.assertEqual(json.loads(self.record.read_text())["publication"]["state"], "failed")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_record_finalization_failure_keeps_poscar_and_reports_incomplete(self) -> None:
        original = publication._write_record

        def fail_finalization(path, record):
            if record["publication"]["state"] == "complete":
                raise OSError("synthetic finalization failure")
            original(path, record)

        with patch.object(publication, "_write_record", side_effect=fail_finalization):
            with self.assertRaises(publication.PublicationError) as caught:
                self.embed()
        self.assertTrue(caught.exception.incomplete)
        self.assertEqual({p.name for p in self.inputs.iterdir()}, {"POSCAR"})
        self.assertEqual(json.loads(self.record.read_text())["publication"]["state"], "incomplete")
        self.assertEqual(publication.inspect_publication(self.record)["status"], "invalid")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_cli_failure_has_nonzero_exit_and_structured_reason(self) -> None:
        self.output.write_text("keep me")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(self.arguments()), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "PUBLICATION_REJECTED")
        self.assertEqual(self.output.read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
