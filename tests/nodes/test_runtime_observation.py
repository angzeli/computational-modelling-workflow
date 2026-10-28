"""Original trust is immutable; metadata recovery is bounded current-disk evidence."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.nodes.identity import _package_digest, runtime_observed, runtime_identity
from cmw.nodes.runtime_observation import RuntimeVerifier, RuntimeRefusal
from tests.nodes import test_identity as identity_fixtures


class RuntimeObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name).resolve()
        self.member = self.package / '__init__.py'
        self.member.write_text('value=1\n')
        self.origin = patch('cmw.__file__', str(self.member))
        self.origin.start()
        self.addCleanup(self.origin.stop)
        # Model a platform with complete security evidence for recovery negatives.
        # A separate real-platform test retains incomplete macOS ACL coverage.
        self.security = patch('cmw.nodes.runtime_observation._extended_security', return_value={})
        self.security.start()
        self.addCleanup(self.security.stop)
        self.imports = patch('cmw.nodes.runtime_observation._source_imports_coherent', return_value=True)
        self.imports.start()
        self.addCleanup(self.imports.stop)
        self.original, self.epoch = runtime_observed()

    def verifier(self, original=True, **options):
        return RuntimeVerifier(self.original, self.epoch if original else None,
            process_started_ns=10**20, isolated=True, **options)

    def touch_metadata(self):
        os.chmod(self.member, self.member.stat().st_mode)

    def test_stable_and_one_full_ctime_recovery_preserve_original_semantics(self):
        verifier = self.verifier()
        trusted = deepcopy(self.epoch)
        self.assertEqual(verifier.verify(runtime_observed), self.original)
        self.touch_metadata()
        self.assertEqual(verifier.verify(runtime_observed), self.original)
        self.assertEqual(verifier.original, trusted)
        self.assertTrue(verifier.recovered)
        self.assertEqual(verifier.last_evidence['category'], 'ctime_only_current_disk_verified')
        self.assertEqual(verifier.verify(runtime_observed), self.original)

    def test_long_lived_importer_requires_fresh_isolated_observer(self):
        verifier = RuntimeVerifier(self.original, self.epoch, process_started_ns=0, isolated=True)
        self.touch_metadata()
        with self.assertRaises(RuntimeRefusal) as caught:
            verifier.verify(runtime_observed)
        self.assertEqual(caught.exception.evidence['category'], 'fresh_isolated_observer_required')
        self.assertEqual(caught.exception.evidence['phase'], 'loaded-code')
        self.assertFalse(verifier.recovered)
        self.assertEqual(verifier.current, self.epoch)

    def test_nonisolated_fresh_process_does_not_restore_authority(self):
        verifier = RuntimeVerifier(self.original, self.epoch, process_started_ns=10**20, isolated=False)
        self.touch_metadata()
        with self.assertRaisesRegex(RuntimeRefusal, 'fresh_isolated'):
            verifier.verify(runtime_observed)

    def test_unknown_baseline_and_repeated_drift_refuse(self):
        verifier = self.verifier(False)
        verifier.verify(runtime_observed)
        self.touch_metadata()
        with self.assertRaisesRegex(RuntimeRefusal, 'original_baseline_unavailable'):
            verifier.verify(runtime_observed)
        verifier = self.verifier()
        verifier.verify(runtime_observed)
        self.touch_metadata()
        with self.assertRaisesRegex(RuntimeRefusal, 'metadata_recovery_exhausted'):
            verifier.verify(runtime_observed)

    def test_uncertain_imported_code_does_not_gain_authority(self):
        self.touch_metadata()
        with patch('cmw.nodes.runtime_observation._source_imports_coherent', return_value=False):
            with self.assertRaisesRegex(RuntimeRefusal, 'fresh_isolated'):
                self.verifier().verify(runtime_observed)

    def test_directory_object_or_security_change_refuses(self):
        original = deepcopy(self.epoch)
        original['directories']['nodes'] = list(original['root_epoch'])
        current = deepcopy(original)
        current['directories']['nodes'][1] += 1
        verifier = RuntimeVerifier(self.original, original, process_started_ns=10**20, isolated=True)
        with self.assertRaisesRegex(RuntimeRefusal, 'directory_identity_or_security_changed'):
            verifier.verify(lambda: (self.original, current))

    def test_unknown_security_baseline_never_recovers(self):
        original = deepcopy(self.epoch)
        original['root_epoch'][-1] = False
        current = deepcopy(original)
        current['members']['__init__.py'][4] += 1
        verifier = RuntimeVerifier(self.original, original, process_started_ns=10**20, isolated=True)
        with self.assertRaisesRegex(RuntimeRefusal, 'security_baseline_incomplete'):
            verifier.verify(lambda: (self.original, current))

    def test_identical_byte_replacement_is_never_adopted(self):
        copy = self.package / 'replacement'
        copy.write_bytes(self.member.read_bytes())
        os.utime(copy, ns=(self.member.stat().st_atime_ns, self.member.stat().st_mtime_ns))
        copy.replace(self.member)
        with self.assertRaisesRegex(RuntimeRefusal, 'member_replaced'):
            self.verifier().verify(runtime_observed)

    def test_permissions_and_security_metadata_refuse(self):
        self.member.chmod(0o600 if self.member.stat().st_mode & 0o077 else 0o644)
        with self.assertRaisesRegex(RuntimeRefusal, 'security_metadata_changed'):
            self.verifier().verify(runtime_observed)

    def test_changed_content_and_membership_refuse_with_original_evidence(self):
        for change in ('content', 'addition', 'removal'):
            with self.subTest(change=change):
                current = deepcopy(self.epoch)
                record = deepcopy(self.original)
                if change == 'content':
                    current['package_sha256'] = '0' * 64
                    record['package_sha256'] = '0' * 64
                    record['fingerprint'] = '0' * 64
                elif change == 'addition':
                    current['members']['added.py'] = list(next(iter(current['members'].values())))
                else:
                    current['members'] = {}
                with self.assertRaises(RuntimeRefusal) as caught:
                    self.verifier().verify(lambda: (record, current))
                self.assertIn(caught.exception.evidence['category'],
                              {'content_changed', 'membership_changed', 'runtime_fingerprint_changed'})

    def test_in_flight_change_is_not_retried_or_cached_as_success(self):
        from cmw.nodes import identity
        from cmw.nodes.runtime_observation import file_epoch
        _package_digest.cache_clear()
        calls = 0
        def racing(path):
            nonlocal calls
            calls += 1
            value = file_epoch(path)
            if calls == 2:
                return (*value[:4], value[4] + 1, *value[5:])
            return value
        with patch.object(identity, 'file_epoch', side_effect=racing):
            with self.assertRaisesRegex(RuntimeRefusal, 'in_flight_change'):
                runtime_observed()
        self.assertEqual(_package_digest.cache_info().currsize, 0)


class ScopedReadinessTests(unittest.TestCase):
    setUp = identity_fixtures.IdentityTests.setUp

    def test_unrelated_storage_and_profile_are_not_read_authority(self):
        from cmw.nodes.identity import check_node, enroll
        config = enroll(self.config, self.spec, **self.enrollment)
        self.runner.unlink()
        self.storage.rmdir()
        for operation in ('status', 'progress', 'request', 'control', 'doctor.inspect'):
            result = check_node(config, **self.checks, operation=operation, queue_alias='research')
            self.assertTrue(result['ready'], result)
            self.assertIsNone(result['profiles_ready'])
            self.assertIsNone(result['storage_ready'])
        required = check_node(config, **self.checks, operation='execution',
            queue_alias='research', profile_aliases=['benign'], storage_aliases=['scratch'])
        self.assertFalse(required['ready'])
        self.assertFalse(required['storage_ready'])
        self.assertFalse(required['profiles_ready'])

    def test_selected_queue_check_never_adopts_another_queue_or_skip_list(self):
        from cmw.nodes.identity import check_node, enroll
        config = enroll(self.config, self.spec, **self.enrollment)
        with self.assertRaises(ValueError):
            check_node(config, **self.checks, operation='status', queue_alias='unknown')
        with self.assertRaises(ValueError):
            check_node(config, **self.checks, operation='skip_identity', queue_alias='research')
        with self.assertRaises(ValueError):
            check_node(config, **self.checks, storage_aliases=[])


class RequestRecoveryTests(unittest.TestCase):
    from tests.nodes.test_backend import BackendObservationTests as Fixture
    setUp = Fixture.setUp
    make_backend = Fixture.make_backend
    job = Fixture.job

    def test_runtime_refusal_after_commit_keeps_original_request_lookup(self):
        from cmw.nodes import control
        from cmw.nodes.protocol import NodeError
        from uuid import uuid4
        job = self.job()
        target = self.backend.target(job)
        preview = self.backend.call('control.preview', {'action': 'release'}, target)['result']
        request_id = uuid4().hex
        params = {key: preview[key] for key in ('action', 'expected_clock_id', 'expected_revision')}
        params.update(confirm=True, reason='', _endpoint={
            'node_id': self.config['node_id'], 'queue_id': self.backend.queue['queue_id'],
            'host_id': self.config['host_id'], 'runtime_id': self.config['runtime']['fingerprint'],
            'clock_id': self.backend.clock_id})
        original = control.dispatch
        def committed_then_refused(*args, **kwargs):
            result = original(*args, **kwargs)
            self.readiness['runtime_ready'] = False
            return result
        with patch.object(control, 'dispatch', side_effect=committed_then_refused):
            with self.assertRaises(NodeError) as caught:
                self.backend.call('control', params, target, request_id=request_id)
        self.assertEqual(caught.exception.code, 'RUNTIME_MISMATCH')
        durable = self.store.request_lookup(request_id)
        self.readiness['runtime_ready'] = True
        fresh = self.make_backend()
        found = fresh.call('request', {'request_id': request_id})['result']['request']
        self.assertEqual(found, durable)
        self.assertEqual(found['result']['target'], target)
        self.assertEqual(found['result']['status'], 'COMPLETED')
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
