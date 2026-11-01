"""Synthetic preparation/staging/Jobs provenance; never execute a runner or VASP."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs import input_sources
from cmw.jobs.progress import resolve_context
from cmw.jobs.store import JobsError, Store
from cmw.periodic.vasp.inputs import parse_poscar
from cmw.periodic.vasp.preparation import prepare
from cmw.progress.formatting import progress_text
from cmw.progress.models import SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts
from tests.periodic.vasp.runner_case import timestamp
from tests.periodic.vasp.test_inputs import potential


INPUTS = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def poscar_text(species=('In', 'S'), counts=(1, 1), flags=None):
    count = sum(counts)
    rows = ['Synthetic registration fixture', '1', '4 0 0', '0 5 0', '0 0 6',
            ' '.join(species), ' '.join(map(str, counts))]
    if flags is not None:
        rows.append('Selective dynamics')
    rows.append('Direct')
    for index in range(count):
        row = f'{(index + 1) / (count + 1):.10f} 0.1 0.2'
        if flags is not None:
            row += ' ' + ' '.join('T' if flag else 'F' for flag in flags[index])
        rows.append(row)
    return '\n'.join(rows) + '\n'


class PoscarRegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-poscar-registration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'jobs')
        blocker = patch('subprocess.Popen', side_effect=AssertionError('No runner or scientific process is allowed'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def case(self, name, *, flags=((True,) * 3, (False,) * 3), species=('In', 'S'),
             counts=(1, 1), static=False, actual_preparation=False, isif=0):
        base = self.root / name
        base.mkdir()
        source = base / 'source.POSCAR'
        source.write_text(poscar_text(species, counts, flags))
        inputs, output = base / 'inputs', base / 'runs' / 'attempt'
        output.parent.mkdir()
        record = None
        if actual_preparation:
            library = base / 'library'
            library.mkdir()
            for element in set(species):
                directory = library / element
                directory.mkdir()
                (directory / 'POTCAR').write_bytes(potential((element,)))
            incar = {'GGA': 'PE', 'ENCUT': 350, 'PREC': 'Accurate', 'ISPIN': 1,
                     'ISMEAR': 0, 'SIGMA': .05, 'EDIFF': 1e-6, 'NELM': 120,
                     'ALGO': 'Normal', 'ISTART': 0, 'ICHARG': 2}
            if not static:
                incar.update(IBRION=2, NSW=20, EDIFFG=-.02, POTIM=.3)
                isif = 2  # Existing preparation contract; monitoring also supports ISIF=0.
            specification = {'schema_version': 1,
                'calculation': 'static' if static else 'fixed-cell-relaxation',
                'structure': 'source.POSCAR',
                'profile': {'name': 'synthetic-only', 'incar': incar,
                    'kpoints': {'mode': 'Gamma', 'mesh': [3, 1, 1], 'shift': [0, 0, 0]},
                    'potentials': {'root': 'library', 'variants': {symbol: symbol for symbol in set(species)},
                                   'requirements': {'family': 'FAKE'}}}}
            spec = base / 'spec.json'
            spec.write_text(json.dumps(specification))
            scratch = base / 'scratch'
            scratch.mkdir()
            record = prepare(spec, output=inputs, scratch_root=scratch, record_directory='preparation')
        else:
            inputs.mkdir()
            shutil.copyfile(source, inputs / 'POSCAR')
            native = native_texts(relaxation=not static, settings={'ISIF': str(isif), 'NSW': '0' if static else '20'})
            (inputs / 'INCAR').write_text(native['INCAR'])
            (inputs / 'KPOINTS').write_text('Synthetic\n0\nGamma\n3 1 1\n0 0 0\n')
            (inputs / 'POTCAR').write_bytes(potential(species))
        return {'input': inputs, 'output': output, 'source': source, 'flags': flags,
                'species': species, 'counts': counts, 'static': static, 'isif': isif, 'preparation': record}

    def argv(self, case):
        return [str(self.root / 'runner' / 'run-vasp.sh'), '--input', str(case['input']),
                '--output', str(case['output']), '--binary', 'std', '--ranks', '8',
                '--restart', 'none', '--managed-foreground']

    def add(self, case, *, cli=False, argv=None, sources=()):
        argv = self.argv(case) if argv is None else argv
        if not cli:
            return self.store.add(argv=argv, cwd=case['input'], name='Synthetic VASP only',
                                  engine='VASP', hold=True, progress_sources=sources)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['jobs', '--state', str(self.store.root), 'add', '--name', 'Synthetic VASP CLI',
                         '--engine', 'VASP', '--cwd', str(case['input']), '--hold', '--json', '--', *argv])
        self.assertEqual((code, stderr.getvalue()), (0, ''))
        return json.loads(stdout.getvalue())

    def read_job(self, job):
        with self.store.transaction(create=False) as con:
            return self.store.get(con, job['id'])

    def stage(self, case, job, *, forces=None, native_count=None, evaluations=1, active=False):
        """Mimic the documented runner copy and prefix, without invoking shell code."""
        output = case['output']
        output.mkdir(exist_ok=True)
        for name in INPUTS:
            if (case['input'] / name).is_file():
                shutil.copyfile(case['input'] / name, output / name)
        staged_at = time.time()
        lines = ['state: STAGING', 'child_status: NOT STARTED',
                 'staging_start_utc: ' + timestamp(staged_at), 'source: ' + str(case['input']),
                 'output: ' + str(output), 'managed_foreground: True', 'restart_requested: none',
                 'requested_version: 6.6.1', 'original_incar_sha256: ' + digest(output / 'INCAR')]
        for name in INPUTS:
            source = case['input'] / name
            if not source.is_file():
                continue
            info = source.stat()
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            lines.append(f'staged_file: {name}; bytes={info.st_size}; source_stat={identity}; SHA256={digest(output / name)}')
        lines.extend(('effective_incar_sha256: ' + digest(output / 'INCAR'), 'state: RUNNING'))
        metadata = output / 'RUN_METADATA.txt'
        metadata.write_text('\n'.join(lines) + '\n')
        count = sum(case['counts']) if native_count is None else native_count
        positions = tuple((index / max(1, count), .1, .2) for index in range(count))
        if forces is None:
            forces = ((.003, .004, 0),) + ((3, 4, 0),) * (count - 1)
        texts = native_texts(relaxation=not case['static'], positions=positions, forces=forces,
            evaluations=evaluations, footer=False,
            settings={'ISIF': str(case['isif']), 'NSW': '0' if case['static'] else '20'})
        species_header = ''.join(f' VRHFIN ={symbol}: synthetic\n' for symbol in case['species'])
        species_header += ' ions per type = ' + ' '.join(map(str, case['counts'])) + '\n'
        outcar = texts['OUTCAR'].replace(' VRHFIN =H: synthetic\n VRHFIN =He: synthetic\n ions per type = 1 1\n',
                                        species_header).replace(' NIONS = 2\n', f' NIONS = {count}\n')
        outcar = outcar.replace(' reached required accuracy - stopping structural energy minimisation\n', '')
        if active:
            index = evaluations + 1
            outcar += f' Iteration {index}( 1)\n EDDAV: synthetic\n free energy    TOTEN = -2.000000000000 eV\n'
            texts['OSZICAR'] += 'DAV: 1 -2.000000000000 -2.00000000E+00 -0.02000000E+00 12 0.00100000\n'
        (output / 'OUTCAR').write_text(outcar)
        (output / 'OSZICAR').write_text(texts['OSZICAR'])
        with self.store.transaction() as con:
            current = self.store.get(con, job['id'])
            current.update(status='Run', started_at=job['enqueued_at'])
            self.store.save(con, current)
        return metadata

    def observe(self, job, *, context=None):
        before = self.store.path.read_bytes()
        context = context or resolve_context(self.store, job['id'], expected_attempt_id=job['attempt_id'])
        service = ObservationService()
        try:
            for _ in range(20):
                snapshot = service.poll(context)
                if snapshot.completeness != 'CATCHING_UP' and not any(source.has_more for source in snapshot.sources):
                    break
            else:
                self.fail('Synthetic fixture exceeded twenty normal bounded observation polls')
        finally:
            service.close()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')
        return snapshot

    def staged_source(self, context):
        matches = [source for source in context.sources if source.role == 'poscar']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].origins, ('attempt-staged-input',))
        return matches[0]

    @staticmethod
    def forces(snapshot):
        return {series.latest.semantic_id.rsplit('.', 1)[-1]: series.latest for series in snapshot.metrics
                if series.latest.semantic_id.startswith('vasp.force.')}

    def assert_no_attributed_mask(self, job):
        snapshot = self.observe(job)
        self.assertFalse(any('attempt-staged-input' in source.origins for source in snapshot.context.sources))
        self.assertNotIn('free', self.forces(snapshot))
        self.assertTrue(all(metric.criterion is None for metric in self.forces(snapshot).values()))
        return snapshot

    def test_actual_preparation_registration_staging_adapter_and_constrained_card(self):
        case = self.case('prepared', actual_preparation=True)
        self.assertEqual(case['preparation']['publication']['state'], 'complete')
        self.assertEqual((case['input'] / 'POSCAR').read_bytes(), case['source'].read_bytes())
        job = self.add(case)
        binding = job['progress_sources']['vasp_input']
        self.assertEqual(binding['attempt_id'], job['attempt_id'])
        self.assertEqual(binding['sha256'], digest(case['input'] / 'POSCAR'))
        self.assertFalse(case['output'].exists())
        pending = resolve_context(self.store, job['id'])
        self.assertNotIn('poscar', {source.role for source in pending.sources})
        self.stage(case, job)
        snapshot = self.observe(job)
        source = self.staged_source(snapshot.context)
        self.assertEqual(source.path, str(case['output'] / 'POSCAR'))
        self.assertEqual(source.expected_sha256, binding['sha256'])
        self.assertEqual(source.expected_size, binding['bytes'])
        self.assertEqual(snapshot.context.attempt_id, job['attempt_id'])
        metric = self.forces(snapshot)['free']
        self.assertEqual(metric.value, .005)
        self.assertEqual(metric.criterion.signed_value, -.02)
        self.assertEqual(metric.qualification, 'provisional')
        self.assertTrue(metric.relation.startswith('PROVISIONAL_'))
        self.assertIsNone(self.forces(snapshot)['fixed'].criterion)
        self.assertIn('Fmax movable', progress_text(snapshot, width=95))
        self.assertIn('Target 0.020', progress_text(snapshot, width=95))

    def test_cli_and_api_share_automatic_registration_for_static_and_relaxation(self):
        for cli in (False, True):
            for static in (False, True):
                with self.subTest(cli=cli, static=static):
                    case = self.case(f'path-{cli}-{static}', static=static, flags=None if static else ((True,) * 3, (False,) * 3))
                    job = self.add(case, cli=cli)
                    self.assertIn('vasp_input', job['progress_sources'])
                    self.assertEqual(job['status'], 'Hold')
                    self.stage(case, job)
                    snapshot = self.observe(job)
                    self.staged_source(snapshot.context)
                    force = self.forces(snapshot)['free']
                    self.assertEqual(force.criterion is None, static)
                    self.assertEqual(snapshot.calculation, 'static' if static else 'fixed-cell-relaxation')

    def test_controlled_suspension_requires_matching_staging_protocol(self):
        for kind in ('valid', 'missing-version', 'wrong-version', 'missing-protocol', 'wrong-protocol'):
            with self.subTest(kind=kind):
                case = self.case('controlled-' + kind)
                argv = self.argv(case) + ['--controlled-suspension', '--execution-budget', '60']
                job = self.add(case, argv=argv)
                self.assertIsNone(job['progress_sources']['vasp_input']['issue'])
                metadata = self.stage(case, job)
                fields = {'runner_metadata_version': '2',
                          'controlled_suspension_protocol': 'cmw-suspension-v1'}
                if kind == 'missing-version':
                    fields.pop('runner_metadata_version')
                elif kind == 'wrong-version':
                    fields['runner_metadata_version'] = '1'
                elif kind == 'missing-protocol':
                    fields.pop('controlled_suspension_protocol')
                elif kind == 'wrong-protocol':
                    fields['controlled_suspension_protocol'] = 'unrecognized-protocol'
                prefix = ''.join(f'{key}: {value}\n' for key, value in fields.items())
                metadata.write_text(metadata.read_text().replace('state: RUNNING\n', prefix + 'state: RUNNING\n'))
                if kind == 'valid':
                    snapshot = self.observe(job)
                    self.staged_source(snapshot.context)
                    self.assertEqual(self.forces(snapshot)['free'].value, .005)
                    self.assertEqual(self.forces(snapshot)['free'].qualification, 'provisional')
                else:
                    snapshot = self.assert_no_attributed_mask(job)
                    self.assertTrue(any('Controlled runner staging protocol' in item
                                        for item in snapshot.context.limitations))

    def test_literal_shell_wrappers_and_equals_options_register_the_same_route(self):
        for kind in ('shell', 'equals', 'shell-equals'):
            with self.subTest(kind=kind):
                case = self.case('arguments-' + kind)
                argv = self.argv(case)
                if 'equals' in kind:
                    argv = [argv[0], f'--input={case["input"]}', f'--output={case["output"]}',
                            '--binary=std', '--ranks=8', '--restart=none', '--managed-foreground']
                if 'shell' in kind:
                    argv = ['/bin/bash', '-eu', '-o', 'pipefail', '--', *argv]
                job = self.add(case, argv=argv)
                binding = job['progress_sources']['vasp_input']
                self.assertIsNone(binding['issue'])
                self.assertEqual((binding['input'], binding['output']),
                                 (str(case['input']), str(case['output'])))
                self.assertEqual(binding['sha256'], digest(case['input'] / 'POSCAR'))
                self.stage(case, job)
                snapshot = self.observe(job)
                self.staged_source(snapshot.context)
                self.assertEqual(self.forces(snapshot)['free'].value, .005)

    def test_160_atom_mask_ordering_and_historical_eval_six_norms(self):
        fixed = set(range(25, 41)) | set(range(101, 125))
        flags = tuple((index not in fixed,) * 3 for index in range(1, 161))
        case = self.case('large', counts=(64, 96), flags=flags)
        job = self.add(case)
        vectors = [(0, 0, 0)] * 160
        vectors[11], vectors[108] = (.972751, 0, 0), (1.044040, 0, 0)
        self.stage(case, job, forces=vectors, evaluations=6, active=True)
        snapshot = self.observe(job)
        source = self.staged_source(snapshot.context)
        parsed = parse_poscar(Path(source.path).read_bytes())
        self.assertEqual(parsed['atom_species'], ['In'] * 64 + ['S'] * 96)
        self.assertEqual(parsed['selective_dynamics'], [list(row) for row in flags])
        self.assertEqual(sum(all(row) for row in parsed['selective_dynamics']), 120)
        self.assertEqual(sum(not any(row) for row in parsed['selective_dynamics']), 40)
        self.assertEqual(sum(sum(row) for row in parsed['selective_dynamics']), 360)
        forces = self.forces(snapshot)
        self.assertAlmostEqual(forces['free'].value, .972751)
        self.assertAlmostEqual(forces['fixed'].value, 1.044040)
        self.assertAlmostEqual(forces['all'].value, 1.044040)
        self.assertEqual(forces['free'].context[0].index, 6)
        text = progress_text(snapshot, width=95)
        for expected in ('Eval 7/20', '0.972751 eV/Å @E6', 'Target 0.020 eV/Å', 'EDIFF 1E-6 eV'):
            self.assertIn(expected, text)
        self.assertEqual(text.count('Fmax'), 1)

    def test_repeated_species_order_and_mask_are_not_regrouped(self):
        flags = ((False,) * 3, (True,) * 3, (False,) * 3)
        case = self.case('ordered', species=('In', 'S', 'In'), counts=(1, 1, 1), flags=flags)
        job = self.add(case)
        self.stage(case, job, forces=((9, 0, 0), (3, 4, 0), (7, 0, 0)))
        snapshot = self.observe(job)
        source = self.staged_source(snapshot.context)
        parsed = parse_poscar(Path(source.path).read_bytes())
        self.assertEqual(parsed['atom_species'], ['In', 'S', 'In'])
        self.assertEqual(parsed['selective_dynamics'], [list(row) for row in flags])
        self.assertEqual(Path(source.path).read_bytes(), case['source'].read_bytes())
        self.assertEqual(self.forces(snapshot)['free'].value, 5)
        self.assertEqual(self.forces(snapshot)['fixed'].value, 9)

    def test_partial_direction_constraints_stay_unsupported_for_force_comparison(self):
        case = self.case('partial', flags=((True, False, True), (True,) * 3))
        job = self.add(case)
        self.stage(case, job)
        snapshot = self.observe(job)
        self.staged_source(snapshot.context)
        self.assertIsNone(self.forces(snapshot)['free'].criterion)
        self.assertNotIn('Fmax movable', progress_text(snapshot, width=95))
        self.assertTrue(any('not Cartesian masks' in item for item in snapshot.limitations))

    def test_missing_malformed_and_wrong_count_inputs_never_invent_a_mask(self):
        for kind in ('missing', 'malformed', 'wrong-count'):
            with self.subTest(kind=kind):
                case = self.case(kind, species=('In',) if kind == 'wrong-count' else ('In', 'S'),
                    counts=(1,) if kind == 'wrong-count' else (1, 1),
                    flags=((True,) * 3,) if kind == 'wrong-count' else ((True,) * 3, (False,) * 3))
                if kind == 'missing':
                    (case['input'] / 'POSCAR').unlink()
                elif kind == 'malformed':
                    (case['input'] / 'POSCAR').write_text('not a POSCAR\n')
                job = self.add(case)
                self.assertEqual(job['status'], 'Hold')
                self.stage(case, job, native_count=2)
                snapshot = self.observe(job)
                self.assertNotIn('free', self.forces(snapshot))
                self.assertIsNone(self.forces(snapshot)['all'].criterion)
                self.assertNotIn('Fmax movable', progress_text(snapshot, width=95))

    def test_changed_prepared_identity_before_staging_is_not_adopted(self):
        for change in ('bytes', 'inode'):
            with self.subTest(change=change):
                case = self.case('prepared-' + change)
                job = self.add(case)
                path = case['input'] / 'POSCAR'
                original = path.read_bytes()
                if change == 'inode':
                    path.rename(path.with_suffix('.old'))
                path.write_bytes(original + (b'\n' if change == 'bytes' else b''))
                self.stage(case, job)
                self.assert_no_attributed_mask(job)

    def test_changed_staged_poscar_and_resolve_read_gap_fail_closed(self):
        for after_resolution in (False, True):
            with self.subTest(after_resolution=after_resolution):
                case = self.case('staged-' + str(after_resolution))
                job = self.add(case)
                self.stage(case, job)
                context = resolve_context(self.store, job['id'])
                self.staged_source(context)
                path = case['output'] / 'POSCAR'
                path.write_bytes(path.read_bytes().replace(b'T T T', b'F F F', 1))
                if after_resolution:
                    snapshot = self.observe(job, context=context)
                    self.assertNotIn('free', self.forces(snapshot))
                    self.assertTrue(all(metric.criterion is None for metric in self.forces(snapshot).values()))
                else:
                    self.assert_no_attributed_mask(job)

    def test_runner_paths_hash_stat_and_lifecycle_prefix_must_match(self):
        for kind in ('source', 'output', 'hash', 'stat', 'missing', 'incomplete'):
            with self.subTest(kind=kind):
                case = self.case('metadata-' + kind)
                job = self.add(case)
                metadata = self.stage(case, job)
                text = metadata.read_text()
                if kind in ('source', 'output'):
                    key = 'source' if kind == 'source' else 'output'
                    path = case['input' if kind == 'source' else 'output']
                    text = text.replace(f'{key}: {path}', f'{key}: {self.root / "unrelated"}')
                elif kind == 'hash':
                    text = text.replace(digest(case['output'] / 'POSCAR'), '0' * 64)
                elif kind == 'stat':
                    line = next(line for line in text.splitlines() if line.startswith('staged_file: POSCAR;'))
                    info = (case['input'] / 'POSCAR').stat()
                    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    changed = (info.st_dev, info.st_ino + 1, info.st_size, info.st_mtime_ns)
                    modified = line.replace(f'source_stat={identity}', f'source_stat={changed}')
                    text = text.replace(line, modified)
                elif kind == 'incomplete':
                    text = text.replace('state: RUNNING\n', '')
                if kind == 'missing':
                    metadata.unlink()
                else:
                    metadata.write_text(text)
                self.assert_no_attributed_mask(job)

    def test_staging_timestamp_must_belong_to_the_recorded_attempt(self):
        for kind in ('before-start', 'after-finish', 'future'):
            with self.subTest(kind=kind):
                case = self.case('time-' + kind)
                job = self.add(case)
                metadata = self.stage(case, job)
                if kind == 'after-finish':
                    with self.store.transaction() as con:
                        current = self.store.get(con, job['id'])
                        current.update(status='Done', finished_at=job['enqueued_at'])
                        self.store.save(con, current)
                else:
                    value = job['enqueued_at'] - 60 if kind == 'before-start' else time.time() + 60
                    lines = metadata.read_text().splitlines()
                    metadata.write_text('\n'.join('staging_start_utc: ' + timestamp(value)
                        if line.startswith('staging_start_utc:') else line for line in lines) + '\n')
                self.assert_no_attributed_mask(job)

    def test_preexisting_output_and_replaced_output_parent_do_not_gain_proof(self):
        for kind in ('preexisting', 'parent-replaced'):
            with self.subTest(kind=kind):
                case = self.case('output-' + kind)
                if kind == 'preexisting':
                    case['output'].mkdir()
                job = self.add(case)
                self.assertEqual(job['status'], 'Hold')
                if kind == 'parent-replaced':
                    parent = case['output'].parent
                    parent.rename(parent.with_name('former-runs'))
                    parent.mkdir()
                self.stage(case, job)
                self.assert_no_attributed_mask(job)

    def test_output_directory_replacement_during_metadata_read_fails_closed(self):
        case = self.case('directory-race')
        job = self.add(case)
        self.stage(case, job)
        original_snapshot = input_sources._snapshot
        replacement = case['output'].with_name('original-attempt')

        def replace_after_metadata(path, role, limit, **kwargs):
            result = original_snapshot(path, role, limit, **kwargs)
            if Path(path).name == 'RUN_METADATA.txt':
                case['output'].rename(replacement)
                shutil.copytree(replacement, case['output'])
            return result

        with patch.object(input_sources, '_snapshot', side_effect=replace_after_metadata):
            context = resolve_context(self.store, job['id'])
        self.assertNotIn('poscar', {source.role for source in context.sources})
        self.assertTrue(any('directory identity changed' in item.lower() for item in context.limitations))
        snapshot = self.observe(job, context=context)
        self.assertNotIn('free', self.forces(snapshot))

    def test_new_attempt_with_changed_input_preserves_historical_staged_identity(self):
        case = self.case('historical')
        first = self.add(case)
        self.stage(case, first)
        with self.store.transaction() as con:
            current = self.store.get(con, first['id'])
            current.update(status='Done', finished_at=time.time())
            self.store.save(con, current)
        old_bytes = (case['output'] / 'POSCAR').read_bytes()
        original = deepcopy(first['progress_sources'])
        (case['input'] / 'POSCAR').write_text(poscar_text(flags=((False,) * 3, (True,) * 3)))
        second_case = dict(case, output=case['output'].with_name('second-attempt'))
        second = self.add(second_case)
        self.stage(second_case, second)
        old, new = self.observe(first), self.observe(second)
        self.assertNotEqual(old.context.attempt_id, new.context.attempt_id)
        self.assertNotEqual(self.staged_source(old.context).expected_sha256,
                            self.staged_source(new.context).expected_sha256)
        self.assertEqual(self.forces(old)['free'].value, .005)
        self.assertEqual(self.forces(new)['free'].value, 5)
        self.assertEqual((case['output'] / 'POSCAR').read_bytes(), old_bytes)
        self.assertEqual(self.read_job(first)['progress_sources'], original)

    def test_identical_contents_do_not_merge_distinct_attempts(self):
        first_case, second_case = self.case('same-one'), self.case('same-two')
        first, second = self.add(first_case), self.add(second_case)
        self.stage(first_case, first)
        self.stage(second_case, second)
        first_context = resolve_context(self.store, first['id'])
        second_context = resolve_context(self.store, second['id'])
        first_source, second_source = self.staged_source(first_context), self.staged_source(second_context)
        self.assertEqual(first_source.expected_sha256, second_source.expected_sha256)
        self.assertNotEqual(first_source.path, second_source.path)
        self.assertNotEqual(first_context.key, second_context.key)
        with self.assertRaises(JobsError):
            resolve_context(self.store, first['id'], expected_attempt_id=second['attempt_id'])

    def test_binding_survives_reload_and_cannot_be_replaced_by_lifecycle_save(self):
        case = self.case('reload')
        job = self.add(case)
        original = deepcopy(job['progress_sources'])
        self.stage(case, job)
        self.store = Store(self.store.root)
        self.staged_source(resolve_context(self.store, job['id']))
        with self.store.transaction() as con:
            current = self.store.get(con, job['id'])
            current.pop('progress_sources')
            self.store.save(con, current)
        self.assertEqual(self.read_job(job)['progress_sources'], original)
        before = self.store.path.read_bytes()
        with self.assertRaises(JobsError):
            with self.store.transaction() as con:
                current = self.store.get(con, job['id'])
                current['progress_sources']['vasp_input']['sha256'] = '0' * 64
                self.store.save(con, current)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_legacy_and_opaque_jobs_never_acquire_a_persisted_proof(self):
        case = self.case('legacy')
        job = self.add(case)
        self.stage(case, job)
        # Model an old persisted record directly; production APIs forbid backfill.
        with self.store.transaction() as con:
            legacy = self.store.get(con, job['id'])
            legacy.pop('progress_sources')
            con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(legacy), job['id']))
        before = self.store.path.read_bytes()
        context = resolve_context(self.store, job['id'])
        self.assertFalse(any('attempt-staged-input' in source.origins for source in context.sources))
        self.assertTrue(all(source.expected_sha256 is None for source in context.sources))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertNotIn('progress_sources', self.read_job(job))
        opaque = self.add(case, argv=['/bin/bash', '-c', 'never-executed wrapper'])
        opaque_context = resolve_context(self.store, opaque['id'])
        self.assertNotIn('progress_sources', opaque)
        self.assertEqual(opaque_context.binding_status, 'SOURCE_UNRESOLVED')
        self.assertFalse(opaque_context.sources)

    def test_explicit_source_cannot_rescue_failed_automatic_staging_proof(self):
        case = self.case('explicit-staged')
        sources = (SourceDescriptor('outcar', str(case['output'] / 'OUTCAR')),
                   SourceDescriptor('oszicar', str(case['output'] / 'OSZICAR')),
                   SourceDescriptor('poscar', str(case['output'] / 'POSCAR'), optional=True))
        job = self.add(case, sources=sources)
        metadata = self.stage(case, job)
        snapshot = self.observe(job)
        source = next(source for source in snapshot.context.sources if source.role == 'poscar')
        self.assertIn('attempt-staged-input', source.origins)
        self.assertEqual(source.expected_sha256, job['progress_sources']['vasp_input']['sha256'])
        self.assertEqual(self.forces(snapshot)['free'].value, .005)
        metadata.write_text(metadata.read_text().replace('state: RUNNING\n', ''))
        self.assert_no_attributed_mask(job)
        # An explicit observation remains local and cannot recreate a staged proof.
        local = resolve_context(self.store, job['id'], sources=sources)
        local_source = next(source for source in local.sources if source.role == 'poscar')
        self.assertEqual(local_source.origins, ('invocation-local',))
        self.assertIsNone(local_source.expected_sha256)
        self.assertTrue(any('historical execution binding is unverified' in item for item in local.limitations))
        self.assertEqual(self.observe(job, context=local).scientific_validation, 'not-assessed')

    def test_conflicting_explicit_poscar_is_not_silently_overridden(self):
        case = self.case('conflict')
        other = self.root / 'another.POSCAR'
        other.write_text(poscar_text())
        sources = (SourceDescriptor('outcar', str(case['output'] / 'OUTCAR')),
                   SourceDescriptor('oszicar', str(case['output'] / 'OSZICAR')),
                   SourceDescriptor('poscar', str(other), optional=True))
        job = self.add(case, sources=sources)
        self.stage(case, job)
        snapshot = self.observe(job)
        self.assertNotIn('poscar', {source.role for source in snapshot.context.sources})
        self.assertNotIn('free', self.forces(snapshot))
        self.assertTrue(any('poscar' in item.lower() and 'conflict' in item.lower()
                            for item in snapshot.context.limitations))


if __name__ == '__main__':
    unittest.main()
