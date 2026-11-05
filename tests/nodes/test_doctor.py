"""Selected-node diagnosis is bounded, identity-bound and never an execution route."""
from copy import deepcopy
from contextlib import closing
import io
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from cmw.doctor.nodes import inspect_connected, inspect_node
from cmw.jobs.locations import _read_job
from cmw.jobs.store import Store
from cmw.nodes.backend import LocalBackend
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.endpoint import serve
from cmw.nodes.identity import check_node, enroll
from cmw.nodes.protocol import NodeError, OPERATIONS, decode, encode, request, response, validate_envelope
from tests.nodes import test_backend as backend_tests, test_identity as identity_tests


class DoctorReadinessTests(unittest.TestCase):
    setUp = identity_tests.IdentityTests.setUp

    def test_selected_queue_skips_unrelated_roots_and_runner_payloads(self):
        self.spec['queues']['unrelated'] = str(self.root / 'other-queue')
        config = enroll(self.config, self.spec, **self.enrollment)
        before = (self.state / 'queue.sqlite3').read_bytes()
        touched = []

        def selected_store(path):
            touched.append(path)
            self.assertEqual(path, str(self.state))
            return Store(path)

        with patch('cmw.nodes.identity._file_identity', side_effect=AssertionError('runner read')), patch(
                'cmw.nodes.identity.check_storage', side_effect=AssertionError('storage read')), patch(
                'cmw.nodes.identity.subprocess.run', side_effect=AssertionError('process launch')):
            result = check_node(config, operation='doctor.inspect', queue_alias='research',
                host_provider=self.enrollment['host_provider'], runtime_provider=self.enrollment['runtime_provider'],
                store_factory=selected_store)
        self.assertTrue(result['ready'])
        self.assertEqual(touched, [str(self.state)])
        self.assertIsNone(result['profiles_ready'])
        self.assertEqual(result['coverage']['storage'], 'NOT_CHECKED')
        self.assertEqual((self.state / 'queue.sqlite3').read_bytes(), before)

    def test_full_readiness_still_rejects_changed_profile_and_bad_runtime(self):
        config = enroll(self.config, self.spec, **self.enrollment)
        self.runner.write_text('#!/bin/sh\ntouch never-executed\n')
        self.assertFalse(check_node(config, **self.checks)['profiles_ready'])
        scoped = check_node(config, **self.checks, operation='doctor.inspect', queue_alias='research')
        self.assertTrue(scoped['ready'])
        bad = check_node(config, **{**self.checks, 'runtime_provider': lambda: {
            **config['runtime'], 'fingerprint': 'f' * 64}}, operation='doctor.inspect', queue_alias='research')
        self.assertFalse(bad['runtime_ready'])
        self.assertFalse((self.root / 'never-executed').exists())

    def test_scoped_readiness_requires_known_operation_and_selected_queue(self):
        config = enroll(self.config, self.spec, **self.enrollment)
        for options in ({'operation': 'anything'}, {'operation': 'doctor.inspect'},
                        {'operation': 'doctor.inspect', 'queue_alias': 'missing'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                check_node(config, **self.checks, **options)


class DoctorBackendTests(unittest.TestCase):
    make_backend = backend_tests.BackendObservationTests.make_backend
    job = backend_tests.BackendObservationTests.job

    def setUp(self):
        backend_tests.BackendObservationTests.setUp(self)
        replacement = patch('cmw.doctor.service.inspect_local', side_effect=self.report)
        self.local_report = replacement.start()
        self.addCleanup(replacement.stop)

    def report(self, *, state=None, job_id=None, expected_attempt_id=None, capability=None):
        job = _read_job(Store(state), job_id)[0] if state is not None and job_id is not None else None
        if job:
            self.assertEqual(job['attempt_id'], expected_attempt_id)
        runtime = self.config['runtime'] if state is not None else {'fingerprint': 'caller-runtime'}
        return {'schema': 1, 'scope': 'local', 'runtime': {'identity': deepcopy(runtime)},
                'target': {'state': str(state) if state else None, 'node_id': None, 'queue_id': None,
                           'job_id': job['id'] if job else None, 'attempt_id': job['attempt_id'] if job else None},
                'worker': {'status': 'UNKNOWN', 'runtime': None, 'qualification': 'No recorded runtime'},
                'capabilities': [], 'findings': [], 'scientific_policy': 'NOT_ASSESSED',
                'observation': {'status': 'OBSERVED', 'observed_at': 100., 'coverage': 'bounded fixture',
                                'transport': {}, 'progress': None, 'binding': None, 'controller': None}}

    def connected(self, backend=None, *, entry_change=None):
        backend = backend or self.backend

        class Pipe:
            def exchange(self, operation, params=None, target=None, *, request_id=None):
                return decode(encode(backend.call(operation, params, target, request_id=request_id)))
            def close(self):
                backend.close()

        entry = {'transport': 'local', 'config_root': str(self.root / 'node-config'),
                 'node_id': self.config['node_id'], 'host_id': self.config['host_id'],
                 'runtime_id': self.config['runtime']['fingerprint'],
                 'queues': {'research': self.config['queues']['research']['queue_id']},
                 'default_queue': 'research', 'read_only': True}
        result = ConnectedBackend('selected', {**entry, **(entry_change or {})}, transport=Pipe())
        self.addCleanup(result.close)
        return result

    def test_registration_pairs_fixed_path_with_observed_owner_launch_flags(self):
        registered = '/owner/qualified/cmw'
        remote = self.connected(entry_change={'transport': 'ssh', 'executable': registered,
                                             'ssh_alias': 'fixture', 'ssh_host_key': 'SHA256:' + 'a' * 43})
        def observed(**kwargs):
            report = self.report(**kwargs)
            report['runtime']['bytecode_policy'] = {'isolated': 1, 'startup_dont_write_bytecode': 1,
                    'dont_write_bytecode': True,
                    'launch_flags': ['-B', '-I'] if kwargs.get('state') else [],
                    'owner_cache_access': 'READ_ONLY'}
            return report
        self.local_report.side_effect = observed
        report = inspect_connected(remote)
        self.assertEqual(report['caller_runtime']['bytecode_policy']['launch_flags'], [])
        self.assertEqual(report['registered_executable'],
                         {'transport': 'ssh', 'path': registered, 'launch_flags': ['-B', '-I']})
        with patch.object(remote, 'call', side_effect=NodeError('DISCONNECTED', 'No reply')):
            report = inspect_connected(remote)
        self.assertIsNone(report['registered_executable']['launch_flags'])
        self.assertEqual(report['registered_executable']['path'], registered)

    def test_schema_advertisement_and_exact_target_roundtrip_without_queue_write(self):
        job = self.job()
        before = self.store.path.read_bytes()
        features = self.backend.call('capabilities')['result']
        self.assertEqual(features['doctor'], {'schema': 1, 'operation': 'doctor.inspect'})
        self.assertIn('doctor.inspect', features['operations'])
        target = self.backend.target(job)
        report = inspect_connected(self.connected(), target=target, capability='progress.vasp')
        self.assertEqual({key: report['target'][key] for key in target}, target)
        self.assertEqual(report['caller_runtime']['identity']['fingerprint'], 'caller-runtime')
        self.assertEqual(report['endpoint_runtime']['identity'], self.config['runtime'])
        self.assertEqual(report['worker']['status'], 'UNKNOWN')
        self.assertEqual(report['observation']['transport']['status'], 'fresh')
        self.assertEqual(report['observation']['transport']['clock_id'], self.backend.clock_id)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.local_report.call_args.kwargs['expected_attempt_id'], job['attempt_id'])

    def test_initial_display_id_resolves_once_on_owning_node(self):
        job = self.job()
        report = inspect_connected(self.connected(), job_id=job['display_id'])
        self.assertEqual(report['target']['attempt_id'], job['attempt_id'])
        self.assertEqual(report['target']['job_id'], job['id'])
        self.assertEqual(self.local_report.call_args.kwargs['state'], self.store.root)

    def test_wrong_node_runtime_pin_and_attempt_fail_closed(self):
        job = self.job()
        target = self.backend.target(job)
        for change, expected in (({'node_id': uuid4().hex}, 'WRONG_NODE'),
                                 ({'attempt_id': uuid4().hex}, 'ATTEMPT_CHANGED')):
            report = inspect_connected(self.connected(), target={**target, **change})
            self.assertEqual(report['findings'][-1]['code'], expected)
            self.assertIsNone(report['endpoint_runtime'])
        wrong = self.connected(entry_change={'runtime_id': 'other-runtime'})
        report = inspect_connected(wrong, target=target)
        self.assertEqual(report['findings'][-1]['code'], 'RUNTIME_MISMATCH')
        self.assertEqual(report['observation']['status'], 'UNAVAILABLE')

    def test_doctor_parameters_cannot_be_shell_callbacks(self):
        before = self.store.path.read_bytes()
        for params in ({'script': 'touch unexpected'}, {'job_id': True}, {'capability': {}},
                       {'job_id': 'x' * 129}):
            with self.subTest(params=params), self.assertRaises(NodeError):
                self.backend.call('doctor.inspect', params)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_deferred_initialization_does_not_skip_ordinary_checks(self):
        checker = Mock(return_value=deepcopy(self.readiness))
        backend = LocalBackend(self.root / 'node', 'research', defer_check=True,
            identity_loader=lambda _: deepcopy(self.config), identity_checker=checker)
        self.addCleanup(backend.close)
        checker.assert_not_called()
        checker.return_value['runtime_ready'] = False
        for operation in ('status', 'capabilities', 'control'):
            with self.subTest(operation=operation), self.assertRaisesRegex(NodeError, 'Pinned endpoint runtime'):
                backend.call(operation)
        self.assertEqual(checker.call_count, 3)

    def test_unknown_queue_schema_is_refused_without_migration(self):
        with closing(sqlite3.connect(self.store.path)) as con, con:
            control = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
            control['schema'] = 999
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control),))
        before = self.store.path.read_bytes()
        report = inspect_connected(self.connected())
        self.assertEqual(report['observation']['status'], 'UNAVAILABLE')
        self.assertIn('schema', report['findings'][-1]['evidence']['error'].lower())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_cached_endpoint_provenance_is_never_reported_as_fresh(self):
        client = self.connected()
        cached = client.call('capabilities')
        with patch.object(client, 'call', side_effect=NodeError('DISCONNECTED', 'No reply')):
            report = inspect_connected(client)
        self.assertEqual(report['observation']['status'], 'CACHED')
        self.assertEqual(report['endpoint_runtime']['qualification'], 'LAST_REPORTED')
        transport = report['observation']['transport']
        self.assertEqual(transport['last_reported']['session_id'], cached['session_id'])
        self.assertEqual(transport['last_reported']['observed_at'], 100.)
        self.assertIsNone(report['observation']['observed_at'])
        self.assertEqual(report['worker']['status'], 'UNKNOWN')

    def test_response_schema_and_cross_owner_runtime_are_rejected(self):
        job = self.job()
        envelope = self.backend.call('doctor.inspect', target=self.backend.target(job))
        validate_envelope(envelope, 'doctor.inspect')
        for field, change in (('schema', 2), ('runtime', {'identity': {'fingerprint': 'another'}}),
                              ('target', {**envelope['result']['report']['target'], 'node_id': uuid4().hex}),
                              ('worker', {'status': 'KNOWN', 'runtime': {}}), ('capabilities', {}),
                              ('worker', {'status': 'RECORDED', 'runtime': None}),
                              ('worker', {'status': 'RECORDED', 'runtime': {'fingerprint': 'unproven'}}),
                              ('observation', {}), ('findings', [{'code': 'INCOMPLETE'}]),
                              ('scientific_policy', 'ACCEPTED')):
            altered = deepcopy(envelope)
            altered['result']['report'][field] = change
            with self.subTest(field=field), self.assertRaises(NodeError):
                validate_envelope(altered, 'doctor.inspect')

    def test_old_endpoint_refuses_before_backend_or_readiness_is_created(self):
        message = request('doctor.inspect')
        outgoing = io.BytesIO()
        factory = Mock(side_effect=AssertionError('old readiness must not be constructed'))
        with patch('cmw.nodes.protocol.OPERATIONS', OPERATIONS - {'doctor.inspect'}):
            serve('/unused', 'research', input_stream=io.BytesIO(encode(message)),
                  output_stream=outgoing, backend_factory=factory)
        with self.assertRaises(NodeError) as caught:
            response(decode(outgoing.getvalue()), message['request_id'])
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_OPERATION')
        factory.assert_not_called()

    def test_only_one_explicit_connection_is_opened_and_closed(self):
        backend = self.connected()
        with patch('cmw.doctor.nodes.connect', return_value=backend) as connect, patch.object(
                backend, 'close', wraps=backend.close) as close:
            report = inspect_node('selected', 'research', nodes_config=self.root / 'frontend')
        connect.assert_called_once_with('selected', root=self.root / 'frontend', queue='research')
        close.assert_called_once()
        self.assertEqual(report['target']['node_alias'], 'selected')


if __name__ == '__main__':
    unittest.main()
