import io
import json
import sys
import unittest
from uuid import uuid4

from cmw.nodes.endpoint import serve
from cmw.nodes.protocol import MAX_FRAME, NodeError, decode, encode, request, response, validate_request, validate_envelope
from cmw.nodes.transport import ProcessTransport, ssh_command


class ProtocolTests(unittest.TestCase):
    def test_strict_version_target_and_duplicate_keys(self):
        message = request('status')
        self.assertEqual(decode(encode(message)), message)
        for raw in (b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b'noise\n', b'{}', b'[]\n'):
            with self.assertRaises(NodeError):
                decode(raw)
        for value in (True, 0, 2, '1'):
            with self.assertRaises(NodeError):
                validate_request({**message, 'protocol': value})
        with self.assertRaises(NodeError):
            request('show', target={'job_id': 'J1.1'})
        with self.assertRaises(NodeError):
            encode({'data': 'x' * MAX_FRAME})
        with self.assertRaises(NodeError):
            request('shell', {'script': 'echo forbidden'})

    def test_response_must_match_outstanding_request(self):
        identity = uuid4().hex
        valid = dict(protocol=1, request_id=identity, ok=True, result={})
        self.assertEqual(response(valid, identity), {})
        for bad in ({**valid, 'request_id': uuid4().hex}, {**valid, 'extra': 1}, {**valid, 'result': []}):
            with self.assertRaises(NodeError):
                response(bad, identity)

    def test_bootstrap_contains_only_fixed_settings_with_shell_quoting(self):
        import shlex
        argv = ssh_command('mini-office', "/safe runtime/cmw'quoted", '/private/node config', 'research')
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('ForwardAgent=no', argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(shlex.split(argv[-1]), ["/safe runtime/cmw'quoted", 'nodes', 'endpoint', '--config', '/private/node config', '--queue', 'research'])
        for alias in ('-oProxyCommand=x', 'mini; echo x', 'user@guessed-host', ''):
            with self.assertRaises(NodeError):
                ssh_command(alias, '/bin/cmw', '/node', 'research')

    def test_endpoint_keeps_machine_stdout_and_closes_observers(self):
        class Backend:
            closed = False
            def __init__(self, *_):
                pass
            def call(self, operation, params, target, *, request_id):
                return {'operation': operation, 'parameters': params}
            def close(self):
                Backend.closed = True
        message = request('status', {'literal': '; $(touch nothing)'})
        output = io.BytesIO()
        self.assertEqual(serve('/unused', 'test', input_stream=io.BytesIO(encode(message)), output_stream=output, backend_factory=Backend), 0)
        result = response(decode(output.getvalue()), message['request_id'])
        self.assertEqual(result['parameters'], message['params'])
        self.assertTrue(Backend.closed)

    def test_real_pipe_protocol_and_noisy_truncated_bounded_failures(self):
        script = "import sys,json\nfor line in sys.stdin:\n q=json.loads(line);print(json.dumps(dict(protocol=1,request_id=q['request_id'],ok=True,result={'operation':q['operation']})),flush=True)"
        channel = ProcessTransport([sys.executable, '-I', '-B', '-c', script], timeout=2)
        try:
            self.assertEqual(channel.exchange('status'), {'operation': 'status'})
            pid = channel.process.pid
            self.assertEqual(channel.exchange('capabilities'), {'operation': 'capabilities'})
            self.assertEqual(channel.process.pid, pid)
        finally:
            process = channel.process
            channel.close()
        self.assertIsNotNone(process.returncode)
        for script in ("print('noise')", "import sys;sys.stdout.write('{}');sys.stdout.flush()", "print('x' * 2200000)"):
            channel = ProcessTransport([sys.executable, '-I', '-B', '-c', script], timeout=2)
            with self.assertRaises(NodeError):
                channel.exchange('status')
            self.assertIsNone(channel.process)

    def test_timeout_after_mutation_is_unknown_outcome_not_job_failure(self):
        script = "import sys,json,time\nfor line in sys.stdin:\n q=json.loads(line)\n if q['operation']=='submit': time.sleep(3)\n print(json.dumps(dict(protocol=1,request_id=q['request_id'],ok=True,result={})),flush=True)"
        channel = ProcessTransport([sys.executable, '-I', '-B', '-c', script], timeout=.3)
        channel.exchange('capabilities')
        with self.assertRaises(NodeError) as caught:
            channel.exchange('submit', request_id=uuid4().hex)
        self.assertEqual(caught.exception.code, 'OUTCOME_UNKNOWN')
        self.assertIsNone(channel.process)

    def test_ssh_host_key_is_distinct_from_endpoint_uuid(self):
        channel = ProcessTransport([], host_key='SHA256:' + 'a' * 43)
        channel._diagnostic(b'debug1: Server host key: ssh-ed25519 SHA256:' + b'a' * 43 + b'\n')
        self.assertTrue(channel._verified_key)
        with self.assertRaises(NodeError):
            channel._diagnostic(b'debug1: Server host key: ssh-ed25519 SHA256:' + b'b' * 43 + b'\n')
        channel._diagnostic(b'x' * 100000)
        self.assertLessEqual(len(channel.stderr), 65536)

    def test_envelope_shape_is_validated_before_cache_publication(self):
        envelope = dict(schema=1, node_id=uuid4().hex, queue_id=uuid4().hex,
                        queue_alias='research', session_id=uuid4().hex, sequence=1,
                        observed_at=1.0, clock_id='a' * 64, runtime={}, host={}, result={})
        self.assertEqual(validate_envelope(envelope), envelope)
        for field, bad in (('schema', 999), ('schema', True), ('sequence', False),
                           ('sequence', -1), ('session_id', 'missing'), ('clock_id', 1),
                           ('observed_at', float('nan')), ('runtime', []), ('host', None), ('result', [])):
            with self.subTest(field=field, bad=bad), self.assertRaises(NodeError):
                validate_envelope({**envelope, field: bad})
        with self.assertRaises(NodeError):
            validate_envelope({key: value for key, value in envelope.items() if key != 'session_id'})

    def test_no_mutating_bytes_before_verified_capability_handshake(self):
        channel = ProcessTransport([sys.executable, '-I', '-B', '-c', 'import sys;sys.stdin.read()'], timeout=1)
        with self.assertRaises(NodeError) as caught:
            channel.exchange('submit')
        self.assertEqual(caught.exception.code, 'HANDSHAKE_REQUIRED')
        self.assertIsNone(channel.process)
