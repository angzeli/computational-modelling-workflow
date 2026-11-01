"""Pure controls for request/close diagnostics; no endpoint is launched."""
import errno
import unittest
from unittest.mock import Mock, patch

from cmw.nodes.protocol import NodeError
from cmw.nodes.transport import ProcessTransport
from tests.campaign.support import MeterTransport


class MeterDiagnosticsTests(unittest.TestCase):
    def test_original_request_error_and_cause_are_retained_without_retry(self):
        meter = MeterTransport(['/unexecuted'])
        original = NodeError('UNREACHABLE', 'original descriptor failure')
        original.__cause__ = OSError(errno.EBADF, 'Bad file descriptor')
        target = {'node_id': 'c'*32, 'queue_id': 'd'*32, 'job_id': 2, 'attempt_id': 'e'*32}
        with patch.object(ProcessTransport, 'exchange', side_effect=original) as exchange:
            with self.assertRaises(NodeError) as caught:
                meter.exchange('progress', target=target, request_id='a'*32)
        self.assertIs(caught.exception, original)
        exchange.assert_called_once_with('progress', None, target, request_id='a'*32)
        sample, = meter.samples
        self.assertEqual(sample['outcome'], 'UNREACHABLE')
        self.assertEqual(sample['error']['cause']['errno'], errno.EBADF)
        self.assertEqual(sample['error']['message'], str(original))
        self.assertGreaterEqual(sample['ended_at'], sample['started_at'])
        self.assertEqual(sample['seconds'], sample['ended_at']-sample['started_at'])
        self.assertEqual(sample['response_bytes'], 0)
        self.assertFalse(sample['expected_refusal'])
        self.assertFalse(meter._meter_active)

    def test_close_overlap_keeps_request_and_actual_process_exit(self):
        meter = MeterTransport(['/unexecuted'])
        process = Mock(pid=123, returncode=0)
        process.poll.return_value = 0
        original = NodeError('UNREACHABLE', 'original close overlap')
        def start(instance):
            instance.process = process
        def close(instance):
            instance.process = None
        def exchange(instance, *args, **kwargs):
            instance._start()
            instance.close()
            raise original
        with patch.object(ProcessTransport, '_start', start), \
             patch.object(ProcessTransport, 'close', autospec=True, side_effect=close) as closer, \
             patch.object(ProcessTransport, 'exchange', autospec=True, side_effect=exchange) as request, \
             patch('tests.campaign.support.process_identity', return_value={'pid': 123, 'birth': 456.}):
            with self.assertRaises(NodeError) as caught:
                meter.exchange('progress', request_id='b'*32)
        self.assertIs(caught.exception, original)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(closer.call_count, 1)
        owner, = meter.owners
        events = owner['transport_events']
        self.assertEqual([item['event'] for item in events], ['launch', 'close_start', 'close_end'])
        self.assertEqual(events[1]['active_requests'][0]['request_id'], 'b'*32)
        self.assertTrue(events[1]['caller'])
        self.assertEqual(events[-1]['returncode'], 0)
        self.assertEqual(events[-1]['drained_bytes'], {'stdout': 0, 'stderr': 0})
        self.assertLessEqual(meter.samples[0]['started_at'], events[1]['at'])
        self.assertLessEqual(events[-1]['at'], meter.samples[0]['ended_at'])

    def test_existing_close_drain_counts_bytes_without_decoding_or_reclassifying(self):
        meter = MeterTransport(['/unexecuted'])
        process = Mock(pid=123, returncode=None)
        process.poll.return_value = None
        process.stdout.closed = process.stderr.closed = False
        process.stdout.fileno.return_value = 10
        process.stderr.fileno.return_value = 11
        owner = {'pid': 123, 'birth': 456.}
        meter.process, meter._meter_owner = process, owner
        def close(instance):
            process.returncode = 0
            instance.process = None
        with patch('tests.campaign.support.select.select', side_effect=[
                ([process.stdout, process.stderr], [], []), ([process.stdout, process.stderr], [], [])]), \
             patch('tests.campaign.support.os.read', side_effect=[b'response', b'diagnostic', b'', b'']), \
             patch.object(ProcessTransport, 'close', autospec=True, side_effect=close) as closer, \
             patch('linecache.getline', side_effect=AssertionError('Trace must not reread runtime source')):
            meter.close()
        closer.assert_called_once_with(meter)
        event = owner['transport_events'][-1]
        self.assertEqual(event['drained_bytes'], {'stdout': 8, 'stderr': 10})
        self.assertEqual(event['stderr_tail'], 'diagnostic')
        self.assertEqual(event['returncode'], 0)
        self.assertFalse(meter.samples)

    def test_close_failure_and_trace_bound_are_preserved(self):
        meter = MeterTransport(['/unexecuted'])
        process = Mock(pid=123, returncode=0)
        process.poll.return_value = 0
        owner = {'pid': 123, 'birth': 456.}
        meter.process, meter._meter_owner = process, owner
        original = OSError('original close failure')
        with patch.object(ProcessTransport, 'close', side_effect=original) as close:
            with self.assertRaises(OSError) as caught:
                meter.close()
        self.assertIs(caught.exception, original)
        close.assert_called_once()
        self.assertEqual(owner['transport_events'][-1]['error']['message'], str(original))
        for index in range(40):
            meter._meter_event(owner, 'bounded-probe', index=index)
        self.assertEqual(len(owner['transport_events']), 32)
        self.assertEqual(owner['transport_events_truncated'], 10)


if __name__ == '__main__':
    unittest.main()
