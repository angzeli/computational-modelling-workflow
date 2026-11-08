"""D30 profile data uses only disposable synthetic executable files."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.nodes.identity import _profile, _validate_config, check_node, enroll, load_node
from cmw.nodes.runner_profiles import (interpreter_observation, pin_interpreter,
                                       profile_capabilities, runner_environment, validate_interpreter)
from cmw.nodes.storage import check_storage, identify_storage
from tests.nodes.test_identity import HOST, RUNTIME
from tests.nodes.test_storage import volume


class RunnerProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-runner-profile-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runner = self.root / 'synthetic-runner'
        self.interpreter = self.root / 'synthetic-python'
        for path in (self.runner, self.interpreter):
            path.write_text('#!/bin/sh\n# synthetic placeholder; never executed\nexit 0\n')
            path.chmod(0o700)
        self.storage = self.root / 'scratch'
        self.storage.mkdir()
        self.spec = {'queues': {'main': str(self.root / 'queue')},
                     'storages': {'scratch': str(self.storage)},
                     'profiles': {'fixture': {'engine': 'Command', 'runner': str(self.runner),
                                              'max_cpus': 1}}}
        self.providers = {'host_provider': lambda: dict(HOST), 'runtime_provider': lambda: dict(RUNTIME),
                          'storage_provider': lambda path: identify_storage(path, probe=volume)}
        self.checks = {**self.providers, 'storage_provider': lambda record: check_storage(record, probe=volume)}

    def pinned_spec(self):
        value = deepcopy(self.spec)
        value['profiles']['fixture'].update(schema=2, runner_interpreter=str(self.interpreter))
        return value

    def test_legacy_record_stays_schema_one_and_reports_unpinned(self):
        node = enroll(self.root / 'node', self.spec, **self.providers)
        self.assertEqual(node['schema'], 1)
        profile = node['profiles']['fixture']
        self.assertNotIn('schema', profile)
        self.assertNotIn('runner_interpreter', profile)
        self.assertEqual(load_node(self.root / 'node'), node)
        readiness = check_node(node, **self.checks)
        self.assertTrue(readiness['ready'])
        self.assertEqual(readiness['profiles']['fixture']['runner_interpreter']['status'], 'unpinned')
        advertised = profile_capabilities(node['profiles'])
        self.assertEqual(advertised['profiles'], node['profiles'])
        self.assertEqual(advertised['runner_profiles']['interpreters']['fixture']['status'], 'unpinned')

    def test_opt_in_profile_persists_versioned_pin_and_round_trips(self):
        node = enroll(self.root / 'node', self.pinned_spec(), **self.providers)
        self.assertEqual(node['schema'], 2)
        self.assertEqual(load_node(self.root / 'node'), node)
        profile = node['profiles']['fixture']
        self.assertEqual(profile['schema'], 2)
        self.assertEqual(profile['runner_interpreter'], pin_interpreter(str(self.interpreter)))
        self.assertEqual(_validate_config(json.loads(json.dumps(node))), node)
        readiness = check_node(node, **self.checks)
        self.assertTrue(readiness['ready'])
        self.assertEqual(readiness['profiles']['fixture']['runner_interpreter']['status'], 'pinned')

    def test_old_capability_profiles_never_receive_new_fields(self):
        profile = _profile(self.pinned_spec()['profiles']['fixture'], RUNTIME)
        profiles = {'fixture': profile}
        before = deepcopy(profiles)
        advertised = profile_capabilities(profiles)
        self.assertEqual(profiles, before)
        self.assertNotIn('schema', advertised['profiles']['fixture'])
        self.assertNotIn('runner_interpreter', advertised['profiles']['fixture'])
        self.assertEqual(advertised['runner_profiles']['schema'], 2)
        self.assertNotIn('profiles', advertised['runner_profiles'])
        advertised_pin = advertised['runner_profiles']['interpreters']['fixture']
        self.assertEqual(advertised_pin['profile_schema'], 2)
        self.assertEqual(advertised_pin['pin'], profile['runner_interpreter'])
        advertised_pin['pin']['path'] = '/changed'
        self.assertEqual(profiles, before)

    def test_backend_advertises_separate_versioned_interpreter_capability(self):
        from cmw.nodes.backend import LocalBackend
        from cmw.nodes.protocol import encode
        node = enroll(self.root / 'node', self.pinned_spec(), **self.providers)
        backend = LocalBackend(self.root / 'node', 'main',
            identity_checker=lambda value: check_node(value, **self.checks),
            activity_projector=lambda store: store.snapshot(), boot_provider=lambda: 1.)
        self.addCleanup(backend.close)
        envelope = backend.call('capabilities')
        encode(envelope)
        advertised = envelope['result']
        self.assertNotIn('runner_interpreter', advertised['profiles']['fixture'])
        pin = advertised['runner_profiles']['interpreters']['fixture']
        self.assertEqual(pin['status'], 'pinned')
        self.assertEqual(pin['pin'], node['profiles']['fixture']['runner_interpreter'])

    def test_changed_pin_refuses_profile_readiness_and_is_reported(self):
        node = enroll(self.root / 'node', self.pinned_spec(), **self.providers)
        self.interpreter.write_text('#!/bin/sh\n# changed synthetic interpreter\nexit 0\n')
        readiness = check_node(node, **self.checks)
        self.assertFalse(readiness['ready'])
        self.assertFalse(readiness['profiles_ready'])
        self.assertEqual(readiness['profiles']['fixture']['runner_interpreter']['status'], 'mismatch')
        advertised = profile_capabilities(node['profiles'], readiness['profiles'])
        self.assertEqual(advertised['runner_profiles']['interpreters']['fixture']['status'], 'mismatch')
        self.assertIn('interpreter identity changed', advertised['runner_profiles']['interpreters']['fixture']['error'])

    def test_schema_one_cannot_silently_carry_new_profile_fields(self):
        node = enroll(self.root / 'node', self.pinned_spec(), **self.providers)
        node['schema'] = 1
        with self.assertRaisesRegex(ValueError, 'schema'):
            _validate_config(node)

    def test_mixed_profiles_keep_legacy_profile_valid(self):
        spec = self.pinned_spec()
        spec['profiles']['legacy'] = deepcopy(self.spec['profiles']['fixture'])
        node = enroll(self.root / 'node', spec, **self.providers)
        readiness = check_node(node, **self.checks)
        self.assertTrue(readiness['ready'])
        self.assertEqual(readiness['profiles']['legacy']['runner_interpreter']['status'], 'unpinned')
        self.assertEqual(readiness['profiles']['fixture']['runner_interpreter']['status'], 'pinned')

    def test_invalid_profile_version_or_missing_pin_refuses_before_enrollment(self):
        changes = ({'schema': True}, {'schema': 1}, {'schema': 3}, {'schema': '2'},
                   {'schema': 2}, {'runner_interpreter': str(self.interpreter)})
        for change in changes:
            spec = deepcopy(self.spec)
            spec['profiles']['fixture'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                enroll(self.root / 'node', spec, **self.providers)
            self.assertFalse((self.root / 'queue').exists())
            self.assertFalse((self.root / 'node').exists())

    def test_invalid_interpreter_path_and_nonexecutable_refuse(self):
        for path in ('relative/python', str(self.root), str(self.root / 'missing'), None):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                pin_interpreter(path)
        self.interpreter.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'executable'):
            pin_interpreter(str(self.interpreter))

    def test_replacement_bytes_permissions_or_symlink_retarget_mismatch(self):
        for change in ('bytes', 'replacement', 'permissions', 'symlink'):
            with self.subTest(change=change):
                path = self.root / change
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o700)
                alias = self.root / (change + '-link')
                alias.symlink_to(path)
                profile = {'schema': 2, 'runner_interpreter': pin_interpreter(str(alias))}
                if change == 'bytes':
                    path.write_text('#!/bin/sh\nexit 1\n')
                elif change == 'replacement':
                    path.unlink()
                    path.write_text('#!/bin/sh\nexit 0\n')
                    path.chmod(0o700)
                elif change == 'permissions':
                    path.chmod(0o600)
                else:
                    alias.unlink()
                    alias.symlink_to(self.interpreter)
                self.assertEqual(interpreter_observation(profile, verify=True)['status'], 'mismatch')

    def test_malformed_or_incomplete_security_identity_refuses(self):
        original = pin_interpreter(str(self.interpreter))
        for mutate in (lambda value: value.update(path='relative'),
                       lambda value: value.update(unknown=True),
                       lambda value: value['epoch'].__setitem__(-1, False),
                       lambda value: value['epoch'].__setitem__(0, value['epoch'][0] + 1),
                       lambda value: value.update(sha256='bad')):
            value = deepcopy(original)
            mutate(value)
            with self.assertRaises(ValueError):
                validate_interpreter(value)

    def test_security_change_and_inflight_pin_change_refuse(self):
        from cmw.nodes.runner_profiles import file_epoch
        original = pin_interpreter(str(self.interpreter))
        epoch = list(file_epoch(self.interpreter))
        changed = list(epoch)
        changed[-2] = ('a' if epoch[-2][0] != 'a' else 'b') + epoch[-2][1:]
        with patch('cmw.nodes.runner_profiles.file_epoch', return_value=changed):
            self.assertEqual(interpreter_observation({'schema': 2, 'runner_interpreter': original},
                                                    verify=True)['status'], 'mismatch')
        with patch('cmw.nodes.runner_profiles.file_epoch', side_effect=[epoch, changed]):
            with self.assertRaisesRegex(ValueError, 'changed while pinning'):
                pin_interpreter(str(self.interpreter))

    def test_profile_interpreter_data_does_not_change_command_argv_or_environment(self):
        from cmw.nodes.submission import _argv
        profile = _profile(self.pinned_spec()['profiles']['fixture'], RUNTIME)
        before = dict(os.environ)
        argv = _argv({'engine': 'Command', 'arguments': ['synthetic'], 'manifest': {}},
                     profile, self.root / 'bundle', self.root / 'work', self.root / 'native')
        self.assertEqual(argv, [str(self.runner), 'synthetic'])
        self.assertEqual(dict(os.environ), before)

    def test_launch_environment_uses_verified_pin_despite_foreign_python_environment(self):
        profile = _profile(self.pinned_spec()['profiles']['fixture'], RUNTIME)
        foreign = {'PATH': '/different/interpreter/bin', 'VIRTUAL_ENV': '/different/environment',
                   'CMW_MANAGED_PYTHON': '/different/python', 'PYTHON_BIN': '/different/python',
                   'PYTHONPATH': '/different/stdlib', 'PYTHONHOME': '/different/home'}
        with patch.dict(os.environ, foreign):
            before = dict(os.environ)
            result = runner_environment(profile)
            self.assertEqual(result, {'CMW_MANAGED_PYTHON': str(self.interpreter),
                'PYTHON_BIN': str(self.interpreter), 'PYTHONPATH': '', 'PYTHONHOME': ''})
            self.assertEqual(dict(os.environ), before)
            effective = {**before, **result}
            self.assertEqual(effective['CMW_MANAGED_PYTHON'], str(self.interpreter))
            self.assertEqual(effective['PYTHON_BIN'], str(self.interpreter))
            self.assertEqual(effective['PYTHONPATH'], '')
            self.assertEqual(effective['PYTHONHOME'], '')

    def test_launch_refuses_legacy_missing_or_replaced_pin(self):
        legacy = _profile(self.spec['profiles']['fixture'], RUNTIME)
        with self.assertRaisesRegex(ValueError, 'verified schema-2 interpreter pin.*unpinned'):
            runner_environment(legacy)
        for mutation in ('missing', 'replaced'):
            with self.subTest(mutation=mutation):
                executable = self.root / (mutation + '-python')
                executable.write_text('#!/bin/sh\nexit 0\n')
                executable.chmod(0o700)
                profile = {'schema': 2, 'runner_interpreter': pin_interpreter(str(executable))}
                replacement = self.root / (mutation + '-replacement')
                replacement.write_text('#!/bin/sh\nexit 0\n')
                replacement.chmod(0o700)
                executable.unlink()
                if mutation == 'replaced':
                    replacement.rename(executable)
                with self.assertRaisesRegex(ValueError, 'verified schema-2 interpreter pin'):
                    runner_environment(profile)
