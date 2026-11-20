"""Backfill of terminated legacy jobs (#41 Phases 4b-4c, #76); synthetic queues, never a runner or VASP."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import manifest_backfill, manifest_check, manifest_legacy, manifest_list, manifests
from cmw.jobs.manifests import ManifestError
from cmw.jobs.store import Store
from tests.jobs.test_catalogue_cli import run_jobs
from tests.jobs.test_manifests import Draft202012Validator, ManifestCase, schema_errors, synthetic_potcar
from tests.periodic.vasp.runner_case import timestamp

CONTRACTS = Path(__file__).resolve().parents[1] / 'fixtures' / 'manifests' / 'parser-contracts'
VOLUME = '11111111-2222-3333-4444-555555555555'
INPUTS = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')
QUEUE = 'node-a/project-queue'
MANIFEST = '.cmw-manifest'


def volume(path):
    return VOLUME


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BackfillCase(ManifestCase):
    def setUp(self):
        super().setUp()
        self.init()
        blocker = patch('subprocess.Popen', side_effect=AssertionError('No runner, VASP or hasher process'))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.legacy = Store(self.base / 'legacy')
        self.runs = self.base / 'runs'
        self.runs.mkdir()

    def filler(self):
        return self.legacy.add(argv=['/bin/true'], cwd=self.runs, name='filler')

    def vasp(self, contract, *, checkpoint=None, restart_from=None, status='Done'):
        """A managed legacy VASP job with the runner's documented staging copy and record, never run."""
        wrapper = self.runs / f'job-{uuid4().hex[:8]}'
        inputs, output = wrapper / 'inputs', wrapper / 'run'
        inputs.mkdir(parents=True)
        (inputs / 'INCAR').write_text(f'ENCUT = 400\nSYSTEM = {wrapper.name}\n')
        (inputs / 'POSCAR').write_text('Si\n1.0\n5 0 0\n0 5 0\n0 0 5\nSi\n2\nDirect\n0 0 0\n0.25 0.25 0.25\n')
        (inputs / 'KPOINTS').write_text('Gamma\n0\nGamma\n1 1 1\n')
        (inputs / 'POTCAR').write_text(synthetic_potcar('Si'))
        restart = 'none' if restart_from is None else 'wavecar'
        if restart_from is not None:
            shutil.copyfile(restart_from, inputs / 'WAVECAR')
        argv = ['/unexecuted/run-vasp.sh', '--input', str(inputs), '--output', str(output), '--binary', 'std',
                '--ranks', '2', '--ncore', '1', '--kpar', '1', '--mpi-mode', 'synthetic', '--restart', restart,
                '--timeout', '10', '--stop-before', '0', '--managed-foreground']
        job = self.legacy.add(argv=argv, cwd=wrapper, name='legacy', engine='VASP', mpi_ranks=2,
                              env={'OMP_NUM_THREADS': '1', 'SECRET': 'x'})
        self.assertIsNone(job['progress_sources']['vasp_input']['issue'])
        output.mkdir()
        names = INPUTS + (('WAVECAR',) if restart_from is not None else ())
        for name in names:
            shutil.copyfile(inputs / name, output / name)
        started = time.time() - 10
        lines = ['state: STAGING', 'child_status: NOT STARTED', 'staging_start_utc: ' + timestamp(started + 1),
                 'source: ' + str(inputs), 'output: ' + str(output), 'managed_foreground: True',
                 'restart_requested: ' + restart, 'requested_version: 6.6.1',
                 'original_incar_sha256: ' + digest(output / 'INCAR')]
        for name in names:
            info = (inputs / name).stat()
            lines.append(f'staged_file: {name}; bytes={info.st_size}; source_stat=({info.st_dev}, {info.st_ino}, '
                         f'{info.st_size}, {info.st_mtime_ns}); SHA256={digest(output / name)}')
        lines.extend(('effective_incar_sha256: ' + digest(output / 'INCAR'), 'state: RUNNING'))
        (output / 'RUN_METADATA.txt').write_text('\n'.join(lines) + '\n')
        contract = json.loads((CONTRACTS / f'{contract}.json').read_text())
        (output / 'OUTCAR').write_text('\n'.join(contract['OUTCAR_lines']) + '\n')
        if contract['stdout_lines']:
            (output / 'stdout.log').write_text('\n'.join(contract['stdout_lines']) + '\n')
        if checkpoint is not None:
            (output / 'WAVECAR').write_bytes(checkpoint)
        terminal = status in {'Done', 'Fail', 'Cancelled'}
        with self.legacy.transaction() as con:
            record = self.legacy.get(con, job['id'])
            record.update(status=status, started_at=started, finished_at=started + 5 if terminal else None,
                          exit_code=0 if status == 'Done' else None)
            self.legacy.save(con, record)
        return job['id'], wrapper

    def bind(self):
        manifest_legacy.bind_queue(self.store, self.project, node='node-a', queue='project-queue',
                                   state_root=self.legacy.root, volume_uuid_of=volume)

    def inventory(self, *entries, **changes):
        path = self.base / f'inventory-{uuid4().hex[:6]}.json'
        path.write_text(json.dumps({'schema_version': 1, 'project_key': 'demo', 'jobs': list(entries), **changes}))
        return path

    @staticmethod
    def entry(number, wrapper, *labels):
        return {'reference': f'{QUEUE}:J{number}.1', 'wrapper': str(wrapper), 'labels': list(labels)}

    def plan(self, path, store=None):
        return manifest_backfill.plan(store or self.store, self.project, path, volume_uuid_of=volume)

    @staticmethod
    def row(report, number):
        return next(row for row in report['jobs'] if row['reference'] == f'{QUEUE}:J{number}.1')

    def j27(self):
        """Queue jobs J16 (parent), J20 (same-size decoy) and J27 (LABORT restart from J16) with an inventory."""
        for _ in range(15):
            self.filler()
        _, parent = self.vasp('converged-at-nelm', checkpoint=b'parent checkpoint')
        for _ in range(3):
            self.filler()
        _, decoy = self.vasp('converged-at-nelm', checkpoint=b'decoys checkpoint')  # Same size.
        for _ in range(6):
            self.filler()
        number, child = self.vasp('labort-outcar', restart_from=parent / 'run' / 'WAVECAR')
        self.assertEqual(number, 27)
        self.bind()
        wrappers = {16: parent, 20: decoy, 27: child}
        inventory = self.inventory(self.entry(27, child, 'J27.1'), self.entry(16, parent, 'J10.7'), self.entry(20, decoy))
        return wrappers, inventory

    def tree(self):
        return {str(path): (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns)
                for path in self.base.rglob('*') if not path.name.startswith('inventory-')}


class BackfillDryRunTests(BackfillCase):
    def test_j27_fixture_is_reported_done_labort_with_its_inferred_parent_and_nothing_is_written(self):
        wrappers, path = self.j27()
        parent_wrapper, decoy_wrapper, child_wrapper = wrappers[16], wrappers[20], wrappers[27]
        before, journal = self.tree(), (self.project / '.cmw' / 'integrity.jsonl').read_bytes()
        report = self.plan(path)
        self.assertEqual(self.tree(), before)  # Nothing in the project, queue or runs is created or changed.
        self.assertEqual((self.project / '.cmw' / 'integrity.jsonl').read_bytes(), journal)
        self.assertEqual((report['mode'], report['written'], report['totals']),
                         ('dry-run', False, {'ready': 3, 'conflict': 0, 'deferred': 0}), report)
        self.assertEqual([row['reference'] for row in report['jobs']], [f'{QUEUE}:J{n}.1' for n in (16, 20, 27)])
        row = self.row(report, 27)
        job, state = row['documents']['job'], row['documents']['state']
        self.assertEqual((job['id'], job['provenance'], state['status'], state['native_status'], state['scf_result']),
                         (f'{QUEUE}:J27.1', 'backfilled', 'done', 'Done', 'labort'))
        self.assertEqual((job['parent'], job['parent_inferred']), (f'{QUEUE}:J16.1', True))
        self.assertEqual(job['restart_inputs'], {'WAVECAR': digest(parent_wrapper / 'run' / 'WAVECAR')})
        self.assertEqual(job['inputs'], {name: digest(child_wrapper / 'inputs' / name)
                                         for name in ('INCAR', 'POSCAR', 'KPOINTS')})
        self.assertEqual((job['launch']['mpi_ranks'], job['launch']['environment']), (2, {'OMP_NUM_THREADS': '1'}))
        self.assertEqual((job['authoring_node'], job['potcar'], state['binding'], state['writer_node']),
                         (None, None, None, None))
        self.assertEqual(row['fields']['job.parent']['state'], 'evidence')
        self.assertEqual(row['fields']['destination']['value'], str(child_wrapper / '.cmw-manifest'))
        self.assertEqual(row['aliases'], [{'label': 'J27.1', 'target': f'{QUEUE}:J27.1'}])
        self.assertEqual(self.row(report, 16)['aliases'], [{'label': 'J10.7', 'target': f'{QUEUE}:J16.1'}])
        # Same-size outputs were hashed to prove uniqueness; the decoy's bytes differ.
        self.assertEqual(self.row(report, 16)['documents']['state']['restart_outputs'],
                         {'WAVECAR': digest(parent_wrapper / 'run' / 'WAVECAR')})
        self.assertEqual(self.row(report, 20)['documents']['state']['restart_outputs'],
                         {'WAVECAR': digest(decoy_wrapper / 'run' / 'WAVECAR')})
        self.assertEqual(self.row(report, 16)['fields']['job.parent']['state'], 'missing')
        self.assertNotIn('SECRET', json.dumps(report))
        for item in report['jobs']:
            self.assertEqual({field['state'] for field in item['fields'].values()} - {'evidence', 'missing'}, set())
            if Draft202012Validator is not None:
                for name, document in item['documents'].items():
                    self.assertEqual(schema_errors(f'{name}.v1.json', document), [], (item['reference'], name))
                for alias in item['aliases']:
                    receipt = {'schema_version': 1, 'sequence': 2, 'request_uuid': uuid4().hex,
                               'event': 'legacy_alias', 'timestamp': '2026-11-20T00:00:00Z', 'project': 'demo',
                               'payload': alias}
                    self.assertEqual(schema_errors('legacy-alias.v1.json', receipt), [])
        text = manifest_backfill.summary_text(report)
        self.assertIn('3 ready, 0 conflict, 0 deferred; nothing was written', text)
        self.assertIn(f'parent {QUEUE}:J16.1', text)

    def test_parents_need_one_comparable_match_for_every_staged_restart_file(self):
        first, first_wrapper = self.vasp('converged-at-nelm', checkpoint=b'shared checkpoint')
        second, second_wrapper = self.vasp('converged-at-nelm', checkpoint=b'shared checkpoint')
        child, child_wrapper = self.vasp('labort-outcar', restart_from=first_wrapper / 'run' / 'WAVECAR')
        orphan_source = self.base / 'elsewhere.WAVECAR'
        orphan_source.write_bytes(b'not an inventory output')
        orphan, orphan_wrapper = self.vasp('labort-outcar', restart_from=orphan_source)
        self.bind()
        entries = [self.entry(first, first_wrapper), self.entry(second, second_wrapper),
                   self.entry(child, child_wrapper), self.entry(orphan, orphan_wrapper)]
        report = self.plan(self.inventory(*entries))
        ambiguous = self.row(report, child)
        self.assertEqual(ambiguous['state'], 'conflict')
        self.assertEqual(ambiguous['fields']['job.parent']['state'], 'conflict')
        self.assertIn(f"'{QUEUE}:J1.1', '{QUEUE}:J2.1'", ambiguous['fields']['job.parent']['detail'])
        self.assertIsNone(ambiguous['documents']['job']['parent'])
        unmatched = self.row(report, orphan)  # No inventory output matches: missing, not a guess.
        self.assertEqual((unmatched['state'], unmatched['fields']['job.parent']['state']), ('ready', 'missing'))
        real = manifest_backfill.stable_digest

        def unstable(path):
            return (None, 'changed while it was hashed') if second_wrapper in path.parents else real(path)
        with patch.object(manifest_backfill, 'stable_digest', unstable):
            report = self.plan(self.inventory(*entries[1:3], self.entry(first, first_wrapper)))
        field = self.row(report, child)['fields']['job.parent']
        self.assertEqual(field['state'], 'conflict')
        self.assertIn('changed while it was hashed', field['detail'])
        self.assertEqual(self.row(report, second)['fields']['state.restart_outputs.WAVECAR']['state'], 'missing')

    def test_a_replaced_donor_wrapper_is_compared_but_never_becomes_a_parent(self):
        donor, donor_wrapper = self.vasp('converged-at-nelm', checkpoint=b'donor checkpoint')
        child, child_wrapper = self.vasp('labort-outcar', restart_from=donor_wrapper / 'run' / 'WAVECAR')
        self.bind()
        copy = self.base / 'copy'
        shutil.copytree(donor_wrapper, copy)
        shutil.rmtree(donor_wrapper)
        os.rename(copy, donor_wrapper)  # Same path and bytes, a different wrapper directory.
        report = self.plan(self.inventory(self.entry(donor, donor_wrapper), self.entry(child, child_wrapper)))
        field = self.row(report, child)['fields']['job.parent']
        self.assertEqual((self.row(report, child)['state'], field['state']), ('conflict', 'conflict'))
        self.assertIn('not verified', field['detail'])
        self.assertIsNone(self.row(report, child)['documents']['job']['parent'])
        replaced = self.row(report, donor)
        self.assertIn('output parent identity changed', replaced['fields']['job.inputs.INCAR']['detail'])
        state = replaced['documents']['state']
        self.assertEqual((state['scf_result'], state['restart_outputs']), ('unknown', {}))
        self.assertIn('not attributed', state['evidence'][0]['marker'])

    def test_active_unresolved_and_unsafe_jobs_are_deferred_or_in_conflict(self):
        running, running_wrapper = self.vasp('converged-at-nelm', status='Run')
        done, done_wrapper = self.vasp('converged-at-nelm')
        command = self.filler()
        with self.legacy.transaction() as con:
            record = self.legacy.get(con, command['id'])
            record.update(status='Fail', started_at=time.time() - 2, finished_at=time.time() - 1, exit_code=3)
            self.legacy.save(con, record)
        unverified, unverified_wrapper = self.vasp('converged-at-nelm')
        (unverified_wrapper / 'run' / 'RUN_METADATA.txt').unlink()
        self.bind()
        link = self.base / 'link'
        link.symlink_to(done_wrapper)
        cases = [
            (self.entry(running, running_wrapper), 'deferred', 'not terminal'),
            ({**self.entry(1, running_wrapper), 'reference': 'node-a/other:J1.1'}, 'deferred', 'no binding'),
            ({**self.entry(1, self.base / 'missing'), 'reference': f'{QUEUE}:J99.1'}, 'deferred', 'no job J99.1'),
            (self.entry(done, link), 'conflict', 'symlink'),
            (self.entry(done, self.runs), 'conflict', 'neither'),
        ]
        for entry, state, message in cases:
            with self.subTest(entry=entry):
                row = self.plan(self.inventory(entry))['jobs'][0]
                self.assertEqual(row['state'], state, row)
                self.assertRegex(' '.join(row['reasons']), message)
        (done_wrapper / '.cmw-manifest').mkdir()
        row = self.plan(self.inventory(self.entry(done, done_wrapper)))['jobs'][0]
        self.assertRegex(' '.join(row['reasons']), 'already exists; backfill never overwrites')
        (done_wrapper / '.cmw-manifest').rmdir()
        key = manifests.load_project(self.project)[1]['project_key']
        manifests.append_journal(self.project, key, 'manifest', uuid4().hex,
                                 {'id': f'{QUEUE}:J{done}.1', 'job_sha256': '0' * 64, 'binding': None})
        row = self.plan(self.inventory(self.entry(done, done_wrapper)))['jobs'][0]
        self.assertRegex(' '.join(row['reasons']), 'manifest receipt already exists')
        # Missing evidence is reported as missing, never filled in: a command job and unverified staging.
        report = self.plan(self.inventory(self.entry(command['id'], self.runs),
                                          self.entry(unverified, unverified_wrapper)))
        failed, unbound = self.row(report, command['id']), self.row(report, unverified)
        self.assertEqual((failed['state'], failed['documents']['state']['status'],
                          failed['documents']['state']['exit_code'], failed['documents']['job']['engine']),
                         ('ready', 'failed', 3, 'Command'))
        self.assertEqual((unbound['state'], unbound['fields']['job.inputs.INCAR']['state']), ('ready', 'missing'))
        self.assertIn('RUN_METADATA.txt', unbound['fields']['job.inputs.INCAR']['detail'])
        self.assertEqual(unbound['documents']['job']['inputs'], dict.fromkeys(('INCAR', 'POSCAR', 'KPOINTS')))

    def test_malformed_inventories_and_non_owners_are_refused(self):
        done, wrapper = self.vasp('converged-at-nelm')
        self.bind()
        good = self.entry(done, wrapper)
        cases = [
            ({'jobs': []}, 'nonempty list'),
            ({'project_key': 'other'}, 'project key'),
            ({'complete': True}, 'exactly schema_version'),
            ({'jobs': [{**good, 'reference': 'J1.1'}]}, 'queue-qualified'),
            ({'jobs': [{**good, 'reference': 'node-a:J1.1'}]}, 'queue-qualified'),
            ({'jobs': [{**good, 'wrapper': 'runs/job'}]}, 'absolute'),
            ({'jobs': [{**good, 'labels': ['J1', 'J1']}]}, 'distinct'),
            ({'jobs': [{**good, 'labels': ['bad\nlabel']}]}, 'control'),
            ({'jobs': [good, good]}, 'listed twice'),
            ({'jobs': [good, {**good, 'reference': f'{QUEUE}:J9.1'}]}, 'two jobs'),
            ({'jobs': [{**good, 'extra': 1}]}, 'exactly reference'),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ManifestError, message):
                self.plan(self.inventory(good, **changes))
        stranger = Store(self.base / 'stranger')
        stranger.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        with self.assertRaisesRegex(ManifestError, 'does not own'):
            self.plan(self.inventory(good), store=stranger)

    def test_cli_reports_and_exits_by_readiness(self):
        done, wrapper = self.vasp('converged-at-nelm')
        running, running_wrapper = self.vasp('converged-at-nelm', status='Run')
        self.bind()
        state = str(self.store.root)
        with patch.object(manifest_legacy, 'observe_volume_uuid', volume):
            code, output = run_jobs(['backfill', '--manifest-project', str(self.project), '--inventory',
                                     str(self.inventory(self.entry(done, wrapper))), '--json'], state)
            self.assertEqual((code, json.loads(output)['totals']['ready']), (0, 1), output)
            code, output = run_jobs(['backfill', '--manifest-project', str(self.project), '--inventory',
                                     str(self.inventory(self.entry(done, wrapper),
                                                        self.entry(running, running_wrapper)))], state)
            self.assertEqual(code, 1, output)
            self.assertIn('1 ready, 0 conflict, 1 deferred; nothing was written', output)
            code, output = run_jobs(['backfill', '--manifest-project', str(self.project), '--inventory',
                                     str(self.inventory(self.entry(done, wrapper), project_key='other'))], state)
            self.assertEqual(code, 2, output)
        self.assertFalse((wrapper / '.cmw-manifest').exists())



class BackfillApplyTests(BackfillCase):
    def reviewed(self, inventory):
        report = self.plan(inventory)
        path = self.base / f'report-{uuid4().hex[:6]}.json'
        path.write_text(json.dumps(report))
        return report, path

    def apply(self, inventory, report):
        return manifest_backfill.apply(self.store, self.project, inventory, report, volume_uuid_of=volume)

    def check(self, *job_ids):
        return manifest_check.check(self.store, self.project, job_ids, volume_uuid_of=volume)

    def files(self):
        return {str(path): (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns)
                for path in self.base.rglob('*') if path.is_file() and '.cmw' not in path.parts
                and not path.name.startswith(('inventory-', 'report-')) and MANIFEST not in path.parts}

    def journal(self):
        return (self.project / '.cmw' / 'integrity.jsonl').read_bytes()

    def sidecars(self):
        return sorted(str(path) for path in self.base.rglob(MANIFEST))

    def test_reviewed_j27_fixture_is_published_once_and_covered_by_check_and_ls(self):
        wrappers, inventory = self.j27()
        report, path = self.reviewed(inventory)
        before = self.files()
        result = self.apply(inventory, path)
        self.assertEqual(self.files(), before)  # Every existing file is untouched; only sidecars are new.
        self.assertEqual([item['reference'] for item in result['applied']], [f'{QUEUE}:J{n}.1' for n in (16, 20, 27)])
        for item, row in zip(result['applied'], report['jobs']):
            sidecar = Path(item['destination'])
            self.assertEqual(sidecar, wrappers[int(item['reference'].split(':J')[1].split('.')[0])] / MANIFEST)
            self.assertEqual(item['published'], ['job.json', 'state.json', 'annotations.json'])
            for name, document, mode in manifest_backfill.SIDECAR:
                self.assertEqual((sidecar / name).read_bytes(), manifests.canonical(row['documents'][document]))
                self.assertEqual(os.stat(sidecar / name).st_mode & 0o777, mode)
        records = manifests.read_journal(self.project)
        self.assertEqual([record['event'] for record in records],
                         ['queue_binding', 'manifest', 'legacy_alias', 'manifest', 'manifest', 'legacy_alias'])
        self.assertEqual([record['payload'] for record in records if record['event'] == 'legacy_alias'],
                         [{'label': 'J10.7', 'target': f'{QUEUE}:J16.1'}, {'label': 'J27.1', 'target': f'{QUEUE}:J27.1'}])
        child = next(record for record in records if record['payload'].get('id') == f'{QUEUE}:J27.1')
        self.assertEqual(child['payload'], {'id': f'{QUEUE}:J27.1', 'binding': None, 'job_sha256': hashlib.sha256(
            (wrappers[27] / MANIFEST / 'job.json').read_bytes()).hexdigest()})
        if Draft202012Validator is not None:
            for record in records:
                self.assertEqual(schema_errors('integrity.v1.json', record), [], record)
        journal = self.journal()
        again = self.apply(inventory, path)  # A completed apply is a no-op.
        self.assertEqual(([item['published'] for item in again['applied']], self.journal()), ([[], [], []], journal))
        checked = self.check()
        self.assertEqual((checked['status'], [job['status'] for job in checked['jobs']]),
                         ('VERIFIED', ['verified'] * 3), checked)
        j27 = checked['jobs'][2]
        self.assertEqual((j27['id'], j27['state'], j27['scf_result'], j27['labels']),
                         (f'{QUEUE}:J27.1', 'done', 'labort', ['J27.1']))
        self.assertEqual(j27['lineage'], {'parent': f'{QUEUE}:J16.1', 'roles': {'WAVECAR': 'match'}})
        listing = manifest_list.list_jobs(self.store, self.project, volume_uuid_of=volume)
        self.assertEqual([row['id'] for row in listing['records']], [f'{QUEUE}:J{n}.1' for n in (16, 20, 27)])
        children = manifest_list.list_jobs(self.store, self.project, volume_uuid_of=volume, parent=f'{QUEUE}:J16.1')
        self.assertEqual([(row['id'], row['status'], row['scf_result'], row['integrity']) for row in children['records']],
                         [(f'{QUEUE}:J27.1', 'done', 'labort', 'verified')])
        after = self.plan(inventory)  # A new dry-run sees the published jobs, never republishes them.
        self.assertEqual(after['totals'], {'ready': 0, 'conflict': 3, 'deferred': 0})

    def test_apply_refuses_before_writing_when_anything_differs_from_the_review(self):
        wrappers, inventory = self.j27()
        report, path = self.reviewed(inventory)
        journal = self.journal()
        wrong = self.base / 'report-wrong.json'
        wrong.write_text(json.dumps({**report, 'mode': 'apply'}))
        smaller = self.inventory(self.entry(16, wrappers[16], 'J10.7'), self.entry(27, wrappers[27], 'J27.1'))
        foreign = wrappers[16] / MANIFEST
        foreign.mkdir()
        (foreign / 'notes.txt').write_text('not written by backfill')
        cases = [(inventory, wrong, '--json output of a backfill dry-run'), (smaller, path, 'different jobs'),
                 (inventory, path, 'holds notes.txt')]
        for chosen, reviewed, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ManifestError, message):
                self.apply(chosen, reviewed)
        (foreign / 'notes.txt').unlink()
        foreign.rmdir()
        (wrappers[20] / 'run' / 'WAVECAR').write_bytes(b'altered checkpoint')  # Same size, different bytes.
        with self.assertRaisesRegex(ManifestError, f'{QUEUE}:J20.1: its sources or documents changed'):
            self.apply(inventory, path)
        self.assertEqual((self.sidecars(), self.journal()), ([], journal))

    def test_an_interrupted_apply_resumes_and_jobs_not_ready_at_review_are_skipped(self):
        wrappers, _ = self.j27()
        running, running_wrapper = self.vasp('converged-at-nelm', status='Run')
        inventory = self.inventory(self.entry(16, wrappers[16], 'J10.7'), self.entry(running, running_wrapper))
        report, path = self.reviewed(inventory)
        self.assertEqual(report['totals'], {'ready': 1, 'conflict': 0, 'deferred': 1})
        real = manifest_backfill.append_journal

        def interrupted(root, key, event, *args, **kwargs):
            if event == 'manifest':
                raise OSError('synthetic interruption before the receipt')
            return real(root, key, event, *args, **kwargs)
        with patch.object(manifest_backfill, 'append_journal', interrupted), self.assertRaises(OSError):
            self.apply(inventory, path)
        self.assertEqual(sorted(entry.name for entry in (wrappers[16] / MANIFEST).iterdir()),
                         ['annotations.json', 'job.json', 'state.json'])
        journal = self.journal()
        for name in ('job.json', 'state.json'):  # A frozen file made writable is never anchored.
            frozen = wrappers[16] / MANIFEST / name
            frozen.chmod(0o644)
            with self.subTest(name=name), self.assertRaisesRegex(ManifestError, f'{name} is writable'):
                self.apply(inventory, path)
            frozen.chmod(0o444)
        self.assertEqual(self.journal(), journal)
        result = self.apply(inventory, path)
        self.assertEqual(([item['published'] for item in result['applied']], result['skipped']),
                         ([[]], [{'reference': f'{QUEUE}:J{running}.1', 'state': 'deferred'}]))
        self.assertFalse((running_wrapper / MANIFEST).exists())
        self.assertEqual(self.check()['status'], 'VERIFIED')

    def test_check_reports_edited_writable_malformed_and_unresolvable_backfilled_records(self):
        wrappers, inventory = self.j27()
        self.apply(inventory, self.reviewed(inventory)[1])
        job, state = wrappers[27] / MANIFEST / 'job.json', wrappers[16] / MANIFEST / 'state.json'
        job.chmod(0o644)
        job.write_bytes(job.read_bytes() + b' ')
        state.chmod(0o644)
        result = self.check()
        codes = {item['id']: {finding['code'] for finding in item['findings']} for item in result['jobs']}
        self.assertEqual(result['status'], 'FAILED')
        self.assertLessEqual({'job_json_writable', 'job_json_changed'}, codes[f'{QUEUE}:J27.1'])
        self.assertIn('state_writable', codes[f'{QUEUE}:J16.1'])
        listed = manifest_list.list_jobs(self.store, self.project, volume_uuid_of=volume)['records']
        self.assertIsNone(next(row for row in listed if row['id'] == f'{QUEUE}:J16.1')['status'])
        moved = manifest_check.check(self.store, self.project, volume_uuid_of=lambda path: VOLUME.replace('1', '9'))
        self.assertEqual({item['status'] for item in moved['jobs']}, {'unverifiable'})
        self.assertIn('legacy_unresolved', {finding['code'] for finding in moved['jobs'][0]['findings']})
        with self.assertRaisesRegex(ManifestError, 'queue-qualified'):
            self.check('node-a:J27.1')
        key = manifests.load_project(self.project)[1]['project_key']
        manifests.append_journal(self.project, key, 'legacy_alias', uuid4().hex, {'label': 'J1', 'target': 'J1.1'})
        damage = [item['message'] for item in self.check()['findings'] if item['code'] == 'journal_damaged']
        self.assertIn('malformed legacy_alias receipt', damage[0])

    def test_cli_applies_a_saved_report_and_checks_qualified_ids(self):
        _, inventory = self.j27()
        state = str(self.store.root)
        with patch.object(manifest_legacy, 'observe_volume_uuid', volume):
            code, output = run_jobs(['backfill', '--manifest-project', str(self.project), '--inventory',
                                     str(inventory), '--json'], state)
            report = self.base / 'report-cli.json'
            report.write_text(output)
            base = ['backfill', '--manifest-project', str(self.project), '--inventory', str(inventory)]
            for extra in (['--apply'], ['--report', str(report)]):
                self.assertEqual(run_jobs([*base, *extra], state)[0], 2)
            code, output = run_jobs([*base, '--apply', '--report', str(report)], state)
            self.assertEqual(code, 0, output)
            self.assertIn('3 reviewed job(s) published', output)
            code, output = run_jobs(['check', '--manifest-project', str(self.project), f'{QUEUE}:J27.1', '--json'],
                                    state)
            self.assertEqual((code, json.loads(output)['jobs'][0]['status']), (0, 'verified'), output)

if __name__ == '__main__':
    unittest.main()
