"""No-subprocess controls for native-peer diagnostic interposition."""
from contextlib import contextmanager, redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from cmw.nodes.protocol import NodeError
from cmw.nodes.transport import ProcessTransport
from tests.campaign import closure_peer


class PeerDiagnosticsTests(unittest.TestCase):
    def test_endpoint_uses_same_cli_arguments_and_exit_without_protocol_noise(self):
        entered = []
        @contextmanager
        def recorder():
            entered.append(True)
            yield
        argv = ['-c', 'nodes', 'endpoint', '--config', '/task/node', '--queue', 'research']
        with patch.object(closure_peer.sys, 'argv', argv), \
             patch('tests.jobs.isolated_runtime.record_runtime_refusals', recorder), \
             patch('cmw.cli.main', return_value=7) as main, redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as caught:
                closure_peer.endpoint_main()
        self.assertEqual(caught.exception.code, 7)
        main.assert_called_once_with(argv[1:])
        self.assertEqual(entered, [True])
        self.assertEqual(output.getvalue(), '')

    def test_refusal_preserves_channel_pid_exit_and_one_original_request(self):
        trace = {'channel': 'analysis'}
        transport = closure_peer.PeerTransport(['/unexecuted'], trace=trace)
        child = Mock(pid=123, returncode=None)
        failure = NodeError('RUNTIME_MISMATCH', 'original refusal')
        def start(instance):
            instance.process = child
        def close(instance):
            child.returncode = 0
            instance.process = None
        def exchange(instance, *args, **kwargs):
            instance._start()
            instance.close()
            raise failure
        identity = Mock(); identity.create_time.return_value = 456.
        with patch.object(ProcessTransport, '_start', start), \
             patch.object(ProcessTransport, 'close', close), \
             patch.object(ProcessTransport, 'exchange', autospec=True, side_effect=exchange) as original, \
             patch.object(closure_peer.psutil, 'Process', return_value=identity):
            with self.assertRaises(NodeError) as caught:
                transport.exchange('progress', target={'attempt_id': 'original'})
        self.assertIs(caught.exception, failure)
        self.assertEqual(original.call_count, 1)
        self.assertEqual([event['event'] for event in trace['events']], ['request', 'launch', 'close', 'refusal_or_error'])
        self.assertEqual(trace['events'][1]['pid'], 123)
        self.assertEqual(trace['events'][1]['birth'], 456.)
        self.assertEqual(trace['events'][2]['returncode'], 0)
        self.assertEqual(trace['events'][3]['code'], 'RUNTIME_MISMATCH')

    def test_connect_uses_isolated_endpoint_recorder_and_explicit_test_only_support(self):
        record = {'entry': {'config_root': '/task/node'}}
        traces = {}
        with patch('cmw.nodes.connections.ConnectedBackend') as backend:
            closure_peer.connect(record, Path('/installed/bin/python'), label='analysis', traces=traces)
        transport = backend.call_args.kwargs['transport']
        self.assertEqual(transport.argv[:4], ['/installed/bin/python', '-I', '-B', '-c'])
        self.assertEqual(transport.argv[5], str(closure_peer.SUPPORT))
        self.assertIn('endpoint_main()', transport.argv[4])
        self.assertEqual(transport.argv[6:], ['nodes', 'endpoint', '--config', '/task/node', '--queue', 'research'])
        self.assertIs(transport.trace, traces['analysis'])
        self.assertIsNone(transport.process)


if __name__ == '__main__':
    unittest.main()
