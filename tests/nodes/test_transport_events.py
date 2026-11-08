"""Structured reachability evidence; no SSH connection or child process."""
import errno
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import Mock, patch

from cmw.nodes.contracts import NodeEvent, NodeState, Reachability, transition
from cmw.nodes.protocol import NodeError
from cmw.nodes.transport import ProcessTransport, SSHTransport
from cmw.nodes.transport_events import (
    FailureKind, TransportFailure, classify_failure, compatible_capabilities,
)


PIN = 'SHA256:' + 'a' * 43
OTHER_PIN = 'SHA256:' + 'b' * 43
KEY_RECORD = f'debug1: Server host key: ssh-ed25519 {PIN}\n'.encode()
EXHAUSTED_RECORD = b'debug1: No more authentication methods to try.\n'
AUTHENTICATED_RECORD = b'Authenticated to cmw-mini ([2001:db8::2]:22) using "publickey".\n'


class TransportEventTests(unittest.TestCase):
    def test_mutation_refusal_is_conservatively_unknown_and_wrapped_timeout_keeps_event(self):
        for code in ('RUNTIME_MISMATCH', 'IDENTITY_CHANGED', 'INVALID_RESPONSE', 'DISCONNECTED'):
            self.assertTrue(classify_failure(NodeError(code, 'refused'), operation='control').outcome_unknown)
        original = TransportFailure('OUTCOME_UNKNOWN', 'timeout', event=NodeEvent.TIMEOUT)
        wrapped = NodeError('OUTCOME_UNKNOWN', 'context added by control client')
        wrapped.__cause__ = original
        self.assertEqual(classify_failure(wrapped, operation='control').event, NodeEvent.TIMEOUT)

    def test_explicit_authentication_codes_are_immediate(self):
        for code in ('AUTH_FAILED', 'SSH_AUTH_FAILED'):
            with self.subTest(code=code):
                failure = classify_failure(NodeError(code, 'structured refusal'))
                self.assertEqual(failure.kind, FailureKind.AUTHENTICATION)
                self.assertEqual(failure.event, NodeEvent.AUTH_FAILURE)
                self.assertTrue(failure.reconnect and failure.controls_refused)
                current = transition(Reachability(NodeState.REACHABLE), failure.event)
                self.assertEqual(current.state, NodeState.AUTH_FAILED)
                self.assertEqual(current.consecutive_failures, 0)

    def test_identity_refusals_never_count_as_transient_transport_failures(self):
        codes = ('SSH_IDENTITY_CHANGED', 'SSH_IDENTITY_UNVERIFIED', 'ENDPOINT_CHANGED',
                 'IDENTITY_CHANGED', 'QUEUE_CHANGED', 'RUNTIME_MISMATCH', 'UNSAFE_CONFIGURATION')
        for code in codes:
            with self.subTest(code=code):
                failure = classify_failure(NodeError(code, 'refused'))
                self.assertEqual(failure.kind, FailureKind.SECURITY)
                self.assertIsNone(failure.event)
                self.assertTrue(failure.reconnect and failure.controls_refused)

    def test_capability_refusals_are_incompatible_only_in_handshake_context(self):
        for code in ('INCOMPATIBLE_PROTOCOL', 'INCOMPATIBLE_CAPABILITIES',
                     'INVALID_RESPONSE', 'UNSUPPORTED_OPERATION'):
            with self.subTest(code=code):
                failure = classify_failure(NodeError(code, 'refused'), operation='capabilities')
                self.assertEqual(failure.kind, FailureKind.INCOMPATIBLE)
                self.assertEqual(failure.event, NodeEvent.INCOMPATIBLE_HANDSHAKE)
                self.assertTrue(failure.controls_refused)
        self.assertEqual(classify_failure(NodeError('UNSUPPORTED_OPERATION', 'refused'),
                         operation='progress').kind, FailureKind.OPERATION)
        self.assertEqual(classify_failure(NodeError('INVALID_RESPONSE', 'refused'),
                         operation='status').kind, FailureKind.TRANSPORT)

    def test_readiness_refusals_disable_controls_immediately(self):
        for code in ('NODE_REFUSED', 'NODE_NOT_READY', 'UNKNOWN_QUEUE', 'INVALID_QUEUE',
                     'HANDSHAKE_REQUIRED', 'OWNERSHIP_UNVERIFIABLE'):
            with self.subTest(code=code):
                failure = classify_failure(NodeError(code, 'refused'))
                self.assertEqual(failure.kind, FailureKind.READINESS)
                self.assertIsNone(failure.event)
                self.assertTrue(failure.reconnect and failure.controls_refused)

    def test_disconnect_and_timeout_are_distinct_events(self):
        cases = (
            (NodeError('DISCONNECTED', 'closed'), NodeEvent.TRANSPORT_FAILURE),
            (NodeError('UNREACHABLE', 'address changed'), NodeEvent.TRANSPORT_FAILURE),
            (NodeError('TIMEOUT', 'late'), NodeEvent.TIMEOUT),
            (TimeoutError('late'), NodeEvent.TIMEOUT),
            (ConnectionRefusedError(errno.ECONNREFUSED, 'refused'), NodeEvent.TRANSPORT_FAILURE),
            (TransportFailure('UNREACHABLE', 'late', event=NodeEvent.TIMEOUT), NodeEvent.TIMEOUT),
        )
        for error, event in cases:
            with self.subTest(error=repr(error)):
                failure = classify_failure(error)
                self.assertEqual(failure.kind, FailureKind.TRANSPORT)
                self.assertEqual(failure.event, event)
                self.assertTrue(failure.reconnect and failure.controls_refused)

    def test_unknown_mutation_outcome_is_retained_with_transport_event(self):
        for event in (NodeEvent.TIMEOUT, NodeEvent.TRANSPORT_FAILURE):
            with self.subTest(event=event):
                failure = classify_failure(TransportFailure('OUTCOME_UNKNOWN', 'lost reply', event=event))
                self.assertEqual(failure.kind, FailureKind.OUTCOME_UNKNOWN)
                self.assertEqual(failure.event, event)
                self.assertTrue(failure.outcome_unknown and failure.controls_refused)

    def test_messages_do_not_supply_authority(self):
        for message in ('Permission denied (publickey).', 'AUTH_FAILED',
                        'WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!',
                        'INCOMPATIBLE_PROTOCOL', 'RUNTIME_MISMATCH'):
            with self.subTest(message=message):
                self.assertEqual(classify_failure(OSError(message)).kind, FailureKind.TRANSPORT)
                self.assertEqual(classify_failure(NodeError('INVALID_PARAMS', message)).kind,
                                 FailureKind.OPERATION)
                self.assertFalse(classify_failure(NodeError('INVALID_PARAMS', message)).controls_refused)

    def test_changed_or_unreachable_ipv6_address_counts_three_failures(self):
        state = Reachability(NodeState.REACHABLE, received_at=10.)
        errors = (OSError(errno.EHOSTUNREACH, '2001:db8::2 is unreachable'),
                  NodeError('UNREACHABLE', 'cmw-mini now resolves to 2001:db8::3'),
                  TimeoutError('no response from changed IPv6 address'))
        for index, error in enumerate(errors, 1):
            failure = classify_failure(error)
            self.assertEqual(failure.kind, FailureKind.TRANSPORT)
            state = transition(state, failure.event)
            self.assertEqual(state.consecutive_failures, index)
            self.assertEqual(state.state, NodeState.UNREACHABLE if index == 3 else NodeState.REACHABLE)
        self.assertEqual(state.received_at, 10.)

    def test_capabilities_need_observation_operations_and_explicit_readiness(self):
        valid = {'operations': ['capabilities', 'status'],
                 'readiness': {'identity_ready': True, 'runtime_ready': True, 'ready': True}}
        self.assertIs(compatible_capabilities({'result': valid}), valid)
        for result in ({}, {'operations': ['status']}, {'operations': ['status', 1]},
                       {'operations': ['capabilities', 'status']}):
            with self.subTest(result=result), self.assertRaises(NodeError) as caught:
                compatible_capabilities({'result': result})
            self.assertEqual(caught.exception.code, 'INCOMPATIBLE_CAPABILITIES')
        for field, code in (('identity_ready', 'IDENTITY_CHANGED'),
                            ('runtime_ready', 'RUNTIME_MISMATCH'), ('ready', 'NODE_NOT_READY')):
            with self.subTest(field=field), self.assertRaises(NodeError) as caught:
                compatible_capabilities({'result': {**valid,
                    'readiness': {**valid['readiness'], field: False}}})
            self.assertEqual(caught.exception.code, code)


class SSHDiagnosticEvidenceTests(unittest.TestCase):
    def channel(self, *, status=255, pinned=True):
        channel = ProcessTransport(['/usr/bin/ssh'], host_key=PIN if pinned else None)
        channel.process = Mock()
        channel.process.stderr.fileno.return_value = 23
        channel.process.wait.return_value = status
        return channel

    def closed(self, channel, *, mutation=False, pending=b''):
        with patch('cmw.nodes.transport.os.read', return_value=pending):
            return channel._closed_error(mutation)

    def test_pin_requires_complete_record_and_never_changes(self):
        channel = self.channel()
        channel._diagnostic(KEY_RECORD[:-1])
        self.assertFalse(channel._verified_key)
        channel._diagnostic(b'\n')
        self.assertTrue(channel._verified_key)
        with self.assertRaises(NodeError) as caught:
            channel._diagnostic(f'debug1: Server host key: ssh-ed25519 {OTHER_PIN}\n'.encode())
        failure = classify_failure(caught.exception)
        self.assertEqual(failure.kind, FailureKind.SECURITY)
        self.assertTrue(failure.controls_refused)
        self.assertEqual(channel.host_key, PIN)

    def test_embedded_and_partial_diagnostic_text_is_not_evidence(self):
        for data in (b'endpoint says: ' + KEY_RECORD, KEY_RECORD.rstrip() + b' extra\n',
                     b'Permission denied (publickey).\n',
                     b'error: ' + EXHAUSTED_RECORD,
                     EXHAUSTED_RECORD.rstrip() + b' extra\n'):
            with self.subTest(data=data):
                channel = self.channel()
                channel._diagnostic(data)
                self.assertFalse(channel._verified_key)
                self.assertFalse(channel._auth_exhausted)
                self.assertEqual(self.closed(channel).code, 'DISCONNECTED')

    def test_auth_failure_requires_pin_exit_255_and_terminal_negotiation(self):
        channel = self.channel()
        error = self.closed(channel, pending=KEY_RECORD + EXHAUSTED_RECORD)
        self.assertEqual(error.code, 'AUTH_FAILED')
        self.assertEqual(error.exit_status, 255)
        self.assertEqual(error.event, NodeEvent.AUTH_FAILURE)
        self.assertEqual(classify_failure(error).kind, FailureKind.AUTHENTICATION)
        for status, records, pinned in (
            (255, EXHAUSTED_RECORD, True),
            (255, KEY_RECORD, True),
            (1, KEY_RECORD + EXHAUSTED_RECORD, True),
            (0, KEY_RECORD + EXHAUSTED_RECORD, True),
            (255, KEY_RECORD + EXHAUSTED_RECORD, False),
        ):
            with self.subTest(status=status, records=records, pinned=pinned):
                candidate = self.channel(status=status, pinned=pinned)
                self.assertEqual(self.closed(candidate, pending=records).code, 'DISCONNECTED')

    def test_unknown_exit_status_does_not_assert_authentication_failure(self):
        channel = self.channel()
        channel.process.wait.side_effect = subprocess.TimeoutExpired('/usr/bin/ssh', .2)
        error = self.closed(channel, pending=KEY_RECORD + EXHAUSTED_RECORD)
        self.assertEqual(error.code, 'DISCONNECTED')
        self.assertIsNone(error.exit_status)

    def test_authenticated_endpoint_cannot_forge_pre_authentication_evidence(self):
        channel = self.channel()
        channel._diagnostic(KEY_RECORD + AUTHENTICATED_RECORD)
        channel._diagnostic(EXHAUSTED_RECORD +
                            f'debug1: Server host key: ssh-ed25519 {OTHER_PIN}\n'.encode())
        self.assertTrue(channel._authenticated)
        self.assertFalse(channel._auth_exhausted)
        self.assertEqual(self.closed(channel).code, 'DISCONNECTED')
        self.assertEqual(self.closed(channel, mutation=True).code, 'OUTCOME_UNKNOWN')
        self.assertEqual(channel.host_key, PIN)

    def test_verified_channel_does_not_accept_endpoint_auth_diagnostics(self):
        channel = self.channel()
        channel._verified_key = channel._verified_channel = True
        channel._diagnostic(EXHAUSTED_RECORD)
        self.assertFalse(channel._auth_exhausted)
        self.assertEqual(self.closed(channel, mutation=True).code, 'OUTCOME_UNKNOWN')

    def test_eof_pending_mismatched_pin_is_immediate_security_refusal(self):
        channel = self.channel()
        with self.assertRaises(NodeError) as caught:
            self.closed(channel, pending=f'debug1: Server host key: ssh-ed25519 {OTHER_PIN}\n'.encode())
        self.assertEqual(classify_failure(caught.exception).kind, FailureKind.SECURITY)

    def test_broken_pipe_does_not_hide_pending_host_key_refusal(self):
        channel = self.channel()
        channel.process.stdin.fileno.return_value = 21
        channel.process.stdout.fileno.return_value = 22
        selector = Mock()
        selector.__enter__ = Mock(return_value=selector)
        selector.__exit__ = Mock(return_value=False)
        selector.select.return_value = [(SimpleNamespace(data='in', fd=21), 2)]
        pending = f'debug1: Server host key: ssh-ed25519 {OTHER_PIN}\n'.encode()
        with patch('cmw.nodes.transport.selectors.DefaultSelector', return_value=selector), \
                patch('cmw.nodes.transport.os.write', side_effect=BrokenPipeError(errno.EPIPE, 'closed')), \
                patch('cmw.nodes.transport.os.read', return_value=pending), \
                patch.object(channel, 'close') as close, self.assertRaises(NodeError) as caught:
            channel.exchange('capabilities')
        self.assertEqual(caught.exception.code, 'SSH_IDENTITY_CHANGED')
        self.assertEqual(classify_failure(caught.exception).kind, FailureKind.SECURITY)
        self.assertEqual(channel.host_key, PIN)
        close.assert_called_once()

    def test_connection_keeps_alias_and_pin_independent_of_address(self):
        connection = {'ssh_alias': 'cmw-mini', 'ssh_host_key': PIN,
                      'executable': '/example/cmw', 'config_root': '/example/node', 'queue': 'main'}
        channel = SSHTransport(connection)
        self.assertEqual(channel.argv[-2], 'cmw-mini')
        self.assertIn('StrictHostKeyChecking=yes', channel.argv)
        self.assertIn('ControlPath=none', channel.argv)
        self.assertEqual(channel.host_key, PIN)
        self.assertIsNone(channel.process)

    def test_close_discards_previous_negotiation_authority(self):
        channel = ProcessTransport([], host_key=PIN)
        channel._diagnostic(KEY_RECORD + EXHAUSTED_RECORD)
        channel._verified_channel = channel._authenticated = True
        channel.close()
        self.assertFalse(channel._verified_key)
        self.assertFalse(channel._verified_channel)
        self.assertFalse(channel._authenticated)
        self.assertFalse(channel._auth_exhausted)
        self.assertEqual(channel.host_key, PIN)
