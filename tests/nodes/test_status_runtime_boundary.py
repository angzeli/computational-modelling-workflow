"""Request-only runtime reuse preserves complete, sticky response refusals."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.nodes import identity
from cmw.nodes.backend import LocalBackend
from cmw.nodes.protocol import NodeError
from cmw.nodes import runtime_observation as runtime
from tests.nodes.test_identity import HOST
from tests.nodes.test_storage import volume


class StatusRuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-status-runtime-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.package = self.root / 'package'
        self.package.mkdir()
        self.member = self.package / '__init__.py'
        self.member.write_text('value = 1\n')
        self.directory = self.package / 'subdir'
        self.directory.mkdir()
        self.second = self.directory / 'other.py'
        self.second.write_text('other = 2\n')
        origin = patch('cmw.__file__', str(self.member))
        origin.start()
        self.addCleanup(origin.stop)
        coherent = patch.object(runtime, '_source_imports_coherent', return_value=True)
        coherent.start()
        self.addCleanup(coherent.stop)
        self.request_import_check = runtime._request_imports_coherent
        request_coherent = patch.object(runtime, '_request_imports_coherent', return_value=True)
        self.request_coherent = request_coherent.start()
        self.addCleanup(request_coherent.stop)
        self.record, self.observed = identity.runtime_observed()
        self.verifier = runtime.RuntimeVerifier(self.record, self.observed, isolated=True)
        self.request = {}

    def begin(self):
        return self.verifier.verify_request(identity.runtime_observed,
            identity.runtime_boundary_observed, self.request)

    def finish(self, provider=None):
        return self.verifier.verify_request(identity.runtime_observed,
            provider or identity.runtime_boundary_observed, self.request, response=True)

    def refuse(self, change):
        self.begin()
        change()
        with self.assertRaises(runtime.RuntimeRefusal):
            self.finish()
        self.assertTrue(self.verifier.tainted)
        with self.assertRaisesRegex(runtime.RuntimeRefusal, 'fresh_isolated_observer_required'):
            self.verifier.verify(identity.runtime_observed)

    def test_one_full_scan_and_no_response_content_reads(self):
        with patch.object(identity, '_package_digest', wraps=identity._package_digest) as digest:
            self.assertEqual(self.begin(), self.record)
            with patch.object(runtime.RuntimeCollection, 'read', side_effect=AssertionError('content reread')):
                self.assertEqual(self.finish(), self.record)
            self.assertEqual(digest.call_count, 1)
            self.assertEqual(self.request, {})
            self.begin()
            self.finish()
            self.assertEqual(digest.call_count, 2)

    def test_same_bytes_replacement_refuses(self):
        def replace():
            replacement = self.root / 'replacement.py'
            replacement.write_bytes(self.member.read_bytes())
            before = self.member.stat()
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(self.member)
        self.refuse(replace)

    def test_content_change_with_restored_mtime_refuses(self):
        def change():
            before = self.member.stat()
            self.member.write_text('value = 2\n')
            os.utime(self.member, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.refuse(change)

    def test_added_member_refuses(self):
        self.refuse(lambda: (self.package / 'added.py').write_text('added = True\n'))

    def test_deleted_member_refuses(self):
        self.refuse(self.second.unlink)

    def test_member_mode_change_refuses(self):
        self.refuse(lambda: self.member.chmod(self.member.stat().st_mode ^ 0o020))

    def test_member_ctime_only_change_refuses(self):
        self.refuse(lambda: self.member.chmod(self.member.stat().st_mode))

    def test_empty_directory_added_refuses(self):
        self.refuse(lambda: (self.package / 'empty').mkdir())

    def test_directory_mode_change_refuses(self):
        self.refuse(lambda: self.directory.chmod(self.directory.stat().st_mode ^ 0o020))

    def test_directory_timestamp_change_refuses(self):
        def change():
            before = self.directory.stat()
            os.utime(self.directory, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        self.refuse(change)

    def test_add_then_delete_is_visible_in_directory_epoch(self):
        def transient():
            path = self.directory / 'transient.py'
            path.write_text('transient = True\n')
            path.unlink()
        self.refuse(transient)

    def test_acl_and_xattr_changes_refuse(self):
        native = runtime._extended_security
        for field in ('__cmw_acl__', 'user.synthetic-xattr'):
            with self.subTest(field=field):
                self.verifier = runtime.RuntimeVerifier(self.record, self.observed, isolated=True)
                self.request = {}
                self.begin()
                def changed(path, **options):
                    result = native(path, **options)
                    if path == self.member:
                        result[field] = '0' * 64
                    return result
                with patch.object(runtime, '_extended_security', side_effect=changed):
                    with self.assertRaises(runtime.RuntimeRefusal):
                        self.finish()
                self.assertTrue(self.verifier.tainted)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin UF_TRACKED')
    def test_response_flag_change_requires_fresh_full_observer(self):
        self.refuse(lambda: os.chflags(self.member, self.member.stat().st_flags ^ runtime.UF_TRACKED))

    def test_interpreter_replacement_refuses(self):
        self.begin()
        other = self.root / 'python-placeholder'
        other.write_bytes(b'synthetic interpreter, never executed')
        with patch.object(sys, 'executable', str(other)):
            with self.assertRaises(runtime.RuntimeRefusal):
                self.finish()
        self.assertTrue(self.verifier.tainted)

    def test_dependency_version_change_refuses(self):
        self.begin()
        native = identity.metadata.version
        with patch.object(identity.metadata, 'version',
                          side_effect=lambda name: 'changed' if name == 'psutil' else native(name)):
            with self.assertRaisesRegex(runtime.RuntimeRefusal, 'interpreter_or_dependency_changed'):
                self.finish()
        self.assertTrue(self.verifier.tainted)

    def test_incomplete_security_refuses(self):
        self.begin()
        with patch.object(runtime, '_extended_security', return_value={'__cmw_acl__': 'UNAVAILABLE:ACL'}):
            with self.assertRaisesRegex(runtime.RuntimeRefusal, 'security_metadata_incomplete'):
                self.finish()
        self.assertTrue(self.verifier.tainted)

    def test_partial_inventory_refuses(self):
        self.begin()
        def partial(snapshot):
            record, observed, directories, auxiliary = identity.runtime_boundary_observed(snapshot)
            observed['directories'] = {}
            return record, observed, directories, auxiliary
        with self.assertRaises(runtime.RuntimeRefusal):
            self.finish(partial)
        self.assertTrue(self.verifier.tainted)

    def test_lost_request_snapshot_refuses_instead_of_recovering_trust(self):
        self.begin()
        self.request.pop('snapshot')
        with self.assertRaisesRegex(runtime.RuntimeRefusal, 'incomplete_request_boundary'):
            self.finish()
        self.assertTrue(self.verifier.tainted)

    def test_lost_nested_request_evidence_is_sticky_before_provider_access(self):
        self.begin()
        complete = deepcopy(self.request)
        paths = ([(key,) for key in complete['snapshot']]
                 + [('record', key) for key in self.record]
                 + [('observation', key) for key in self.observed]
                 + [('auxiliary', key) for key in complete['snapshot']['auxiliary']]
                 + [('record', 'dependencies', 'psutil'),
                    ('observation', 'member_sha256', '__init__.py'),
                    ('directory_epochs', str(self.package)),
                    ('directory_epochs', str(self.directory))])
        for path in paths:
            with self.subTest(path=path):
                self.verifier = runtime.RuntimeVerifier(self.record, self.observed, isolated=True)
                self.request = deepcopy(complete)
                parent = self.request['snapshot']
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with patch.object(identity, 'runtime_boundary_observed') as provider:
                    with self.assertRaisesRegex(runtime.RuntimeRefusal, 'incomplete_request_boundary'):
                        self.finish(provider)
                    provider.assert_not_called()
                self.assertTrue(self.verifier.tainted)
                with self.assertRaisesRegex(runtime.RuntimeRefusal, 'fresh_isolated_observer_required'):
                    self.verifier.verify(identity.runtime_observed)

    def test_malformed_nested_request_evidence_refuses_before_provider_access(self):
        self.begin()
        complete = deepcopy(self.request)
        cases = [(('record',), None), (('observation',), []), (('directory_epochs',), []),
                 (('auxiliary',), None), (('auxiliary', 'members'), []),
                 (('auxiliary', 'directories'), []), (('auxiliary', 'member_sha256'), []),
                 (('record', 'dependencies'), []), (('record', 'package_sha256'), None),
                 (('observation', 'members'), None), (('observation', 'executable_epoch'), []),
                 (('observation', 'root_epoch'), [*self.observed['root_epoch'][:-1], False]),
                 (('directory_epochs', str(self.package)), (0,)),
                 (('directory_epochs', str(self.directory)), 'missing')]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                self.verifier = runtime.RuntimeVerifier(self.record, self.observed, isolated=True)
                self.request = deepcopy(complete)
                parent = self.request['snapshot']
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = value
                with patch.object(identity, 'runtime_boundary_observed') as provider:
                    with self.assertRaisesRegex(runtime.RuntimeRefusal, 'incomplete_request_boundary'):
                        self.finish(provider)
                    provider.assert_not_called()
                self.assertTrue(self.verifier.tainted)

    def test_malformed_response_evidence_is_sticky(self):
        self.begin()
        complete = deepcopy(self.request)
        record, observed, directories, auxiliary = identity.runtime_boundary_observed(complete['snapshot'])
        cases = [None, (record, observed), (None, observed, directories, auxiliary),
                 (record, None, directories, auxiliary), (record, observed, None, auxiliary),
                 (record, observed, directories, None),
                 (record, {key: value for key, value in observed.items() if key != 'members'}, directories, auxiliary)]
        for result in cases:
            with self.subTest(result=result):
                self.verifier = runtime.RuntimeVerifier(self.record, self.observed, isolated=True)
                self.request = deepcopy(complete)
                with self.assertRaisesRegex(runtime.RuntimeRefusal, 'incomplete_observation'):
                    self.finish(lambda snapshot: result)
                self.assertTrue(self.verifier.tainted)

    def test_new_incoherent_import_refuses(self):
        self.begin()
        with patch.object(runtime, '_request_imports_coherent', return_value=False):
            with self.assertRaisesRegex(runtime.RuntimeRefusal, 'fresh_isolated_observer_required'):
                self.finish()

    def test_incoherent_import_at_full_capture_refuses(self):
        with patch.object(runtime, '_request_imports_coherent', return_value=False):
            with self.assertRaisesRegex(runtime.RuntimeRefusal, 'fresh_isolated_observer_required'):
                self.begin()
        self.assertTrue(self.verifier.tainted)

    def cache(self):
        directory = self.package / '__pycache__'
        directory.mkdir()
        path = directory / '__init__.cpython-311.pyc'
        path.write_bytes(b'synthetic cache evidence, never executed')
        return path

    def test_preexisting_auxiliary_members_preserve_semantic_identity_and_are_read_once(self):
        cache = self.cache()
        artifact = self.package / '.DS_Store'
        artifact.write_bytes(b'synthetic metadata')
        with patch.object(runtime.RuntimeCollection, 'read', autospec=True,
                          side_effect=runtime.RuntimeCollection.read) as read:
            self.assertEqual(self.begin(), self.record)
            snapshot = self.request['snapshot']
            self.assertEqual(snapshot['observation'], self.observed)
            self.assertEqual(set(snapshot['auxiliary']['members']),
                             {str(cache.relative_to(self.package)), '.DS_Store'})
            self.assertEqual(sum(call.args[1] == cache for call in read.call_args_list), 1)
            self.assertEqual(sum(call.args[1] == artifact for call in read.call_args_list), 1)
            calls = read.call_count
            self.assertEqual(self.finish(), self.record)
            self.assertEqual(read.call_count, calls)

    def test_auxiliary_content_change_with_restored_mtime_refuses(self):
        cache = self.cache()
        def change():
            before = cache.stat()
            cache.write_bytes(b'x' * before.st_size)
            os.utime(cache, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.refuse(change)

    def test_auxiliary_same_bytes_replacement_refuses(self):
        cache = self.cache()
        def change():
            replacement = self.root / 'replacement.pyc'
            replacement.write_bytes(cache.read_bytes())
            replacement.replace(cache)
        self.refuse(change)

    def test_auxiliary_addition_refuses(self):
        cache = self.cache()
        self.refuse(lambda: (cache.parent / 'added.pyc').write_bytes(b'synthetic cache'))

    def test_auxiliary_deletion_refuses(self):
        cache = self.cache()
        self.refuse(cache.unlink)

    def test_auxiliary_transient_add_delete_refuses(self):
        cache = self.cache()
        def transient():
            added = cache.parent / 'transient.pyc'
            added.write_bytes(b'synthetic cache')
            added.unlink()
        self.refuse(transient)

    def test_auxiliary_empty_directory_addition_refuses(self):
        cache = self.cache()
        self.refuse(lambda: (cache.parent / 'empty').mkdir())

    def test_auxiliary_directory_mode_change_refuses(self):
        cache = self.cache()
        self.refuse(lambda: cache.parent.chmod(cache.parent.stat().st_mode ^ 0o020))

    def test_auxiliary_file_mode_change_refuses(self):
        cache = self.cache()
        self.refuse(lambda: cache.chmod(cache.stat().st_mode ^ 0o020))

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin UF_TRACKED')
    def test_auxiliary_flag_change_is_not_metadata_recovery(self):
        cache = self.cache()
        self.refuse(lambda: os.chflags(cache, cache.stat().st_flags ^ runtime.UF_TRACKED))

    def test_auxiliary_acl_and_xattr_changes_refuse(self):
        cache = self.cache()
        native = runtime._extended_security
        for field in ('__cmw_acl__', 'user.synthetic-xattr'):
            with self.subTest(field=field):
                self.verifier = runtime.RuntimeVerifier(self.record, self.observed, isolated=True)
                self.request = {}
                self.begin()
                def changed(path, **options):
                    result = native(path, **options)
                    if path == cache:
                        result[field] = '0' * 64
                    return result
                with patch.object(runtime, '_extended_security', side_effect=changed):
                    with self.assertRaises(runtime.RuntimeRefusal):
                        self.finish()
                self.assertTrue(self.verifier.tainted)

    def test_auxiliary_symlink_directory_refuses_without_reading_target(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'unread.pyc').write_bytes(b'not part of the runtime')
        (self.package / '__pycache__').symlink_to(outside, target_is_directory=True)
        with patch.object(runtime.RuntimeCollection, 'read', side_effect=AssertionError('content read')):
            with self.assertRaises(runtime.RuntimeRefusal):
                self.begin()
        self.assertTrue(self.verifier.tainted)

    def test_auxiliary_symlink_member_refuses_without_reading_target(self):
        cache = self.cache()
        cache.unlink()
        cache.symlink_to(self.member)
        with patch.object(runtime.RuntimeCollection, 'read', side_effect=AssertionError('content read')):
            with self.assertRaises(runtime.RuntimeRefusal):
                self.begin()
        self.assertTrue(self.verifier.tainted)

    def test_incomplete_auxiliary_directory_walk_refuses(self):
        cache = self.cache()
        native = os.fwalk
        def partial(*args, **kwargs):
            for item in native(*args, **kwargs):
                if Path(item[0]) != cache.parent:
                    yield item
        with patch.object(os, 'fwalk', side_effect=partial):
            with self.assertRaisesRegex(runtime.RuntimeRefusal, 'incomplete_directory_inventory'):
                self.begin()
        self.assertTrue(self.verifier.tainted)

    def test_lost_auxiliary_evidence_refuses_before_provider_access(self):
        cache = self.cache()
        self.begin()
        complete = deepcopy(self.request)
        name = str(cache.relative_to(self.package))
        for path in [('auxiliary', 'members', name), ('auxiliary', 'member_sha256', name),
                     ('auxiliary', 'directories', '__pycache__'),
                     ('directory_epochs', str(cache.parent))]:
            with self.subTest(path=path):
                self.verifier = runtime.RuntimeVerifier(self.record, self.observed, isolated=True)
                self.request = deepcopy(complete)
                parent = self.request['snapshot']
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with patch.object(identity, 'runtime_boundary_observed') as provider:
                    with self.assertRaisesRegex(runtime.RuntimeRefusal, 'incomplete_request_boundary'):
                        self.finish(provider)
                    provider.assert_not_called()
                self.assertTrue(self.verifier.tainted)

    def test_request_import_predicate_accepts_tracked_source_and_preexisting_bytecode(self):
        cache = self.cache()
        self.begin()
        observed = self.request['snapshot']['observation']
        auxiliary = self.request['snapshot']['auxiliary']
        for filename, cached in [(self.member, cache), (cache, cache),
                                 (self.second, self.directory / '__pycache__' / 'other.cpython-311.pyc')]:
            with self.subTest(filename=filename), patch.object(sys, 'modules', {
                    'cmw': SimpleNamespace(__file__=str(filename), __cached__=str(cached))}):
                self.assertTrue(self.request_import_check(observed, auxiliary))

    def test_request_import_predicate_refuses_outside_and_uninventoried_origins(self):
        cache = self.cache()
        self.begin()
        observed = self.request['snapshot']['observation']
        auxiliary = self.request['snapshot']['auxiliary']
        for filename, cached in [(self.root / 'outside.py', None),
                                 (self.package / 'unobserved.py', None),
                                 (self.member, self.root / 'outside.pyc'),
                                 (self.member, cache.parent)]:
            with self.subTest(filename=filename, cached=cached), patch.object(sys, 'modules', {
                    'cmw': SimpleNamespace(__file__=str(filename), __cached__=None if cached is None else str(cached))}):
                self.assertFalse(self.request_import_check(observed, auxiliary))

    def test_malformed_auxiliary_evidence_is_sticky(self):
        cache = self.cache()
        self.begin()
        complete = deepcopy(self.request)
        name = str(cache.relative_to(self.package))
        cases = [('member_sha256', name, 'not-a-digest'), ('members', name, [0]),
                 ('directories', '__pycache__', None)]
        for kind, name, value in cases:
            with self.subTest(kind=kind):
                self.verifier = runtime.RuntimeVerifier(self.record, self.observed, isolated=True)
                self.request = deepcopy(complete)
                self.request['snapshot']['auxiliary'][kind][name] = value
                with patch.object(identity, 'runtime_boundary_observed') as provider:
                    with self.assertRaisesRegex(runtime.RuntimeRefusal, 'incomplete_request_boundary'):
                        self.finish(provider)
                    provider.assert_not_called()
                self.assertTrue(self.verifier.tainted)

    def test_legacy_enrollments_keep_two_full_scans(self):
        for schema in (1, 2):
            with self.subTest(schema=schema):
                legacy = deepcopy(self.observed)
                legacy['schema'] = schema
                del legacy['member_sha256']
                self.verifier = runtime.RuntimeVerifier(self.record, legacy, isolated=True)
                self.request = {}
                with patch.object(identity, '_package_digest', wraps=identity._package_digest) as digest:
                    self.begin()
                    self.finish()
                    self.assertEqual(digest.call_count, 2)
                    self.assertNotIn('snapshot', self.request)

    def backend(self):
        storage = self.root / 'storage'
        storage.mkdir()
        runner = self.root / 'runner'
        runner.write_text('#!/bin/sh\nexit 0\n')
        runner.chmod(0o700)
        spec = {'queues': {'main': str(self.root / 'queue')},
                'storages': {'scratch': str(storage)},
                'profiles': {'fixture': {'engine': 'Command', 'runner': str(runner), 'max_cpus': 1}}}
        config_root = self.root / 'node'
        config = identity.enroll(config_root, spec, host_provider=lambda: dict(HOST),
            runtime_provider=lambda: self.record,
            storage_provider=lambda path: identity.identify_storage(path, probe=volume))
        config['runtime_observation'] = self.observed
        (config_root / 'node.json').write_text(json.dumps(config))
        host = patch.object(identity, 'host_evidence', return_value=dict(HOST))
        host.start()
        self.addCleanup(host.stop)
        backend = LocalBackend(config_root, 'main', boot_provider=lambda: 10.,
            activity_projector=lambda store: store.snapshot(now=100.))
        self.addCleanup(backend.close)
        return backend

    def test_backend_checks_owner_configuration_queue_and_boot_at_both_boundaries(self):
        backend = self.backend()
        from cmw.jobs.store import Store
        native_queue = Store.queue_identity
        with patch.object(identity, '_package_digest', wraps=identity._package_digest) as digest, \
                patch.object(backend, '_load_identity', wraps=backend._load_identity) as loader, \
                patch.object(identity, 'host_evidence', return_value=dict(HOST)) as host, \
                patch.object(Store, 'queue_identity', autospec=True, side_effect=native_queue) as queue, \
                patch.object(backend, '_boot_provider', return_value=10.) as boot:
            backend.call('status')
        self.assertEqual(digest.call_count, 1)
        self.assertEqual((loader.call_count, host.call_count, queue.call_count, boot.call_count), (2, 2, 2, 2))
        self.assertIsNone(backend._runtime_request)

    def test_backend_discards_response_and_remains_sticky_after_mid_request_change(self):
        backend = self.backend()
        def changed(store):
            self.second.write_text('other = 3\n')
            return store.snapshot(now=100.)
        backend._activity_projector = changed
        with self.assertRaises(NodeError) as caught:
            backend.call('status')
        self.assertEqual(caught.exception.code, 'RUNTIME_MISMATCH')
        self.assertEqual(backend.sequence, 0)
        self.assertTrue(backend._runtime_verifier.tainted)
        self.second.write_text('other = 2\n')
        with self.assertRaises(NodeError) as repeated:
            backend.call('status')
        self.assertEqual(repeated.exception.code, 'RUNTIME_MISMATCH')
        self.assertIsNone(backend._runtime_request)

    def test_mutation_still_checks_full_runtime_twice_and_retains_lost_result(self):
        from cmw.nodes import control
        backend = self.backend()
        job = backend.store.add(argv=['/bin/true'], cwd=self.root, name='Never executed', hold=True)
        target = backend.target(job)
        preview = backend.call('control.preview', {'action': 'release'}, target)['result']
        request_id = uuid4().hex
        params = {key: preview[key] for key in ('action', 'expected_clock_id', 'expected_revision')}
        params.update(confirm=True, reason='', _endpoint={
            'node_id': backend.config['node_id'], 'queue_id': backend.queue['queue_id'],
            'host_id': backend.config['host_id'], 'runtime_id': backend.config['runtime']['fingerprint'],
            'clock_id': backend.clock_id})
        native = control.dispatch
        def lost(*args, **options):
            result = native(*args, **options)
            self.second.write_text('other = 3\n')
            return result
        with patch.object(control, 'dispatch', side_effect=lost), \
                patch.object(identity, '_package_digest', wraps=identity._package_digest) as digest:
            with self.assertRaises(NodeError) as caught:
                backend.call('control', params, target, request_id=request_id)
        self.assertEqual(caught.exception.code, 'RUNTIME_MISMATCH')
        self.assertEqual(digest.call_count, 2)
        receipt = backend.store.request_lookup(request_id)
        self.assertEqual(receipt['result']['target'], target)
        self.assertEqual(receipt['result']['status'], 'COMPLETED')
        self.assertEqual(len(backend.store.snapshot()['jobs']), 1)
