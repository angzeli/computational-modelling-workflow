"""Explicit enrollment with real isolated Store and pinned benign executables."""
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.store import Store
from cmw.nodes.identity import check_node, enroll, host_evidence, load_node, runtime_identity
from cmw.nodes.storage import check_storage, identify_storage
from tests.nodes.test_storage import volume


HOST = {'platform': 'darwin', 'hardware_id': '12345678-1234-1234-1234-123456789abc'}
RUNTIME = {'fingerprint': 'a' * 64, 'version': 'test-runtime', 'python': 'test-python',
           'dependencies': {}, 'algorithm': 'cmw-runtime-sha256-v1', 'package_sha256': 'b' * 64,
           'package_origin': '/fixture/package', 'executable': '/fixture/python'}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root / 'storage'
        self.storage.mkdir()
        self.runner = self.root / 'benign-command'
        self.runner.write_text('#!/bin/sh\nexit 0\n')
        self.runner.chmod(0o700)
        self.config = self.root / 'node'
        self.state = self.root / 'queue'
        self.spec = {'queues': {'research': str(self.state)}, 'storages': {'scratch': str(self.storage)},
                     'profiles': {'benign': {'engine': 'Command', 'runner': str(self.runner), 'max_cpus': 2}}}
        self.enrollment = {'host_provider': lambda: dict(HOST), 'runtime_provider': lambda: dict(RUNTIME),
                           'storage_provider': lambda path: identify_storage(path, probe=volume)}
        self.checks = {**self.enrollment, 'storage_provider': lambda record: check_storage(record, probe=volume)}

    def test_explicit_private_enrollment_and_readonly_check(self):
        config = enroll(self.config, self.spec, **self.enrollment)
        self.assertEqual(load_node(self.config), config)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.config / 'node.json').stat().st_mode), 0o600)
        self.assertRegex(config['node_id'], r'^[0-9a-f]{32}$')
        identity = Store(self.state).queue_identity()
        self.assertEqual(config['queues']['research']['queue_id'], identity['queue_id'])
        before = (self.state / 'queue.sqlite3').read_bytes() if (self.state / 'queue.sqlite3').exists() else None
        result = check_node(config, **self.checks)
        self.assertTrue(result['ready'], result)
        self.assertEqual(Store(self.state).queue_identity(), identity)
        if before is not None:
            self.assertEqual((self.state / 'queue.sqlite3').read_bytes(), before)
        with self.assertRaises(ValueError):
            enroll(self.config, self.spec, **self.enrollment)
        self.assertEqual(load_node(self.config), config)

    def test_readonly_missing_config_never_initializes(self):
        with self.assertRaises(FileNotFoundError):
            load_node(self.config)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.state.exists())

    def test_host_clone_runtime_mismatch_and_replaced_queue(self):
        config = enroll(self.config, self.spec, **self.enrollment)
        changed_host = {**HOST, 'hardware_id': '12345678-1234-1234-1234-000000000000'}
        result = check_node(config, **{**self.checks, 'host_provider': lambda: changed_host})
        self.assertFalse(result['identity_ready'])
        result = check_node(config, **{**self.checks, 'runtime_provider': lambda: {**RUNTIME, 'fingerprint': 'b' * 64}})
        self.assertFalse(result['runtime_ready'])
        self.state.rename(self.root / 'old-queue')
        result = check_node(config, **self.checks)
        self.assertFalse(result['identity_ready'])
        self.assertFalse(self.state.exists())

    def test_missing_storage_keeps_identity_observable(self):
        config = enroll(self.config, self.spec, **self.enrollment)
        self.storage.rmdir()
        result = check_node(config, **self.checks)
        self.assertTrue(result['identity_ready'])
        self.assertTrue(result['runtime_ready'])
        self.assertFalse(result['storage_ready'])
        self.assertFalse(result['ready'])
        self.assertFalse(self.storage.exists())

    def test_runner_dependency_and_mode_changes_refuse_readiness(self):
        dependency = self.root / 'explicit-dependency'
        dependency.write_bytes(b'one')
        self.spec['profiles']['benign']['dependencies'] = [str(dependency)]
        config = enroll(self.config, self.spec, **self.enrollment)
        dependency.write_bytes(b'two')
        self.assertFalse(check_node(config, **self.checks)['profiles_ready'])
        self.runner.chmod(0o600)
        self.assertFalse(check_node(config, **self.checks)['profiles_ready'])

    def test_bad_profile_and_unsupported_vasp_ranks_do_not_enroll_queue(self):
        for change in ({'max_cpus': True}, {'max_cpus': 0}, {'dependencies': [{}]},
                       {'engine': 'VASP', 'max_cpus': 16, 'max_ranks': 16}):
            spec = copy.deepcopy(self.spec)
            spec['profiles']['benign'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                enroll(self.config, spec, **self.enrollment)
            self.assertFalse(self.state.exists())
            self.assertFalse(self.config.exists())

    def test_duplicate_queue_and_storage_aliases_refused(self):
        for section, value in (('queues', str(self.state)), ('storages', str(self.storage))):
            spec = copy.deepcopy(self.spec)
            spec[section]['other'] = value
            with self.subTest(section=section), self.assertRaises(ValueError):
                enroll(self.config, spec, **self.enrollment)
        self.assertFalse(self.state.exists())

    def test_malformed_duplicate_or_nonprivate_config_refused(self):
        config = enroll(self.config, self.spec, **self.enrollment)
        path = self.config / 'node.json'
        for content in ('{"schema":1,"schema":1}', json.dumps({**config, 'extra': 1}),
                        json.dumps({**config, 'schema': True}),
                        json.dumps({**config, 'profiles': {'bad': {'runtime_fingerprint': 'a' * 64}}}), 'NaN'):
            path.write_text(content)
            with self.subTest(content=content[:40]), self.assertRaises(ValueError):
                load_node(self.config)
        path.write_text(json.dumps(config))
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            load_node(self.config)
        path.chmod(0o600)
        self.config.chmod(0o755)
        with self.assertRaises(ValueError):
            load_node(self.config)

    def test_actual_runtime_fingerprint_is_source_sensitive_without_ui_imports(self):
        package = self.root / 'package'
        package.mkdir()
        init = package / '__init__.py'
        init.write_text('value = 1\n')
        with patch('cmw.__file__', str(init)):
            first = runtime_identity()
            second = runtime_identity()
            self.assertEqual(first['fingerprint'], second['fingerprint'])
            init.write_text('value = 2\n')
            changed = runtime_identity()['fingerprint']
            self.assertNotEqual(first['fingerprint'], changed)
            (package / 'new.py').write_text('new = 3\n')
            added = runtime_identity()['fingerprint']
            self.assertNotEqual(changed, added)
            (package / 'new.py').unlink()
            self.assertEqual(changed, runtime_identity()['fingerprint'])
        self.assertEqual(first['algorithm'], 'cmw-runtime-sha256-v1')

    def test_host_evidence_is_local_and_bounded(self):
        class Result:
            stdout = b'"IOPlatformUUID" = "12345678-1234-1234-1234-123456789ABC"\n'
        with patch('cmw.nodes.identity.sys.platform', 'darwin'), patch(
                'cmw.nodes.identity.subprocess.run', return_value=Result()) as run:
            self.assertEqual(host_evidence(), HOST)
            self.assertEqual(run.call_args.args[0][0], '/usr/sbin/ioreg')
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
