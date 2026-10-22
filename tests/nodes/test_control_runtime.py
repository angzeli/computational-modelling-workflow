"""Real endpoint/worker control with benign processes and response-only fault injection.

The test launcher reuses isolated admission and records real signal syscalls;
Store, node control, suspension decisions and the kernel are not substituted.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import psutil

from cmw.jobs import runtime, suspension
from cmw.jobs.ownership import group_members, identity, owner_alive
from cmw.jobs.store import Store, private_opener
from cmw.nodes.client import apply_control
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.identity import enroll
from cmw.nodes.protocol import NodeError
from cmw.nodes.transport import ProcessTransport
from tests.jobs.isolated_runtime import environment, install
from tests.jobs.lifecycle_evidence import capture, retain_lifecycle_evidence


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('Owned node control fixture did not reach the bounded condition')


@contextmanager
def record_real_signals(root):
    """Observe caller identity without changing signal targets or decisions."""
    root = Path(root)
    original_kill, original_killpg = os.kill, os.killpg
    def send(function, kind, target, signum):
        if signum:
            record = {'sender': identity(), 'kind': kind, 'target': target,
                      'signal': signum, 'time': time.time()}
            with open(root / 'node-control-signals.jsonl', 'a', opener=private_opener) as handle:
                handle.write(json.dumps(record) + '\n')
        return function(target, signum)
    with patch.object(os, 'kill', lambda pid, sig: send(original_kill, 'pid', pid, sig)), \
            patch.object(os, 'killpg', lambda pgid, sig: send(original_killpg, 'group', pgid, sig)):
        yield


def traced_detached(arguments, log):
    """Fixture-only launcher; the production runtime still owns every child."""
    log.parent.mkdir(parents=True, exist_ok=True)
    runtime.reap_detached()
    with open(log, 'ab', buffering=0, opener=private_opener) as output:
        process = subprocess.Popen([sys.executable, '-B', '-m', __name__ if __name__ != '__main__'
                                    else 'tests.nodes.test_control_runtime', 'runtime', *arguments],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
            close_fds=True, env=environment(), cwd='/')
    runtime._CHILDREN.append(process)
    return process


class DelayedReply:
    """Lose one actual committed response, without altering endpoint execution."""
    def __init__(self, stream, request_id, root):
        self.stream, self.request_id, self.root = stream, request_id, Path(root)

    def write(self, packet):
        reply = json.loads(packet)
        if reply.get('request_id') == self.request_id and reply.get('ok'):
            (self.root / 'reply-delayed-after-commit').write_text(self.request_id)
            time.sleep(5)
        return self.stream.write(packet)

    def flush(self):
        self.stream.flush()


class NodeControlRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.started_at = time.time()
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-node-runtime-')
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root / 'calculation'
        self.storage.mkdir(mode=0o700)
        self.config_root = self.root / 'node'
        self.config = enroll(self.config_root, {'queues': {'research': str(self.root / 'queue')},
            'storages': {'scratch': str(self.storage)},
            'profiles': {'benign': {'engine': 'Command', 'runner': '/bin/sh', 'max_cpus': 1}}})
        self.store = Store(self.root / 'queue')
        self.entry = {'transport': 'local', 'config_root': str(self.config_root),
            'node_id': self.config['node_id'], 'host_id': self.config['host_id'],
            'runtime_id': self.config['runtime']['fingerprint'],
            'queues': {'research': self.config['queues']['research']['queue_id']},
            'default_queue': 'research', 'read_only': False}
        self.frontend = self.root / 'frontend'
        self.owners, self.backends, self.endpoints = [], [], []
        self.backend = self.connect()

    def connect(self, lost_request=None):
        args = [sys.executable, '-B', '-m', 'tests.nodes.test_control_runtime',
                'endpoint', str(self.config_root), str(self.store.root)]
        if lost_request is not None:
            args.append(lost_request)
        transport = ProcessTransport(args, timeout=1.5 if lost_request else 15, env=environment())
        backend = ConnectedBackend('fixture-mini', self.entry, transport=transport)
        backend.call('capabilities')
        self.backends.append(backend)
        self.endpoints.append(transport.process)
        return backend

    def job(self):
        return self.store.snapshot()['jobs'][0]

    def phase(self, expected):
        job = self.job()
        if job['status'] in {'Unknown', 'Fail'} or job['suspension']['phase'] == 'Unverified':
            raise AssertionError(json.dumps(job, indent=2))
        return job if job['suspension']['phase'] == expected else None

    def preview(self, action, backend=None):
        return (backend or self.backend).call('control.preview', {'action': action},
            None if '.' in action else self.target)['result']

    def apply(self, plan, *, backend=None, request_id=None):
        return apply_control(backend or self.backend, plan, root=self.frontend,
            confirm=True, request_id=request_id)['result']

    def duplicate(self, plan, request_id):
        params = {key: plan[key] for key in ('action', 'expected_clock_id', 'expected_revision')}
        params.update(confirm=True, reason='')
        return self.backend.call('control', params, plan['target'], request_id=request_id)['result']

    def counter(self):
        return json.loads((self.storage / 'counter.json').read_text())

    def signals(self):
        path = self.store.root / 'node-control-signals.jsonl'
        return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines()]

    def remember(self):
        state = self.store.snapshot()
        owners = [state['controller'].get('owner')]
        for job in state['jobs']:
            value = job.get('suspension') or {}
            owners.extend([job.get('worker'), job.get('group'), (value.get('capability') or {}).get('runner'),
                           *(value.get('evidence') or {}).get('computational', []),
                           *(value.get('evidence') or {}).get('protected', [])])
        for owner in owners:
            if owner is not None and owner not in self.owners:
                self.owners.append(owner)

    def tearDown(self):
        try:
            self.remember()
            if self.store.path.exists():
                for job in self.store.snapshot()['jobs']:
                    if job['status'] in {'Run', 'Starting'}:
                        self.store.change(job['id'], 'cancel', confirm=True)
                wait_for(lambda: not any(job['status'] in {'Run', 'Starting', 'Cancelling'}
                                        for job in self.store.snapshot()['jobs']), timeout=20)
                runtime.stop(self.store)
                wait_for(lambda: not self.store.snapshot()['controller']['online'])
            for backend in self.backends:
                backend.close()
            for process in self.endpoints:
                process.wait(timeout=5)
            wait_for(lambda: not any(owner_alive(owner) for owner in self.owners), timeout=15)
            runtime.reap_detached()
            evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
            if evidence:
                destination = Path(evidence) / ('control-runtime-cleanup-' + uuid4().hex)
                destination.mkdir(mode=0o700, parents=True)
                (destination / 'cleanup.json').write_text(json.dumps({
                    'owners': self.owners, 'remaining_verified_owners': [],
                    'endpoint_exit_codes': [process.returncode for process in self.endpoints]}, indent=2))
        except BaseException as exc:
            evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
            destination = Path(tempfile.mkdtemp(prefix='node-control-teardown-', dir=evidence))
            capture(self.store, destination, outcome=f'TEARDOWN {type(exc).__name__}: {exc}', started_at=self.started_at)
            raise
        finally:
            for backend in self.backends:
                backend.close()
            self.temp.cleanup()

    @retain_lifecycle_evidence
    def test_remote_suspend_continue_cancel_lost_reply_and_duplicate_suppression(self):
        code = '''import json,os,time,sys
from pathlib import Path
root=Path(sys.argv[1]); seq=0; sentinel=17391
while True:
 seq+=1
 temporary=root/'counter.tmp'
 temporary.write_text(json.dumps({'pid':os.getpid(),'seq':seq,'sentinel':sentinel}))
 temporary.replace(root/'counter.json')
 sum(range(500)); time.sleep(.03)
'''
        job = self.store.add(argv=[sys.executable, '-m', 'cmw.jobs.suspension_protocol',
            '--execution-budget', '35', '--hard-timeout', '45', '--',
            sys.executable, '-c', code, str(self.storage)], cwd=self.storage,
            name='Benign endpoint suspension counter', cpus=1, memory_gib=.1,
            suspension_backend='local-command-v1')
        self.target = {'node_id': self.entry['node_id'], 'queue_id': self.entry['queues']['research'],
                       'job_id': job['id'], 'attempt_id': job['attempt_id']}
        self.apply(self.preview('controller.start'))
        wait_for(lambda: (self.storage / 'counter.json').exists())
        wait_for(lambda: suspension.readiness(self.store, job['id'], 'suspend')['ready'])
        self.remember()
        original = self.job()
        worker = original['worker']
        computational = original['suspension']['evidence']['computational']
        self.assertEqual(len(computational), 1)
        member = computational[0]

        suspend_plan = self.preview('suspend'); suspend_id = uuid4().hex
        suspended_receipt = self.apply(suspend_plan, request_id=suspend_id)
        self.assertEqual(suspended_receipt['status'], 'ACCEPTED')
        self.backend.close()
        self.backend = self.connect()
        stopped = wait_for(lambda: self.phase('Suspended'))
        paused = self.counter(); time.sleep(.2)
        self.assertEqual(self.counter(), paused)
        self.assertEqual(psutil.Process(member['pid']).status(), psutil.STATUS_STOPPED)
        self.assertTrue(all(psutil.Process(item['pid']).status() != psutil.STATUS_STOPPED
                            for item in stopped['suspension']['evidence']['protected']))
        before = self.signals()
        self.assertEqual(self.duplicate(suspend_plan, suspend_id), suspended_receipt)
        time.sleep(.15)
        self.assertEqual(self.signals(), before)
        self.assertEqual(self.job()['suspension']['revision'], 1)

        continue_plan = self.preview('continue'); continue_id = uuid4().hex
        delayed = self.connect(lost_request=continue_id)
        with self.assertRaises(NodeError) as uncertain:
            self.apply(continue_plan, backend=delayed, request_id=continue_id)
        self.assertEqual(uncertain.exception.code, 'OUTCOME_UNKNOWN')
        self.assertEqual((self.store.root / 'reply-delayed-after-commit').read_text(), continue_id)
        wait_for(lambda: self.phase('Run'))
        wait_for(lambda: self.counter()['seq'] > paused['seq'])
        resumed = self.job()
        self.assertEqual(resumed['worker'], worker)
        self.assertEqual(resumed['attempt_id'], original['attempt_id'])
        self.assertEqual(identity(member['pid']), member)
        self.assertEqual(self.counter()['sentinel'], paused['sentinel'])
        self.backend.close()
        self.backend = self.connect()
        recovered = self.backend.call('request', {'request_id': continue_id})['result']['request']['result']
        self.assertEqual(recovered['status'], 'ACCEPTED')
        before = self.signals()
        self.assertEqual(self.duplicate(continue_plan, continue_id), recovered)
        time.sleep(.2)
        self.assertEqual(self.signals(), before)
        self.assertEqual(self.job()['suspension']['revision'], 2)
        self.assertEqual(len(self.job()['suspension']['intervals']), 1)

        cancel_plan = self.preview('cancel'); cancel_id = uuid4().hex
        cancelled_receipt = self.apply(cancel_plan, request_id=cancel_id)
        self.assertEqual(cancelled_receipt['status'], 'ACCEPTED')
        wait_for(lambda: self.job()['status'] == 'Cancelled')
        self.assertFalse(group_members(original['group']['pid'], session=True))
        before = self.signals()
        self.assertEqual(self.duplicate(cancel_plan, cancel_id), cancelled_receipt)
        time.sleep(.2)
        self.assertEqual(self.signals(), before)
        self.assertEqual(self.job()['attempt_id'], original['attempt_id'])
        events = self.store.snapshot()['events']
        self.assertEqual(sum(event['message'] == 'Cancel requested' for event in events), 1)
        signals = self.signals()
        delivered = [item for item in signals if item['target'] == member['pid']]
        self.assertTrue(any(item['signal'] == signal.SIGSTOP for item in delivered), signals)
        self.assertTrue(any(item['signal'] == signal.SIGCONT for item in delivered), signals)
        self.assertTrue(any(item['signal'] == signal.SIGTERM for item in delivered), signals)
        self.assertTrue(all(item['sender'] == worker for item in delivered), delivered)
        self.assertFalse(any(item['sender']['pid'] in {process.pid for process in self.endpoints}
                             for item in signals), signals)
        evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
        if evidence:
            destination = Path(evidence) / ('control-runtime-signals-' + uuid4().hex + '.json')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps({'worker': worker, 'computational': computational,
                'signals': signals, 'suspend_receipt': suspended_receipt,
                'continue_receipt_after_timeout': recovered, 'cancel_receipt': cancelled_receipt}, indent=2))


def child_main():
    kind = sys.argv[1]
    with install(), patch.object(runtime, 'detached', traced_detached):
        if kind == 'endpoint':
            from cmw.nodes.endpoint import serve
            config, root = sys.argv[2:4]
            output = DelayedReply(sys.stdout.buffer, sys.argv[4], root) if len(sys.argv) > 4 else sys.stdout.buffer
            with record_real_signals(root):
                raise SystemExit(serve(config, 'research', output_stream=output))
        if kind == 'runtime':
            del sys.argv[1]
            with record_real_signals(sys.argv[2]):
                runtime.main()
            return
        raise ValueError('Unknown isolated child mode')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in {'endpoint', 'runtime'}:
        child_main()
    else:
        unittest.main()
