"""Policy-3 runtime checks preserve original bytes, authority and request identity."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.nodes.identity import runtime_observed
from cmw.nodes.runtime_observation import RuntimeVerifier, RuntimeRefusal, validate_observation
from tests.nodes import test_identity as identity_fixtures


class RuntimeObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-policy3-')
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name).resolve()
        self.member = self.package / '__init__.py'
        self.member.write_text('value=1\n')
        self.origin = patch('cmw.__file__', str(self.member))
        self.origin.start(); self.addCleanup(self.origin.stop)
        self.imports = patch('cmw.nodes.runtime_observation._source_imports_coherent', return_value=True)
        self.imports.start(); self.addCleanup(self.imports.stop)
        self.original, self.epoch = runtime_observed()

    def verifier(self, original=True, **options):
        return RuntimeVerifier(self.original, self.epoch if original else None,
            process_started_ns=0, isolated=True, **options)

    def touch_metadata(self):
        if not hasattr(os, 'chflags'):
            self.skipTest('Darwin UF_TRACKED')
        os.chflags(self.member, self.member.stat().st_flags ^ 0x40)

    def test_unchanged_runtime_and_complete_member_hashes(self):
        import hashlib
        verifier = self.verifier()
        self.assertEqual(verifier.verify(runtime_observed), self.original)
        self.assertEqual(self.epoch['schema'], 3)
        self.assertEqual(self.epoch['member_sha256'],
            {'__init__.py': hashlib.sha256(b'value=1\n').hexdigest()})
        self.assertFalse(verifier.recovered)

    def test_late_tracked_change_preserves_fingerprint_and_original_trust(self):
        verifier = self.verifier()
        trusted = deepcopy(self.epoch)
        self.touch_metadata()
        self.assertEqual(verifier.verify(runtime_observed), self.original)
        self.assertEqual(verifier.original, trusted)
        self.assertEqual(verifier.last_evidence['category'], 'uf_tracked_current_disk_verified')
        self.assertGreater(verifier.current['members']['__init__.py'][4], verifier.process_started_ns)
        self.assertEqual(verifier.verify(runtime_observed), self.original)

    def test_unexplained_ctime_is_not_exempt(self):
        os.chmod(self.member, self.member.stat().st_mode)
        with self.assertRaisesRegex(RuntimeRefusal, 'unqualified_observation_change'):
            self.verifier().verify(runtime_observed)

    def test_independent_member_changes_across_calls_do_not_exhaust_global_budget(self):
        other = self.package/'second.py'; other.write_text('value=2\n')
        original, epoch = runtime_observed()
        verifier = RuntimeVerifier(original, epoch, process_started_ns=0, isolated=True)
        self.touch_metadata()
        self.assertEqual(verifier.verify(runtime_observed), original)
        os.chflags(other, other.stat().st_flags ^ 0x40)
        self.assertEqual(verifier.verify(runtime_observed), original)
        self.assertEqual(verifier.original, epoch)

    def test_content_with_restored_mtime_and_tracked_still_refuses(self):
        before = self.member.stat()
        self.member.write_text('value=2\n')
        os.utime(self.member, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.touch_metadata()
        with self.assertRaisesRegex(RuntimeRefusal, 'content_changed'):
            self.verifier().verify(runtime_observed)

    def test_same_bytes_replacement_and_symlink_refuse(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                other = self.package.parent/(self.package.name+'-replacement')
                other.write_bytes(b'value=1\n'); self.addCleanup(other.unlink, missing_ok=True)
                before = self.member.stat(); os.utime(other, ns=(before.st_atime_ns,before.st_mtime_ns))
                self.member.unlink()
                if symlink: self.member.symlink_to(other)
                else: other.replace(self.member)
                with self.assertRaises(RuntimeRefusal): self.verifier().verify(runtime_observed)

    def test_material_refusal_taints_interpreter_after_disk_restoration(self):
        verifier = self.verifier()
        changed = deepcopy(self.epoch); changed['members']['__init__.py'][5] ^= 0o020
        with self.assertRaisesRegex(RuntimeRefusal, 'security_metadata_changed'):
            verifier.verify(lambda:(self.original,changed))
        with self.assertRaisesRegex(RuntimeRefusal, 'fresh_isolated_observer_required'):
            verifier.verify(lambda:(self.original,self.epoch))
        self.assertEqual(verifier.current,self.epoch)

    def test_original_authority_negatives_are_handwritten(self):
        cases = [('inode',1,1),('owner',6,1),('mode',5,0o020),('links',9,1),
                 ('unknown_flag',8,2),('mixed_flag',8,0x42),('acl',10,'0'*64),('incomplete',11,False)]
        for name,index,delta in cases:
            with self.subTest(name=name):
                current=deepcopy(self.epoch)
                current['members']['__init__.py'][index] = (current['members']['__init__.py'][index]^delta
                    if type(delta) is int else delta)
                with self.assertRaises(RuntimeRefusal): self.verifier().verify(lambda:(self.original,current))
        for field in ('root_epoch','executable_epoch'):
            current=deepcopy(self.epoch); current[field][0]+=1
            with self.subTest(field=field),self.assertRaises(RuntimeRefusal):
                self.verifier().verify(lambda:(self.original,current))
        current=deepcopy(self.epoch); current['directories']['injected']=list(current['root_epoch'])
        with self.assertRaises(RuntimeRefusal): self.verifier().verify(lambda:(self.original,current))

    def test_added_removed_and_partial_member_evidence_refuse(self):
        for kind in ('added','removed','partial'):
            current=deepcopy(self.epoch)
            if kind=='added':
                current['members']['other.py']=list(current['members']['__init__.py'])
                current['member_sha256']['other.py']='0'*64
            elif kind=='removed': current['members'].clear();current['member_sha256'].clear()
            else: current['member_sha256'].clear()
            with self.subTest(kind=kind),self.assertRaises(RuntimeRefusal):
                self.verifier().verify(lambda:(self.original,current))

    def test_legacy_exact_only_and_no_retroactive_security(self):
        for schema in (1,2):
            legacy=deepcopy(self.epoch);legacy.pop('member_sha256');legacy['schema']=schema
            if schema==1:
                for row in [legacy['root_epoch'],legacy['executable_epoch'],*legacy['members'].values()]:row[-1]=False
            verifier=RuntimeVerifier(self.original,legacy,isolated=True)
            self.assertEqual(verifier.verify(lambda:(self.original,self.epoch)),self.original)
            current=deepcopy(self.epoch);current['members']['__init__.py'][8]^=0x40
            current['members']['__init__.py'][4]+=1
            with self.assertRaisesRegex(RuntimeRefusal,'legacy_observation_policy'):
                verifier.verify(lambda:(self.original,current))
            self.assertEqual(verifier.original,legacy)
        invalid=deepcopy(self.epoch);invalid['schema']=True
        with self.assertRaises(ValueError):validate_observation(invalid)

    def test_missing_original_baseline_does_not_gain_revalidation(self):
        verifier=self.verifier(False);verifier.verify(runtime_observed)
        self.touch_metadata()
        with self.assertRaisesRegex(RuntimeRefusal,'original_baseline_unavailable'):verifier.verify(runtime_observed)

    def test_incomplete_original_baseline_refuses_revalidation(self):
        original=deepcopy(self.epoch);original['root_epoch'][-1]=False
        verifier=RuntimeVerifier(self.original,original,isolated=True)
        current=deepcopy(original);current['members']['__init__.py'][8]^=0x40
        with self.assertRaisesRegex(RuntimeRefusal,'security_baseline_incomplete'):
            verifier.verify(lambda:(self.original,current))


    def test_darwin_flag_exception_is_not_portable_to_other_platforms(self):
        current=deepcopy(self.epoch)
        current['members']['__init__.py'][8]^=0x40
        current['members']['__init__.py'][4]+=1
        with patch('cmw.nodes.runtime_observation.sys.platform','linux'):
            with self.assertRaisesRegex(RuntimeRefusal,'unqualified_observation_change'):
                self.verifier().verify(lambda:(self.original,current))


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
