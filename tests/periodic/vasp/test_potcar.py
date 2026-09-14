from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.periodic.vasp.potentials import (
    build_potcar, check_potcar, list_potentials, parse_potcar,
    read_poscar_species, resolve_root,
)


def synthetic(element: str, label: str | None = None) -> bytes:
    # Entirely invented metadata fixture; never copied from a licensed dataset.
    return (f'TITEL = FAKE {label or element}\nVRHFIN = {element}: fake\n'
            'synthetic payload\nEnd of Dataset\n').encode()


def poscar(species: str) -> str:
    count = len(species.split())
    return ('synthetic\n1\n1 0 0\n0 1 0\n0 0 1\n' + species + '\n'
            + ' '.join(['1'] * count) + '\nDirect\n' + '0 0 0\n' * count)


class PotcarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.root = self.work / 'library'
        self.root.mkdir()
        self.poscar = self.work / 'POSCAR'
        self.poscar.write_text(poscar('Zn In S'))
        self.output = self.work / 'POTCAR'
        for element, label in [('Zn', 'Zn'), ('In', 'In'), ('S', 'S'),
                               ('In', 'In_d'), ('Zn', 'Zn_sv'), ('Ti', 'Ti_pv'),
                               ('C', 'C'), ('C', 'C_h'), ('Ca', 'Ca'), ('Cl', 'Cl')]:
            directory = self.root / label
            directory.mkdir()
            (directory / 'POTCAR').write_bytes(synthetic(element, label))
        self.environment = patch.dict(os.environ, {'CMW_VASP_POTCAR_ROOT': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def build(self, **kwargs):
        return build_potcar(self.poscar, self.output, root=self.root, **kwargs)

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['vasp', 'potcar', *map(str, arguments)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_species_order(self):
        self.assertEqual(read_poscar_species(self.poscar), ('Zn', 'In', 'S'))

    def test_repeated_species_blocks_preserve_order(self):
        self.poscar.write_text(poscar('In Zn In'))
        self.build(pots=['In=In_d'])
        self.assertEqual([d.label for d in check_potcar(self.poscar, self.output)], ['In_d', 'Zn', 'In_d'])

    def test_default_suffix_free_and_exact_concatenation(self):
        result = self.build()
        self.assertEqual(result['potentials'], ('Zn', 'In', 'S'))
        self.assertEqual(self.output.read_bytes(), b''.join(synthetic(e) for e in ('Zn', 'In', 'S')))
        self.assertEqual(len(check_potcar(self.poscar, self.output)), 3)
        self.assertEqual({p.name for p in self.work.iterdir()}, {'POSCAR', 'POTCAR', 'library'})

    def test_single_cli_override_and_variant_check(self):
        code, out, err = self.cli('build', self.poscar, '--output', self.output,
                                  '--potcar-root', self.root, '--pot', 'In=In_d')
        self.assertEqual(code, 0, err)
        self.assertIn('In -> In_d  [CLI override]', out)
        self.assertEqual([d.label for d in check_potcar(self.poscar, self.output)], ['Zn', 'In_d', 'S'])
        self.assertEqual(self.cli('check', self.poscar, '--potcar', self.output)[0], 0)

    def test_multiple_cli_overrides_are_order_independent(self):
        outputs = []
        for overrides in [('In=In_d', 'Zn=Zn_sv'), ('Zn=Zn_sv', 'In=In_d')]:
            code, _, err = self.cli('build', self.poscar, '--output', self.output,
                                    '--potcar-root', self.root, '--force',
                                    '--pot', overrides[0], '--pot', overrides[1])
            self.assertEqual(code, 0, err)
            outputs.append(self.output.read_bytes())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual([d.label for d in check_potcar(self.poscar, self.output)], ['Zn_sv', 'In_d', 'S'])

    def test_environment_fallback(self):
        with patch.dict(os.environ, {'CMW_VASP_POTCAR_ROOT': str(self.root)}):
            self.assertEqual(self.cli('build', self.poscar, '--output', self.output)[0], 0)

    def test_cli_root_precedence(self):
        with patch.dict(os.environ, {'CMW_VASP_POTCAR_ROOT': str(self.work / 'missing')}):
            self.assertEqual(self.cli('build', self.poscar, '--output', self.output,
                                      '--potcar-root', self.root)[0], 0)

    def test_missing_root_diagnostic(self):
        code, _, err = self.cli('build', self.poscar, '--output', self.output)
        self.assertNotEqual(code, 0)
        self.assertIn('--potcar-root', err)
        self.assertIn('CMW_VASP_POTCAR_ROOT', err)
        self.assertFalse(self.output.exists())

    def test_invalid_root(self):
        for root in (self.work / 'absent', self.poscar):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, 'not a directory'):
                resolve_root(root)

    def test_missing_directory_no_partial_output(self):
        (self.root / 'In' / 'POTCAR').unlink()
        (self.root / 'In').rmdir()
        with self.assertRaisesRegex(ValueError, 'Missing potential directory'):
            self.build()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.work.glob('.cmw-potcar-*')), [])

    def test_missing_potcar(self):
        (self.root / 'In' / 'POTCAR').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing POTCAR'):
            self.build()

    def test_invalid_variant(self):
        with self.assertRaisesRegex(ValueError, 'Missing potential directory'):
            self.build(pots=['In=DoesNotExist'])

    def test_absent_element(self):
        with self.assertRaisesRegex(ValueError, 'absent from POSCAR'):
            self.build(pots=['Ti=Ti_pv'])

    def test_conflicting_duplicates(self):
        with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
            self.build(pots=['In=In', 'In=In_d'])

    def test_identical_duplicates(self):
        self.assertEqual(self.build(pots=['In=In_d', 'In=In_d'])['potentials'][1], 'In_d')

    def test_malformed_override(self):
        for value in ('In', 'In=', '=In', 'In=In_d=extra'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build(pots=[value])

    def test_wrong_element_metadata(self):
        (self.root / 'In' / 'POTCAR').write_bytes(synthetic('Ti'))
        with self.assertRaisesRegex(ValueError, 'species order mismatch'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_path_traversal_and_absolute_paths(self):
        for label in ('../In', '/In', 'In/../../outside', 'In\\outside', '..'):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, 'single directory entry'):
                self.build(pots=[f'In={label}'])

    def test_directory_and_file_symlink_escape(self):
        (self.root / 'In_escape').symlink_to(self.work, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.build(pots=['In=In_escape'])
        target = self.root / 'In' / 'POTCAR'
        target.unlink()
        target.symlink_to(self.poscar)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.build()

    def test_unreadable_selected_file(self):
        original = Path.read_bytes
        def read(path):
            if path == (self.root / 'In' / 'POTCAR').resolve():
                raise PermissionError('synthetic unreadable input')
            return original(path)
        with patch.object(Path, 'read_bytes', read), self.assertRaises(PermissionError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_overwrite_protection_and_force(self):
        self.output.write_bytes(b'previous output')
        with self.assertRaisesRegex(FileExistsError, '--force'):
            self.build()
        self.assertEqual(self.output.read_bytes(), b'previous output')
        self.build(force=True)
        self.assertEqual(len(check_potcar(self.poscar, self.output)), 3)

    def test_failed_force_preserves_existing_output(self):
        self.output.write_bytes(b'previous output')
        with self.assertRaises(ValueError):
            self.build(force=True, pots=['In=absent'])
        self.assertEqual(self.output.read_bytes(), b'previous output')

    def test_failed_publication_cleans_temporary_file(self):
        with patch('cmw.periodic.vasp.potentials.os.link', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.build()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.work.glob('.cmw-potcar-*')), [])

    def test_racing_writer_is_not_overwritten(self):
        link = os.link
        def racing_link(source, destination):
            Path(destination).write_bytes(b'racing output')
            link(source, destination)
        with patch('cmw.periodic.vasp.potentials.os.link', side_effect=racing_link), self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(self.output.read_bytes(), b'racing output')
        self.assertEqual(list(self.work.glob('.cmw-potcar-*')), [])

    def test_cannot_force_overwrite_inputs(self):
        for output in (self.poscar, self.root / 'In' / 'POTCAR'):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'must not replace'):
                build_potcar(self.poscar, output, root=self.root, force=True)

    def test_check_order_mismatch_cli(self):
        self.output.write_bytes(b''.join(synthetic(e) for e in ('In', 'Zn', 'S')))
        code, _, err = self.cli('check', self.poscar, '--potcar', self.output)
        self.assertNotEqual(code, 0)
        self.assertIn('species order mismatch', err)

    def test_check_count_mismatch_cli(self):
        self.output.write_bytes(synthetic('Zn'))
        code, _, err = self.cli('check', self.poscar, '--potcar', self.output)
        self.assertNotEqual(code, 0)
        self.assertIn('dataset count mismatch', err)

    def test_list_base_variants_and_prefix_collisions(self):
        self.assertEqual(list_potentials('In', self.root), ('In', 'In_d'))
        self.assertEqual(list_potentials('C', self.root), ('C', 'C_h'))
        code, out, err = self.cli('list', 'In', '--potcar-root', self.root)
        self.assertEqual(code, 0, err)
        self.assertIn('In_d', out)
        self.assertNotIn('synthetic payload', out)
        self.assertEqual(list_potentials('Xe', self.root), ())

    def test_ambiguous_poscar_formats(self):
        for text in (poscar('3 2 1'), poscar('Zn In_d S'), poscar('Qq'), '',
                     poscar('Zn In S').replace('1 1 1\n', '1 1\n')):
            self.poscar.write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'POSCAR'):
                read_poscar_species(self.poscar)

    def test_metadata_fallbacks(self):
        self.assertEqual(parse_potcar(b'TITEL = FAKE In_d\nEnd of Dataset\n')[0].element, 'In')
        self.assertIsNone(parse_potcar(b'VRHFIN = In: fake\nEnd of Dataset\n')[0].label)

    def test_invalid_or_truncated_metadata(self):
        for data in (b'', b'End of Dataset\n', synthetic('In').replace(b'End of Dataset\n', b''),
                     synthetic('In') + b'unterminated tail',
                     synthetic('In').replace(b'VRHFIN = In', b'VRHFIN = Ti'),
                     synthetic('In').replace(b'synthetic payload', b'TITEL = FAKE In'),
                     synthetic('In').replace(b'FAKE In', b'FAKE Qq')):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_potcar(data)

    def test_multiple_datasets_in_single_library_file_rejected(self):
        (self.root / 'In' / 'POTCAR').write_bytes(synthetic('In') * 2)
        with self.assertRaisesRegex(ValueError, 'dataset count mismatch'):
            self.build()

    def test_missing_final_newline_is_separated(self):
        (self.root / 'In' / 'POTCAR').write_bytes(synthetic('In').rstrip(b'\n'))
        self.build()
        self.assertEqual(len(check_potcar(self.poscar, self.output)), 3)

    def test_default_cli_paths(self):
        previous = Path.cwd()
        try:
            os.chdir(self.work)
            self.assertEqual(self.cli('build', '--potcar-root', self.root)[0], 0)
            self.assertEqual(self.cli('check')[0], 0)
        finally:
            os.chdir(previous)

    def test_help_documents_selection_and_root(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exc:
            main(['vasp', 'potcar', 'build', '--help'])
        self.assertEqual(exc.exception.code, 0)
        for text in ('suffix-free', 'ELEMENT=VARIANT', 'CMW_VASP_POTCAR_ROOT', '--force', 'does not provide'):
            self.assertIn(text, output.getvalue())
