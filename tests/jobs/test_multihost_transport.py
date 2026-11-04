"""Offline transport contract tests: no subprocess, network, files or sleeps."""
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import patch

from cmw.jobs.multihost.models import ContractError, Envelope, HelloResult, UnsupportedVersionError
from cmw.jobs.multihost.transport import (
    FailureKind, FakeStep, FakeTransport, LocalTransport, SshTransport,
    Transport, TransportFailure,
)


def response(command='snapshot', request_id=None, *, ok=True):
    value = {'protocol_version': '1.0', 'node_id': 'mm_m5pro',
             'command': command, 'ok': ok, 'result': {} if ok else None,
             'error': None if ok else {'code': 'not_found', 'message': 'Synthetic missing job', 'details': {}}}
    if request_id is not None:
        value['request_id'] = request_id
    return value


class TransportTests(unittest.TestCase):
    def test_interface_is_abstract(self):
        with self.assertRaises(TypeError):
            Transport()

    def test_production_skeletons_name_implementation_phase(self):
        for transport, phase in ((LocalTransport(), 'Phase 1'), (SshTransport(), 'Phase 3')):
            with self.subTest(phase=phase), self.assertRaisesRegex(NotImplementedError, phase):
                transport.run('hello')

    def test_generated_hello_and_echo(self):
        tiers = [{'level': 0, 'name': ' Synthetic Tier A ', 'physical_cores': 12},
                 {'level': 1, 'name': 'arbitrary native name', 'physical_cores': 4}]
        transport = FakeTransport(capabilities={'submit': True}, physical_cores=16,
                                  core_tiers=tiers, recommended_max_parallelism=10)
        envelope = transport.run('hello', request_id='echo-me')
        self.assertIsInstance(envelope, Envelope)
        self.assertEqual(envelope.request_id, 'echo-me')
        hello = HelloResult.from_dict(envelope.result)
        self.assertEqual(hello.hardware.physical_cores, 16)
        self.assertEqual([tier.to_dict() for tier in hello.hardware.core_tiers], tiers)
        self.assertEqual(hello.hardware.recommended_max_parallelism, 10)
        self.assertTrue(hello.capabilities.submit)

    def test_scripted_mapping_json_bytes_and_model(self):
        value = response()
        model = Envelope.from_dict(value)
        transport = FakeTransport([FakeStep(value), FakeStep(model.to_json()),
                                   FakeStep(model.to_json().encode()), FakeStep(model)])
        for _ in range(4):
            self.assertEqual(transport.run('snapshot').to_dict(), value)

    def test_returned_snapshot_does_not_mutate_script(self):
        value = response()
        value['result'] = {'jobs': []}
        original = deepcopy(value)
        transport = FakeTransport([FakeStep(value), FakeStep(value)])
        transport.run('snapshot').result['jobs'].append('local-only')
        self.assertEqual(value, original)
        self.assertEqual(transport.run('snapshot').result['jobs'], [])

    def test_node_error_is_an_envelope_not_a_channel_failure(self):
        result = FakeTransport([FakeStep(response(ok=False))]).run('snapshot')
        self.assertIsInstance(result, Envelope)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code.value, 'not_found')

    def test_virtual_latency_without_sleep(self):
        transport = FakeTransport(latency_seconds=2, start_time=1000)
        with patch('time.sleep', side_effect=AssertionError('Must not sleep')):
            hello = HelloResult.from_dict(transport.run('hello').result)
        self.assertEqual(transport.last_sent_at, 1000)
        self.assertEqual(transport.last_received_at, 1002)
        self.assertEqual(datetime.fromisoformat(hello.node_time_utc.replace('Z', '+00:00')).timestamp(), 1001)

    def test_timeout_at_deadline_and_success_at_equal_latency(self):
        transport = FakeTransport([FakeStep(latency_seconds=5), FakeStep(latency_seconds=4)], start_time=0)
        failed = transport.run('hello', timeout=4)
        self.assertEqual(failed, TransportFailure(FailureKind.TIMEOUT, 'Synthetic request timed out', 4))
        self.assertEqual(transport.now, 4)
        self.assertIsInstance(transport.run('hello', timeout=4), Envelope)
        self.assertEqual(transport.now, 8)

    def test_explicit_scripted_failures(self):
        for kind in FailureKind:
            with self.subTest(kind=kind):
                result = FakeTransport([FakeStep(failure=kind)]).run('hello')
                self.assertEqual(result.kind, kind)
                self.assertEqual(result.elapsed_seconds, 4 if kind == FailureKind.TIMEOUT else 0)

    def test_disconnect_after_n_requests_is_persistent(self):
        transport = FakeTransport(disconnect_after=2)
        self.assertIsInstance(transport.run('hello'), Envelope)
        self.assertIsInstance(transport.run('hello'), Envelope)
        for _ in range(3):
            self.assertEqual(transport.run('hello').kind, FailureKind.CONNECT_ERROR)

    def test_auth_failure_until_explicit_recovery(self):
        transport = FakeTransport(auth_failure=True)
        for _ in range(2):
            self.assertEqual(transport.run('hello').kind, FailureKind.AUTH_FAILURE)
        transport.auth_failure = False
        self.assertIsInstance(transport.run('hello'), Envelope)

    def test_version_mismatch_preserves_typed_protocol_error(self):
        for version in ('2.0', '0.9'):
            with self.subTest(version=version), self.assertRaises(UnsupportedVersionError):
                FakeTransport(protocol_version=version).run('hello')
        self.assertEqual(FakeTransport(protocol_version='1.9').run('hello').protocol_version, '1.9')

    def test_clock_offset_is_independent_of_receive_time(self):
        for offset in (-60.001, -60, -5, -4.999, 0, 4.999, 5, 59.999, 60, 60.001):
            with self.subTest(offset=offset):
                transport = FakeTransport(start_time=1000, latency_seconds=2, clock_offset_seconds=offset)
                hello = HelloResult.from_dict(transport.run('hello').result)
                remote = datetime.fromisoformat(hello.node_time_utc.replace('Z', '+00:00')).timestamp()
                self.assertAlmostEqual(remote - (transport.last_sent_at + transport.last_received_at) / 2, offset)
                self.assertEqual(transport.last_received_at, 1002)

    def test_response_binding_is_checked(self):
        for key, value in (('command', 'tail'), ('node_id', 'wrong_node'), ('request_id', 'wrong-id')):
            payload = response()
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(ContractError):
                FakeTransport([FakeStep(payload)]).run('snapshot')

    def test_invalid_payload_and_unscripted_command_are_not_success(self):
        for payload in ('{', {'ok': True}):
            with self.subTest(payload=payload), self.assertRaises(ContractError):
                FakeTransport([FakeStep(payload)]).run('snapshot')
        with self.assertRaises(ContractError):
            FakeTransport().run('submit', request_id='same-id')

    def test_scripted_idempotent_submit_responses(self):
        payload = response('submit', 'same-id')
        payload['result'] = {'job_id': 'J1.1'}
        transport = FakeTransport([FakeStep(failure=FailureKind.CONNECT_ERROR),
                                   FakeStep(payload), FakeStep(payload)])
        self.assertIsInstance(transport.run('submit', request_id='same-id'), TransportFailure)
        first = transport.run('submit', request_id='same-id')
        self.assertEqual(first.to_dict(), transport.run('submit', request_id='same-id').to_dict())

    def test_invalid_simulation_inputs(self):
        for options in ({'latency_seconds': -1}, {'clock_offset_seconds': float('nan')},
                        {'disconnect_after': True}, {'start_time': float('inf')},
                        {'auth_failure': 1}, {'steps': ['bad']}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                FakeTransport(**options)
        for options in ({'latency_seconds': -1}, {'failure': 'timeout'},
                        {'response': {}, 'failure': FailureKind.TIMEOUT}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                FakeStep(**options)
        for timeout in (0, -1, True, float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                FakeTransport().run('hello', timeout=timeout)


if __name__ == '__main__':
    unittest.main()
