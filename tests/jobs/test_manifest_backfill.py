"""Dry-run backfill of terminated legacy jobs (#41 Phase 4b, #76); synthetic queues, never a runner or VASP."""
import hashlib
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import manifest_backfill, manifest_legacy, manifests
from cmw.jobs.manifests import ManifestError
from cmw.jobs.store import Store
from tests.jobs.test_catalogue_cli import run_jobs
from tests.jobs.test_manifests import Draft202012Validator, ManifestCase, schema_errors, synthetic_potcar
from tests.periodic.vasp.runner_case import timestamp

CONTRACTS = Path(__file__).resolve().parents[1] / 'fixtures' / 'manifests' / 'parser-contracts'
VOLUME = '11111111-2222-3333-4444-555555555555'
INPUTS = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')
QUEUE = 'node-a/project-queue'


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

    def tree(self):
        return {str(path): (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns)
                for path in self.base.rglob('*') if not path.name.startswith('inventory-')}


class BackfillDryRunTests(BackfillCase):
    def test_j27_fixture_is_reported_done_labort_with_its_inferred_parent_and_nothing_is_written(self):
        for _ in range(15):
            self.filler()
        parent, parent_wrapper = self.vasp('converged-at-nelm', checkpoint=b'parent checkpoint')
        for _ in range(3):
            self.filler()
        decoy, decoy_wrapper = self.vasp('converged-at-nelm', checkpoint=b'decoys checkpoint')  # Same size.
        for _ in range(6):
            self.filler()
        child, child_wrapper = self.vasp('labort-outcar', restart_from=parent_wrapper / 'run' / 'WAVECAR')
        self.assertEqual((parent, decoy, child), (16, 20, 27))
        self.bind()
        path = self.inventory(self.entry(27, child_wrapper, 'J27.1'), self.entry(16, parent_wrapper, 'J10.7'),
                              self.entry(20, decoy_wrapper))
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
        self.assertIn('could not be compared', field['detail'])
        self.assertEqual(self.row(report, second)['fields']['state.restart_outputs.WAVECAR']['state'], 'missing')

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


if __name__ == '__main__':
    unittest.main()
