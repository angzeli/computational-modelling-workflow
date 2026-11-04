"""Synthetic parsed bundle evidence only: no POTCAR reads or tar extraction."""
import copy
import hashlib
import json
import unittest

from cmw.jobs.multihost.bundle import (
    BundleError, IdempotencyAction, TarMemberHeader, decide_idempotency,
    plan_bundle, validate_file_payload, validate_manifest, validate_tar_headers,
)
from cmw.jobs.multihost.models import ContractError
from jobs.test_multihost_logic import capabilities


def manifest():
    payload = b'synthetic placeholder only\n'
    digest = hashlib.sha256(payload).hexdigest()
    return {'schema_version': '1.0', 'request_id': '12345678-1234-4234-8234-123456789abc',
            'source_node_id': 'mbp_m3max', 'created_utc': '2026-11-04T12:00:00.000Z',
            'engine': 'vasp', 'executable': 'vasp_std',
            'build': {'build_id': None, 'allow_experimental': False},
            'execution': {'mpi_ranks': 16, 'omp_threads': 1},
            'files': [{'path': 'inputs/' + name, 'size': len(payload), 'sha256': digest}
                      for name in ('INCAR', 'KPOINTS', 'POSCAR', 'POTCAR')],
            'provenance': {'preparation_record_sha256': 'a' * 64,
                           'potcar_identity': [{'element': 'Zn', 'variant': 'Zn',
                                                'titel': 'SYNTHETIC Zn', 'sha256': digest}]}}


def hello():
    return {'node_id': 'mm_m5pro', 'protocol_version': '1.0', 'cmw_version': '0.1.0',
            'cmw_commit': 'b' * 40, 'node_time_utc': '2026-11-04T12:00:00.000Z',
            'hardware': {'physical_cores': 18, 'core_tiers': [
                {'level': 0, 'name': 'Synthetic tier A', 'physical_cores': 12},
                {'level': 1, 'name': 'Synthetic tier B', 'physical_cores': 6}],
                'recommended_max_parallelism': None}, 'capabilities': capabilities()}


class BundleValidationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifest()
        self.manifest_size = len(json.dumps(self.manifest).encode())
        self.headers = [TarMemberHeader('manifest.json', self.manifest_size)] + [
            TarMemberHeader(item['path'], item['size']) for item in self.manifest['files']]
        self.total = self.manifest_size + sum(item['size'] for item in self.manifest['files'])

    def validate(self, headers=None, limit=None):
        return validate_tar_headers(self.manifest, self.headers if headers is None else headers,
                                    manifest_size=self.manifest_size,
                                    max_bundle_bytes=self.total if limit is None else limit)

    def test_exact_size_boundary_and_payload_identities(self):
        self.validate()
        for name in ('INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'):
            validate_file_payload(self.manifest, 'inputs/' + name, b'synthetic placeholder only\n')
        with self.assertRaises(BundleError):
            self.validate(limit=self.total - 1)

    def test_hostile_paths_and_nonregular_members(self):
        for name in ('/inputs/INCAR', '../INCAR', 'inputs/../INCAR', './inputs/INCAR',
                     'inputs//INCAR', 'inputs\\INCAR', 'inputs/INCAR\x00', 'extra.txt'):
            with self.subTest(name=name), self.assertRaises(BundleError):
                self.validate([self.headers[0], TarMemberHeader(name, self.headers[1].size), *self.headers[2:]])
        for flag in (b'1', b'2', b'3', b'4', b'5', b'6', b'7', b'x', b'g', b'L', b'K', b'S'):
            with self.subTest(typeflag=flag), self.assertRaises(BundleError):
                self.validate([self.headers[0], TarMemberHeader('inputs/INCAR', self.headers[1].size, flag), *self.headers[2:]])

    def test_regular_header_cannot_hide_links_or_sparse_content(self):
        for header in (TarMemberHeader('inputs/INCAR', self.headers[1].size, linkname='elsewhere'),
                       TarMemberHeader('inputs/INCAR', self.headers[1].size, sparse=True)):
            with self.subTest(header=header), self.assertRaises(BundleError):
                self.validate([self.headers[0], header, *self.headers[2:]])

    def test_manifest_first_regular_exact_once(self):
        cases = [self.headers[1:] + self.headers[:1], self.headers + self.headers[:1],
                 [TarMemberHeader('manifest.json', self.manifest_size, b'2'), *self.headers[1:]],
                 [TarMemberHeader('manifest.json', self.manifest_size + 1), *self.headers[1:]]]
        for headers in cases:
            with self.subTest(headers=headers), self.assertRaises(BundleError):
                self.validate(headers)

    def test_duplicate_extra_missing_and_truncated_headers(self):
        for headers in ([], self.headers[:-1], self.headers + self.headers[1:2],
                        self.headers + [TarMemberHeader('extra', 0)]):
            with self.subTest(headers=headers), self.assertRaises(BundleError):
                self.validate(headers)

    def test_member_size_and_hash_mismatch_and_truncated_payload(self):
        for size in (-1, True, 1.5, self.headers[1].size + 1):
            with self.subTest(size=size), self.assertRaises(BundleError):
                self.validate([self.headers[0], TarMemberHeader('inputs/INCAR', size), *self.headers[2:]])
        for payload in (b'', b'synthetic placeholder only', b'x' * self.headers[1].size):
            with self.subTest(length=len(payload)), self.assertRaises(BundleError) as caught:
                validate_file_payload(self.manifest, 'inputs/POTCAR', payload)
            self.assertNotIn(repr(payload), str(caught.exception))

    def test_manifest_must_declare_exact_four_files(self):
        for files in (self.manifest['files'][:-1], self.manifest['files'] * 2,
                      self.manifest['files'] + [{'path': 'support/run.sh', 'size': 1, 'sha256': 'a' * 64}]):
            value = {**self.manifest, 'files': files}
            with self.subTest(count=len(files)), self.assertRaises(ContractError):
                validate_manifest(value, manifest_size=self.manifest_size)


class BundlePlanTests(unittest.TestCase):
    def plan(self, value=None, target=None, **kwargs):
        return plan_bundle(manifest() if value is None else value, hello() if target is None else target,
                           **{'kpar': 2, 'ncore': 2, 'manifest_size': 1500, 'poscar_species': ['Zn'], **kwargs})

    def test_production_default_and_explicit_build(self):
        for build_id in (None, 'synthetic-production'):
            value = manifest()
            value['build']['build_id'] = build_id
            result = self.plan(value)
            self.assertTrue(result.complete)
            self.assertTrue(result.permitted)
            self.assertEqual(result.build_id, 'synthetic-production')

    def test_requested_build_and_executable_must_exist(self):
        for field, value, expected in (('build', {'build_id': 'missing', 'allow_experimental': False}, 'build_unavailable'),
                                       ('executable', 'vasp_ncl', 'executable_unavailable')):
            result = self.plan({**manifest(), field: value})
            self.assertFalse(result.permitted)
            self.assertIn(expected, [item.code for item in result.findings])

    def test_experimental_opt_in_and_default_production_meaning(self):
        target = hello()
        target['capabilities']['engines']['vasp']['builds'][0]['experimental'] = True
        self.assertIn('build_unavailable', [item.code for item in self.plan(target=target).findings])
        value = manifest()
        value['build']['build_id'] = 'synthetic-production'
        self.assertIn('experimental_build', [item.code for item in self.plan(value, target).findings])
        value['build']['allow_experimental'] = True
        self.assertTrue(self.plan(value, target).permitted)

    def test_rank_thread_capacity_and_kpar_error_ncore_warning(self):
        value = manifest()
        value['execution']['omp_threads'] = 2
        self.assertIn('physical_cores', [item.code for item in self.plan(value).findings])
        for kpar in (3, 32):
            result = self.plan(kpar=kpar)
            self.assertFalse(result.permitted)
            self.assertIn('kpar_divisibility', [item.code for item in result.findings])
        result = self.plan(ncore=3)
        self.assertTrue(result.permitted)
        self.assertEqual([(item.code, item.severity) for item in result.findings], [('ncore_compatibility', 'warning')])

    def test_recommended_parallelism_boundary_is_only_a_warning(self):
        target = hello()
        for maximum, expected_warning in ((16, False), (15, True), (None, False)):
            with self.subTest(maximum=maximum):
                target['hardware']['recommended_max_parallelism'] = maximum
                result = self.plan(target=target)
                self.assertTrue(result.permitted)
                self.assertEqual(any(item.code == 'recommended_max_parallelism' for item in result.findings), expected_warning)
                self.assertTrue(all(item.severity == 'warning' for item in result.findings))

    def test_physical_core_boundary_remains_an_error(self):
        target = hello()
        target['hardware']['recommended_max_parallelism'] = 8
        for cores, expected_error in ((16, False), (15, True)):
            with self.subTest(cores=cores):
                target['hardware']['physical_cores'] = cores
                result = self.plan(target=target)
                self.assertEqual(any(item.code == 'physical_cores' and item.severity == 'error'
                                     for item in result.findings), expected_error)
                self.assertEqual(result.permitted, not expected_error)

    def test_core_tier_names_have_no_policy_semantics(self):
        target = hello()
        original = self.plan(target=target)
        for item in target['hardware']['core_tiers']:
            item['name'] = 'Efficiency / performance / arbitrary vendor text'
        self.assertEqual(self.plan(target=target), original)

    def test_missing_evidence_never_looks_like_a_complete_plan(self):
        result = self.plan(kpar=None, ncore=None, poscar_species=None)
        self.assertFalse(result.complete)
        self.assertFalse(result.permitted)
        self.assertEqual(set(result.unassessed), {'KPAR', 'NCORE', 'POSCAR/POTCAR species order'})

    def test_bad_parsed_controls_and_species_order(self):
        for name in ('kpar', 'ncore'):
            for value in (False, 0, -1, 1.5, '2'):
                with self.subTest(name=name, value=value):
                    self.assertFalse(self.plan(**{name: value}).permitted)
        self.assertIn('species_order', [item.code for item in self.plan(poscar_species=['S', 'Zn']).findings])

    def test_size_limit_and_missing_capabilities(self):
        target = hello()
        target['capabilities']['receive']['max_bundle_bytes'] = 10
        self.assertIn('bundle_size', [item.code for item in self.plan(target=target).findings])
        target['capabilities'] = {}
        result = self.plan(target=target)
        self.assertFalse(result.permitted)
        self.assertIn('capability_unsupported', [item.code for item in result.findings])


class IdempotencyTests(unittest.TestCase):
    def test_new_and_replay_preserve_original_job_without_registration(self):
        value = manifest()
        self.assertEqual(decide_idempotency(value, None).action, IdempotencyAction.NEW)
        original = {'job_id': 'J4.1'}
        result = decide_idempotency(value, copy.deepcopy(value), original_result=original)
        self.assertEqual(result.action, IdempotencyAction.REUSE)
        self.assertEqual(result.original_result, original)

    def test_object_order_irrelevant_array_order_and_values_relevant(self):
        value = manifest()
        reordered = dict(reversed(list(value.items())))
        self.assertEqual(decide_idempotency(value, reordered).action, IdempotencyAction.REUSE)
        for field, replacement in (('created_utc', '2026-11-04T12:00:01.000Z'),
                                   ('files', list(reversed(value['files']))),
                                   ('execution', {'mpi_ranks': 8, 'omp_threads': 1})):
            result = decide_idempotency(value, {**value, field: replacement})
            self.assertEqual(result.action, IdempotencyAction.CONFLICT)
            self.assertEqual(result.error_code, 'conflict')

    def test_different_request_id_is_distinct_request(self):
        value = manifest()
        other = {**value, 'request_id': '22345678-1234-4234-8234-123456789abc'}
        self.assertEqual(decide_idempotency(value, other).action, IdempotencyAction.NEW)


if __name__ == '__main__':
    unittest.main()
