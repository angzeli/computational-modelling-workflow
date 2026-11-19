"""Owner-side manifest listing (#41 Phase 3c): numeric order, exact filters, integrity on every row."""
import json
import unittest

from cmw.jobs import manifest_annotations, manifest_list, manifests
from cmw.periodic.vasp import scf_classifier
from tests.jobs.test_catalogue_cli import run_jobs
from tests.jobs import test_manifest_check


class ManifestListTests(test_manifest_check.ManifestCheckTests):
    def populate(self):
        """J1 done parent, J2 restarted from it, J3 command job, J4–J11 reserved only."""
        self.lineage()
        self.command_job()
        for _ in range(8):
            manifests.reserve(self.store, self.project)
        manifest_annotations.annotate(self.store, self.project, 'J1', {'system': 'Si', 'outcome': 'reported'})
        manifest_annotations.annotate(self.store, self.project, 'J2', {'system': 'Si', 'outcome': 'superseded',
                                                                        'label': 'restart'})
        manifest_annotations.annotate(self.store, self.project, 'J3', {'system': 'GaAs'})

    def ids(self, **filters):
        return [row['id'] for row in manifest_list.list_jobs(self.store, self.project, **filters)['records']]

    def test_numeric_order_and_exact_filters(self):
        self.populate()
        result = manifest_list.list_jobs(self.store, self.project)
        self.assertEqual([row['id'] for row in result['records']], [f'J{number}' for number in range(1, 12)])
        self.assertEqual((result['integrity'], result['matching_count'], result['total_count']), ('VERIFIED', 11, 11))
        first = result['records'][0]
        self.assertEqual({key: first[key] for key in ('integrity', 'engine', 'status', 'system', 'outcome')},
                         {'integrity': 'verified', 'engine': 'VASP', 'status': 'done', 'system': 'Si',
                          'outcome': 'reported'})
        self.assertEqual(result['records'][1]['parent'], 'J1')
        self.assertEqual(result['records'][10]['integrity'], 'reserved')
        self.assertEqual(self.ids(system='Si'), ['J1', 'J2'])
        self.assertEqual(self.ids(system='Si', outcome='superseded'), ['J2'])
        self.assertEqual(self.ids(parent='J1'), ['J2'])
        self.assertEqual(self.ids(status='done'), ['J1'])
        # No synthetic job has bound OUTCAR evidence, so each published state is unclassified.
        self.assertEqual(self.ids(scf_result='unknown'), ['J1', 'J2', 'J3'])
        self.assertEqual(self.ids(scf_result='converged'), [])
        self.assertEqual(self.ids(system='Ge'), [])
        for filters in ({'status': 'Done'}, {'outcome': 'kept'}, {'parent': '1'}, {'system': ''}, {'owner': 'x'}):
            with self.subTest(filters=filters), self.assertRaises(manifests.ManifestError):
                manifest_list.list_jobs(self.store, self.project, **filters)

    def test_integrity_failures_stay_listed_but_contribute_no_fields(self):
        self.populate()
        path = self.project / 'jobs' / 'J1' / '.cmw-manifest' / 'annotations.json'
        edited = json.loads(path.read_text())
        edited['events'][0]['system'] = 'Ge'
        path.write_text(json.dumps(edited))
        result = manifest_list.list_jobs(self.store, self.project)
        row = result['records'][0]
        self.assertEqual((result['integrity'], row['integrity'], row['system'], row['errors']),
                         ('FAILED', 'failed', None, 1))
        self.assertEqual(self.ids(system='Ge'), [])
        self.assertEqual(self.ids(system='Si'), ['J2'])
        state_path = self.project / 'jobs' / 'J3' / '.cmw-manifest' / 'state.json'
        state = json.loads(state_path.read_text())
        state_path.write_text(json.dumps({**state, 'binding': {**state['binding'], 'attempt_id': 'f' * 32}}))
        row = manifest_list.list_jobs(self.store, self.project)['records'][2]
        self.assertEqual((row['integrity'], row['status'], row['scf_result'], row['system']),
                         ('failed', None, None, 'GaAs'))

    def test_listing_writes_nothing_and_cli_forms_are_explicit(self):
        self.populate()
        def files():
            return {path: path.stat().st_mtime_ns for path in self.project.rglob('*')
                    if path != self.project / '.cmw' and not path.name.endswith(('-wal', '-shm'))}
        before = files()
        code, output = run_jobs(['ls', '--manifest-project', str(self.project), '--system', 'Si', '--json'],
                                self.base / 'state')
        self.assertEqual((code, [row['id'] for row in json.loads(output)['records']]), (0, ['J1', 'J2']), output)
        code, output = run_jobs(['ls', '--manifest-project', str(self.project)], self.base / 'state')
        self.assertEqual(code, 0)
        listed = [line.split()[0] for line in output.splitlines()[1:]]
        self.assertEqual(listed, [f'J{number}' for number in range(1, 12)])
        self.assertEqual(files(), before)
        for arguments in (['ls'], ['ls', '--manifest-project', str(self.project), '--status', 'Done'],
                          ['ls', '--manifest-project', str(self.project), '--parent', 'J1.1']):
            with self.subTest(arguments=arguments):
                self.assertEqual(run_jobs(arguments, self.base / 'state')[0], 2)

    def test_vocabularies_match_their_sources(self):
        self.assertEqual(manifest_list.SCF_RESULTS, scf_classifier.RESULTS)
        self.assertEqual(manifest_list.STATUSES, ('queued', 'running', 'done', 'failed', 'cancelled'))


# The inherited check tests already run in test_manifest_check.
for name in [name for name in vars(test_manifest_check.ManifestCheckTests) if name.startswith('test_')]:
    setattr(ManifestListTests, name, None)


if __name__ == '__main__':
    unittest.main()
