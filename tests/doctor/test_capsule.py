"""Synthetic support exports cannot import research files, logs or archives."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cmw.doctor.capsule import export_capsule, project_capsule


GUIDANCE = {'SOURCE_PENDING': 'Inspect the declared source status with a read-only check.'}


def report():
    private_path = '/private/researcher/project name/calculation/output.log'
    target = {'state': '/private/researcher/state', 'node_id': 'secret-host-name',
              'queue_id': 'private-queue-id', 'job_id': 'private-job-id',
              'attempt_id': 'private-attempt-id', 'engine': 'VASP', 'status': 'Run'}
    return {'schema': 1, 'scope': 'job', 'captured_at': 1234.5,
        'qualification': 'Evidence from private researcher', 'target': target,
        'runtime': {'identity': {'algorithm': 'cmw-runtime-sha256-v1',
            'fingerprint': 'a' * 64, 'package_sha256': 'b' * 64,
            'version': '0.1.0', 'python': '3.14.0 (private compiler path)',
            'package_origin': private_path, 'executable': '/private/researcher/bin/python',
            'dependencies': {'textual': '6.12.0', 'numpy': '2.1.0', 'secret-dependency': 'secret-value'}},
            'origins': {'package': private_path},
            'build_source': {'status': 'UNKNOWN', 'commit': None, 'root': private_path}},
        'worker': {'status': 'UNKNOWN', 'runtime': None},
        'findings': [{'code': 'SOURCE_PENDING', 'domain': 'binding', 'scope': 'job',
            'severity': 'WARNING', 'capability': 'progress',
            'summary': 'Source pending at ' + private_path,
            'next_check': GUIDANCE['SOURCE_PENDING'],
            'evidence': {'path': private_path, 'attempt_id': target['attempt_id'],
                         'detail': 'token=secret-value', 'argv': ['private-credential-value'],
                         'nested_archive': {'content': 'never serialize this'}},
            'caused_by': None}],
        'observation': {'status': 'PARTIAL', 'observed_at': 1230,
            'coverage': {'complete': False, 'bounded': True, 'bytes_read': 0},
            'transport': {'status': 'CACHED', 'node_id': target['node_id'], 'observed_at': 1200, 'stale': True},
            'progress': {'status': 'PENDING', 'sources': [{'role': 'stdout', 'path': private_path,
                'status': 'PENDING', 'last_success_at': None, 'bytes_read': 0}]}},
        'scientific_policy': 'NOT_ASSESSED',
        'env': {'API_TOKEN': 'never serialize env'}, 'argv': ['never serialize argv'],
        'raw_logs': 'never serialize logs', 'database': 'never serialize database',
        'coordinates': [[1, 2, 3]], 'CHGCAR': 'never serialize charge density'}


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scratch = self.root / 'Scratch'
        self.scratch.mkdir()
        self.destination = self.scratch / 'capsule'
        patcher = patch('cmw.doctor.capsule._finding_guidance', return_value=GUIDANCE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_projection_is_read_only_allowlisted_and_consistently_aliased(self):
        original = report()
        expected = deepcopy(original)
        with patch('builtins.open', side_effect=AssertionError('No file reads')), \
                patch('os.open', side_effect=AssertionError('No filesystem access')):
            snapshot = project_capsule(original)
        self.assertEqual(original, expected)
        data = snapshot.to_dict()
        text = snapshot.payload
        for secret in ('researcher', 'project name', 'secret-host-name', 'private-queue-id',
                       'private-job-id', 'private-attempt-id', 'secret-value', 'private-credential-value',
                       'never serialize', 'private compiler', 'coordinates', 'CHGCAR'):
            # Exclusion descriptions name forbidden payload categories, not their contents.
            if secret not in {'coordinates', 'CHGCAR'}:
                self.assertNotIn(secret, text)
        self.assertNotIn('env', data)
        self.assertNotIn('coordinates', data)
        source_alias = data['runtime']['identity']['package_origin']
        self.assertEqual(source_alias, data['runtime']['origins']['package'])
        self.assertEqual(source_alias, data['findings'][0]['evidence']['path'])
        self.assertEqual(source_alias, data['observation']['progress']['sources'][0]['path'])
        self.assertEqual(data['target']['attempt_id'], data['findings'][0]['evidence']['attempt_id'])
        self.assertEqual(data['target']['node_id'], data['observation']['transport']['node_id'])
        self.assertEqual(data['findings'][0]['code'], 'SOURCE_PENDING')
        self.assertEqual(data['findings'][0]['next_check'], GUIDANCE['SOURCE_PENDING'])
        self.assertEqual(data['runtime']['identity']['python'], '3.14.0')
        self.assertEqual(data['runtime']['identity']['fingerprint'], 'a' * 64)
        self.assertEqual(data['observation']['transport']['observed_at'], 1200)
        self.assertFalse(data['observation']['coverage']['complete'])
        self.assertEqual(list(self.scratch.iterdir()), [])
        original['target']['attempt_id'] = 'changed'
        self.assertNotIn('changed', snapshot.payload)

    def test_remote_ownership_clock_coverage_and_source_identity_are_retained(self):
        source = report()
        source['scope'] = 'node'
        source['caller_runtime'] = deepcopy(source['runtime'])
        source['endpoint_runtime'] = deepcopy(source['runtime'])
        source['endpoint_runtime']['identity']['fingerprint'] = 'c' * 64
        source['findings'][0]['scope'] = 'endpoint'
        source['target'].update(node_alias='private-host-alias', queue_alias='private-queue-alias', queue_schema=4)
        clock = {'node_id': source['target']['node_id'], 'queue_id': source['target']['queue_id'],
                 'session_id': 'private-session', 'clock_id': 'private-clock', 'sequence': 7, 'observed_at': 1200}
        source['observation']['transport'] = {**clock, 'status': 'fresh', 'received_at': 1234,
            'request_seconds': 0.5, 'cached': False,
            'qualification': 'Observation timestamps belong to the endpoint clock; receipt does not refresh source evidence.'}
        source['observation']['coverage'] = 'One bounded poll of explicitly selected sources; no log corpus or full replay.'
        source['observation']['binding'] = {'status': 'BOUND', 'binding_id': 'private-binding',
                                           'generation': 2, 'source_count': 1, 'declared_count': 1}
        source['observation']['progress']['sources'][0].update(identity=[123, 456], health='EMPTY', offset=0, has_more=False)
        source['findings'][0]['evidence']['identity'] = [123, 456]
        data = project_capsule(source).to_dict()
        transport = data['observation']['transport']
        self.assertEqual(transport['status'], 'fresh')
        self.assertEqual(transport['observed_at'], 1200)
        self.assertEqual(transport['received_at'], 1234)
        self.assertEqual(transport['sequence'], 7)
        self.assertEqual(transport['node_id'], data['target']['node_id'])
        self.assertEqual(transport['qualification'], source['observation']['transport']['qualification'])
        self.assertEqual(data['observation']['coverage'], source['observation']['coverage'])
        self.assertEqual(data['endpoint_runtime']['identity']['fingerprint'], 'c' * 64)
        self.assertEqual(data['caller_runtime']['identity']['fingerprint'], 'a' * 64)
        self.assertEqual(data['findings'][0]['scope'], 'endpoint')
        self.assertEqual(data['target']['queue_schema'], 4)
        self.assertEqual(data['observation']['binding']['declared_count'], 1)
        self.assertEqual(data['observation']['progress']['sources'][0]['identity'],
                         data['findings'][0]['evidence']['identity'])
        self.assertEqual(data['observation']['progress']['sources'][0]['health'], 'EMPTY')
        self.assertNotIn('private-host-alias', json.dumps(data))
        self.assertNotIn('private-clock', json.dumps(data))
        self.assertNotIn('private-binding', json.dumps(data))

    def test_unknown_codes_and_injected_guidance_are_not_trusted(self):
        source = report()
        source['findings'][0]['next_check'] += ' API_KEY=topsecret'
        source['findings'][0]['code'] = 'API_KEY_TOPSECRET'
        source['findings'][0]['caused_by'] = 'API_KEY_TOPSECRET'
        source['runtime']['identity']['version'] = '0.1.0+private-company-token'
        text = project_capsule(source).payload
        self.assertNotIn('TOPSECRET', text)
        self.assertNotIn('topsecret', text)
        self.assertNotIn('company', text)

    def test_private_modes_exact_hashes_and_no_clobber(self):
        manifest = export_capsule(report(), self.destination)
        files = sorted(path.name for path in self.destination.iterdir())
        self.assertEqual(files, ['doctor.json', 'manifest.json'])
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)
        for path in self.destination.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        content = (self.destination / 'doctor.json').read_bytes()
        self.assertEqual(manifest['files'], [{'name': 'doctor.json', 'bytes': len(content),
                                            'sha256': hashlib.sha256(content).hexdigest()}])
        self.assertEqual(json.loads((self.destination / 'manifest.json').read_text()), manifest)
        self.assertIn('not a full replay corpus', ' '.join(manifest['qualifications']))
        with self.assertRaises(FileExistsError):
            export_capsule({'schema': 1}, self.destination)
        self.assertEqual((self.destination / 'doctor.json').read_bytes(), content)

    def test_size_limits_reject_before_any_output(self):
        for limits in ({'per_file_limit': 100}, {'total_limit': 100},
                       {'per_file_limit': 1024 * 1024}, {'total_limit': 0},
                       {'total_limit': True}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                export_capsule(report(), self.destination, **limits)
            self.assertFalse(self.destination.exists())
        for field, value in (('findings', [{}] * 257), ('qualification', 'x' * 4097)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                export_capsule({**report(), field: value}, self.destination)
            self.assertFalse(self.destination.exists())

    def test_paths_must_be_explicit_safe_desktop_or_scratch_destinations(self):
        for destination in ('relative/capsule', self.root / 'unselected',
                            str(self.scratch) + '/../escape', str(self.scratch) + '/./escape'):
            # TMPDIR may itself be below Desktop; make the excluded home explicit.
            with patch('cmw.doctor.capsule.Path.home', return_value=self.root / 'fixture-home'), \
                    self.subTest(destination=destination), self.assertRaises(ValueError):
                export_capsule(report(), destination)
        with self.assertRaises(FileNotFoundError):
            export_capsule(report(), self.scratch / 'missing' / 'capsule')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_symlink_parents_and_leaf_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.scratch / 'parent-link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            export_capsule(report(), self.scratch / 'parent-link' / 'capsule')
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            export_capsule(report(), self.destination)
        self.assertEqual(list(outside.iterdir()), [])

    def test_archives_and_scientific_sources_are_never_opened_or_extracted(self):
        archive = self.scratch / 'nested.zip'
        archive.write_bytes(b'PK\x03\x04not an archive to read')
        source = report()
        source['observation']['progress']['sources'][0]['path'] = str(archive)
        source['archive'] = {'path': str(archive), 'members': {'../escape': 'secret contents'}}
        with patch('zipfile.ZipFile', side_effect=AssertionError('No archive access')), \
                patch('tarfile.open', side_effect=AssertionError('No archive access')):
            export_capsule(source, self.destination)
        text = (self.destination / 'doctor.json').read_text()
        self.assertNotIn(str(archive), text)
        self.assertNotIn('secret contents', text)
        self.assertFalse((self.root / 'escape').exists())
        self.assertEqual(archive.read_bytes(), b'PK\x03\x04not an archive to read')

    def test_invalid_schema_and_nonfinite_values_cannot_escape_projection(self):
        for schema in (2, True, '1', None):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                export_capsule({'schema': schema}, self.destination)
        source = report()
        source['captured_at'] = float('nan')
        source['observation']['observed_at'] = 10 ** 1000
        data = project_capsule(source).to_dict()
        self.assertIsNone(data['captured_at'])
        self.assertIsNone(data['observation']['observed_at'])

    def test_write_failure_removes_only_its_new_partial_bundle(self):
        original_open = os.open
        def broken_open(path, flags, *args, **kwargs):
            if path == 'manifest.json':
                raise OSError('synthetic write failure')
            return original_open(path, flags, *args, **kwargs)
        sentinel = self.scratch / 'sentinel'
        sentinel.write_text('preserve')
        with patch('cmw.doctor.capsule.os.open', side_effect=broken_open), self.assertRaises(OSError):
            export_capsule(report(), self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(sentinel.read_text(), 'preserve')


class CapsuleServiceProjectionTests(unittest.TestCase):
    def test_actual_selected_job_report_keeps_integer_selector_and_parser_context(self):
        from cmw.doctor.service import inspect_local
        from cmw.jobs.store import Store
        from cmw.progress.models import SourceDescriptor
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output, oszicar = root / 'OUTCAR', root / 'OSZICAR'
            output.write_text('')
            oszicar.write_text('')
            store = Store(root / 'state')
            job = store.add(argv=['/never/execute'], cwd=root, name='synthetic capsule', engine='VASP', hold=True,
                            progress_sources=(SourceDescriptor('outcar', str(output)),
                                              SourceDescriptor('oszicar', str(oszicar))))
            source = inspect_local(store.root, job['id'])
            self.assertIsInstance(source['target']['job_id'], int)
            source['findings'][0]['evidence']['job_id'] = job['id']
            before = store.path.read_bytes()
            projected = project_capsule(source).to_dict()
            self.assertEqual(store.path.read_bytes(), before)
            self.assertTrue(projected['target']['job_id'].startswith('<identity '))
            self.assertEqual(projected['target']['job_id'], projected['findings'][0]['evidence']['job_id'])
            native = source['observation']['progress']['source_diagnostics']
            self.assertTrue(native)
            retained = projected['observation']['progress']['source_diagnostics']
            for original, exported in zip(native, retained):
                for key in ('role', 'status', 'parsed_lines', 'complete_rows', 'usable_rows', 'last_complete_row_at'):
                    self.assertEqual(exported[key], original[key])
            self.assertEqual(projected['runtime']['commands'], source['runtime']['commands'])
            self.assertEqual(projected['runtime']['scientific_grammars'], source['runtime']['scientific_grammars'])
            self.assertEqual([item['name'] for item in projected['runtime']['assets']],
                             [item['name'] for item in source['runtime']['assets']])
            self.assertNotIn(str(root), json.dumps(projected))

    def test_node_guidance_and_shared_codes_preserve_both_trusted_variants(self):
        from cmw.doctor.nodes import NODE_FINDING_GUIDANCE
        from cmw.doctor.service import FINDING_GUIDANCE
        source = report()
        source['findings'] = [{'code': code, 'scope': scope, 'next_check': guidance[code]}
            for code, scope, guidance in (
                ('REMOTE_OBSERVATION_CACHED', 'endpoint', NODE_FINDING_GUIDANCE),
                ('ATTEMPT_CHANGED', 'endpoint', NODE_FINDING_GUIDANCE),
                ('ATTEMPT_CHANGED', 'job', FINDING_GUIDANCE))]
        projected = project_capsule(source).to_dict()
        self.assertEqual(projected['findings'], source['findings'])

    def test_known_source_roles_and_bounded_integer_aliases(self):
        source = report()
        source['target']['job_id'] = 23
        source['findings'][0]['evidence']['job_id'] = '23'
        source['observation']['progress']['source_diagnostics'] = [
            {'role': role, 'status': 'OK', 'parsed_lines': 7, 'complete_rows': 6,
             'usable_rows': 5, 'last_complete_row_at': 123.5}
            for role in ('incar', 'orca_output', 'multiwfn_session', 'multiwfn_events',
                         'multiwfn_validation', 'multiwfn_metadata')]
        projected = project_capsule(source).to_dict()
        self.assertEqual(projected['target']['job_id'], projected['findings'][0]['evidence']['job_id'])
        self.assertEqual(projected['observation']['progress']['source_diagnostics'],
                         source['observation']['progress']['source_diagnostics'])
        for invalid in (-1, 2 ** 64, True, 1.5):
            source['target']['job_id'] = invalid
            self.assertIsNone(project_capsule(source).to_dict()['target']['job_id'])


if __name__ == '__main__':
    unittest.main()
