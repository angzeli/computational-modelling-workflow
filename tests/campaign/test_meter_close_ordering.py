"""Gated real-channel controls for close versus an in-flight observation read.

The selector returns a real ready descriptor. An event holds its consumption so
ordering is deliberate, not a timed race. No application outcome is reclassified.
"""
import errno
import json
import os
from pathlib import Path
import selectors
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tests.campaign.support import MeterTransport


ENDPOINT = '''import json,sys
message=json.loads(sys.stdin.buffer.readline())
reply={'protocol':1,'request_id':message['request_id'],'ok':True,'result':{'observed':True}}
sys.stdout.write(json.dumps(reply)+'\\n');sys.stdout.flush()
sys.stdin.buffer.read()
'''


def gated_exchange(order):
    ready, release = threading.Event(), threading.Event()
    real_selector = selectors.DefaultSelector
    timeline, result = [], {}
    process = None
    meter = MeterTransport([sys.executable, '-I', '-B', '-c', ENDPOINT], timeout=3)
    def event(kind, **values):
        timeline.append({'event': kind, 'at': time.monotonic(),
                         'thread_ident': threading.get_ident(), **values})
    class GatedSelector(real_selector):
        def select(self, timeout=None):
            events = super().select(timeout)
            if any(key.data == 'out' for key, _ in events) and not ready.is_set():
                event('actual_stdout_ready', descriptors=[key.fd for key, _ in events])
                ready.set()
                if not release.wait(3):
                    raise TimeoutError('Controlled ready-read release was not delivered')
                event('ready_read_released')
                if order == 'active_error':
                    raise OSError(errno.EIO, 'Controlled active-channel read failure')
            return events
    def exchange():
        try:
            result['value'] = meter.exchange('status', request_id='a'*32)
        except BaseException as exc:
            result['error'] = meter._meter_exception(exc)
    worker = threading.Thread(target=exchange, name='gated-meter-read', daemon=True)
    try:
        with patch('cmw.nodes.transport.selectors.DefaultSelector', GatedSelector):
            worker.start()
            if not ready.wait(3):
                raise AssertionError('Native endpoint did not reach the actual ready-read gate')
            process = meter.process
            result['original_pid'] = process.pid
            result['live_at_ready_gate'] = process.poll() is None
            if order == 'close_before_read':
                event('explicit_close_requested', pid=process.pid)
                meter.close()
                event('explicit_close_completed', pid=process.pid, returncode=process.returncode)
            release.set()
            worker.join(3)
            if worker.is_alive():
                raise AssertionError('The gated original exchange did not complete')
            if order != 'close_before_read':
                event('explicit_close_requested', pid=process.pid)
                meter.close()
                event('explicit_close_completed', pid=process.pid, returncode=process.returncode)
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(1)
        meter.close()
        result.update(order=order, worker_alive=worker.is_alive(), timeline=timeline,
            samples=meter.samples, owners=meter.owners,
            actual_child_returncode=process.returncode if process is not None else None,
            strict_all_requests_ok=bool(meter.samples) and all(s['outcome'] == 'ok' for s in meter.samples),
            historical_failure_cause='UNRESOLVED', acceptance_rule_changed=False)
        directory = os.environ.get('CMW_METER_GATE_EVIDENCE')
        if directory:
            with (Path(directory)/(order+'.json')).open('x') as stream:
                json.dump(result, stream, indent=2)
                stream.write('\n')
    return result


class MeterCloseOrderingTests(unittest.TestCase):
    def test_read_before_close_succeeds(self):
        result = gated_exchange('read_before_close')
        self.assertTrue(result['live_at_ready_gate'])
        self.assertTrue(result['strict_all_requests_ok'])
        self.assertEqual(result['value'], {'observed': True})
        self.assertEqual(result['actual_child_returncode'], 0)
        self.assertFalse(result['worker_alive'])

    def test_external_close_before_ready_read_causes_real_closed_descriptor_error(self):
        result = gated_exchange('close_before_read')
        self.assertTrue(result['live_at_ready_gate'])
        sample, = result['samples']
        self.assertEqual(sample['outcome'], 'UNREACHABLE')
        self.assertEqual(sample['error']['cause']['errno'], errno.EBADF)
        self.assertEqual(sample['response_bytes'], 0)
        self.assertFalse(sample['expected_refusal'])
        self.assertFalse(result['strict_all_requests_ok'])
        self.assertEqual(result['actual_child_returncode'], 0)
        events = result['owners'][0]['transport_events']
        close = next(item for item in events if item['event'] == 'close_start')
        end = next(item for item in events if item['event'] == 'close_end')
        self.assertEqual(close['active_requests'][0]['request_id'], sample['request_id'])
        self.assertNotEqual(close['thread_ident'], sample['thread_ident'])
        self.assertGreater(end['drained_bytes']['stdout'], 0)
        self.assertLessEqual(end['at'], sample['ended_at'])

    def test_active_error_stays_failed_despite_automatic_channel_cleanup(self):
        result = gated_exchange('active_error')
        sample, = result['samples']
        self.assertEqual(sample['outcome'], 'UNREACHABLE')
        self.assertEqual(sample['error']['cause']['errno'], errno.EIO)
        self.assertFalse(sample['expected_refusal'])
        self.assertFalse(result['strict_all_requests_ok'])
        close = next(item for item in result['owners'][0]['transport_events'] if item['event'] == 'close_start')
        explicit = next(item for item in result['timeline'] if item['event'] == 'explicit_close_requested')
        self.assertEqual(close['thread_ident'], sample['thread_ident'])
        self.assertLess(close['at'], explicit['at'])
        self.assertEqual(result['actual_child_returncode'], 0)
        self.assertFalse(result['worker_alive'])


if __name__ == '__main__':
    unittest.main()
