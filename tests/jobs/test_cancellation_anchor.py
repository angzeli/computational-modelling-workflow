"""Deterministic worker/receipt races; every signalled identity is task-owned."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import ownership, runtime, suspension
from cmw.jobs.store import Store


class WorkerCancellationModel:
    """A bounded process model around the real worker and private Store."""
    def __init__(self, root, mode):
        self.root, self.mode, self.clock = root, mode, 0.0
        self.store = Store(root/'state')
        self.job = self.store.add(argv=['/bin/true'], cwd=root, name='controlled cancellation',
                                  cpus=2, memory_gib=.25, suspension_backend='local-command-v1')
        self.claim = 'fixture-claim'
        self.events, self.signals, self.checked = [], [], []
        self.anchor, self.runner, self.compute, self.writer, self.sentinel = 101, 102, 201, 103, 999
        self.owners = {pid: dict(pid=pid, birth=float(pid), host='fixture', boot=1)
                       for pid in (self.anchor, self.runner, self.compute, self.writer, self.sentinel, 99)}
        self.alive = {self.anchor, self.runner, self.compute, self.sentinel, 99}
        self.receipt = self.store.root/'attempts'/self.job['attempt_id']/'payload-exit.json'
        self.released = False
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(status='Starting', claim=self.claim, claimed_at=1, order=None)
            self.store.save(con, job)
            control = self.store.control(con)
            control.update(dispatch=True, stop=False)
            self.store.set_control(con, control)
        self.leader = Mock(pid=self.anchor)
        self.leader.stdin.write.side_effect = self.pipe
        self.leader.wait.side_effect = self.wait
        self.coordinator = None

    def make_coordinator(self, store, job, anchor):
        coordinator = self.coordinator_type(store, job, anchor)
        coordinator.registration = {'root': self.owners[self.compute], 'runner': self.owners[self.runner],
                                    'protected': [], 'strategy': 'direct-tree'}
        def domain():
            coordinator.observe_session()
            return suspension.Domain((self.owners[self.compute],),
                                     (self.owners[self.anchor], self.owners[self.runner]), ())
        coordinator._validate = domain
        coordinator.last_domain = domain()
        def acknowledge(*args):
            if self.mode == 'receipt-during-ack':
                self.alive.difference_update((self.runner, self.compute))
                self.write_receipt()
                return None
            return {'status': 'cancel_prepared'}
        coordinator.ack = Mock(side_effect=acknowledge)
        prepare_cancel = coordinator.prepare_cancel
        def prepare(job):
            result = prepare_cancel(job)
            self.prepare(job)
            return result
        coordinator.prepare_cancel = prepare
        self.coordinator = coordinator
        return coordinator

    def process(self, pid):
        model = self
        if pid not in model.alive:
            raise psutil.NoSuchProcess(pid)
        class Process:
            def status(self):
                if pid not in model.alive:
                    raise psutil.NoSuchProcess(pid)
                return psutil.STATUS_RUNNING
            def ppid(self):
                return model.runner if pid == model.compute else model.anchor
        return Process()

    def send_pid(self, pid, signum):
        assert pid in self.alive, 'Unverified numeric PID targeted'
        assert pid not in (self.anchor, self.runner, self.writer, self.sentinel), 'Protected control was targeted'
        assert pid in self.checked, 'Target birth was not checked'
        self.signals.append((pid, signum))
        self.events.append(('signal', pid, signum))
        if pid == self.compute and signum == signal.SIGTERM and self.mode != 'no-receipt-escalation':
            self.alive.difference_update((self.compute, self.runner))
            self.alive.add(self.writer)
            self.events.append('receipt-writer-started')
        if signum == signal.SIGKILL:
            self.alive.remove(pid)

    def current(self):
        with self.store.transaction() as con:
            return self.store.get(con, self.job['id'])

    def prepare(self, job):
        self.events.append('prepared')
        if self.mode == 'anchor-lost':
            self.alive.remove(self.anchor)
        if self.mode == 'worker-replaced':
            with self.store.transaction() as con:
                latest = self.store.get(con, self.job['id'])
                latest['worker'] = {**latest['worker'], 'birth': 999.0}
                self.store.save(con, latest)
        return True

    def pipe(self, data):
        self.events.append(data.decode().strip())
        if data == b'GO\n':
            with self.store.transaction() as con:
                job = self.store.get(con, self.job['id'])
                job.update(status='Cancelling', cancel_requested=True)
                self.store.save(con, job)
            if self.mode == 'receipt-first':
                self.alive.difference_update((self.runner, self.compute))
                self.write_receipt()
        elif data == b'RELEASE\n':
            assert self.receipt.exists(), 'RELEASE without a receipt'
            assert self.members(self.anchor, session=True) == [self.anchor], 'RELEASE with live descendants'
            assert self.anchor in self.alive, 'RELEASE after authority loss'
            self.released = True
            self.alive.remove(self.anchor)

    def wait(self, timeout=None):
        assert self.anchor not in self.alive, 'Reaping a live authority'
        return 0

    def write_receipt(self):
        self.receipt.write_text(json.dumps(dict(exit_code=143, recorded_at=1)))
        self.events.append('receipt')

    def identify(self, pid=None):
        pid = 99 if pid is None else pid
        self.checked.append(pid)
        if pid not in self.alive:
            raise psutil.NoSuchProcess(pid)
        return self.owners[pid]

    def owner_alive(self, owner):
        self.checked.append(owner['pid'])
        return owner == self.owners.get(owner['pid']) and owner['pid'] in self.alive

    def sid(self, pid):
        if pid not in self.alive:
            raise ProcessLookupError()
        return self.sentinel if pid == self.sentinel else self.anchor

    def pgid(self, pid):
        self.sid(pid)
        return self.compute if pid == self.compute else self.sentinel if pid == self.sentinel else self.anchor

    def members(self, pgid, *, session=False):
        return sorted(pid for pid in self.alive if pid != 99 and
                      (self.sid(pid) if session else self.pgid(pid)) == pgid)

    def send_group(self, pgid, signum):
        targets = [pid for pid in self.alive if pid != 99 and self.pgid(pid) == pgid]
        if not targets:
            raise ProcessLookupError()
        if not signum:
            return
        assert self.sentinel not in targets, 'Unrelated sentinel was targeted'
        assert all(pid in self.checked for pid in targets if pid in (self.anchor, self.compute))
        self.signals.append((pgid, signum))
        self.events.append(('signal', pgid, signum))
        if pgid == self.compute and signum == signal.SIGTERM and self.mode != 'no-receipt-escalation':
            self.alive.difference_update((self.compute, self.runner))
            self.alive.add(self.writer)
            self.events.append('receipt-writer-started')
        if pgid == self.anchor and signum == signal.SIGTERM and self.writer in self.alive:
            self.alive.difference_update((self.writer, self.anchor))
            self.events.append('receipt-writer-term-anchor-exit125')
        if signum == signal.SIGKILL:
            self.alive.difference_update(targets)

    def sleep(self, interval):
        self.clock += interval
        if self.clock > 10:
            raise AssertionError('Modeled cancellation exceeded its bounded lifecycle')
        if self.writer in self.alive:
            self.alive.remove(self.writer)
            self.write_receipt()

    def run(self):
        guard = dict(state='NO_MATCH', observations=[], reason='', observed_at=1)
        self.coordinator_type = suspension.Coordinator
        with ExitStack() as stack:
            for target, value in (
                ('cmw.jobs.runtime.subprocess.Popen', self.leader),
                ('cmw.jobs.runtime.activity.DEFAULT_OBSERVER.scan', guard),
                ('cmw.jobs.runtime.sharing.evaluate', {'permitted': True}),
                ('cmw.jobs.runtime.dispatch_authority', True),
            ):
                stack.enter_context(patch(target, return_value=value))
            stack.enter_context(patch('cmw.jobs.suspension.Coordinator', side_effect=self.make_coordinator))
            stack.enter_context(patch.object(self.store, 'snapshot', side_effect=lambda: {'jobs': [self.current()]}))
            stack.enter_context(patch('cmw.jobs.runtime.record_guard'))
            stack.enter_context(patch('cmw.jobs.runtime.signal.signal'))
            for module in (runtime, ownership, suspension):
                stack.enter_context(patch.object(module, 'identity', side_effect=self.identify))
                stack.enter_context(patch.object(module, 'owner_alive', side_effect=self.owner_alive))
                stack.enter_context(patch.object(module, 'group_members', side_effect=self.members))
            stack.enter_context(patch('os.getsid', side_effect=self.sid))
            stack.enter_context(patch('os.getpgid', side_effect=self.pgid))
            stack.enter_context(patch('os.killpg', side_effect=self.send_group))
            stack.enter_context(patch('os.kill', side_effect=self.send_pid))
            stack.enter_context(patch.object(ownership.psutil, 'pids', side_effect=lambda: sorted(self.alive-{99})))
            stack.enter_context(patch.object(ownership.psutil, 'Process', side_effect=self.process))
            stack.enter_context(patch('time.monotonic', side_effect=lambda: self.clock))
            stack.enter_context(patch('time.sleep', side_effect=self.sleep))
            runtime.worker(self.store, self.job['id'], self.claim)
        assert self.sentinel in self.alive
        return self.current()


class CancellationAnchorTests(unittest.TestCase):
    def model(self, mode):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-anchor-model-')
        self.addCleanup(temporary.cleanup)
        return WorkerCancellationModel(Path(temporary.name), mode)

    def test_receipt_writer_in_anchor_group_survives_computational_cancellation(self):
        model = self.model('receipt-race')
        job = model.run()
        self.assertEqual(job['status'], 'Cancelled', model.events)
        self.assertTrue(model.released)
        self.assertFalse(any(group == model.anchor for group, sig in model.signals if sig))
        self.assertLess(model.events.index('receipt'), model.events.index('RELEASE'))
        self.assertEqual(job['attempt_id'], model.job['attempt_id'])

    def test_genuine_receipt_racing_cancel_releases_without_signalling_anchor(self):
        model = self.model('receipt-first')
        job = model.run()
        self.assertEqual(job['status'], 'Cancelled', model.events)
        self.assertTrue(model.released)
        self.assertEqual(model.signals, [])
        self.assertLess(model.events.index('receipt'), model.events.index('RELEASE'))

    def test_genuine_receipt_during_cancel_ack_wait_wins_without_signal(self):
        model = self.model('receipt-during-ack')
        job = model.run()
        self.assertEqual(job['status'], 'Cancelled', model.events)
        self.assertTrue(model.released)
        self.assertEqual(model.signals, [])
        self.assertLess(model.events.index('receipt'), model.events.index('RELEASE'))
        self.assertEqual(job['attempt_id'], model.job['attempt_id'])
        self.assertFalse(model.alive & {model.anchor, model.runner, model.compute, model.writer})

    def test_unexpected_anchor_loss_retains_unknown_and_commitment(self):
        model = self.model('anchor-lost')
        job = model.run()
        self.assertEqual(job['status'], 'Unknown')
        self.assertTrue(job['cancel_requested'])
        self.assertEqual(job['resources'], model.job['resources'])
        self.assertIsNone(job['finished_at'])
        self.assertFalse(model.released)
        self.assertEqual(model.signals, [])
        with model.store.transaction() as con:
            self.assertFalse(model.store.control(con)['dispatch'])

    def test_replaced_worker_authority_cannot_continue_signalling(self):
        model = self.model('worker-replaced')
        job = model.run()
        self.assertEqual(job['status'], 'Unknown')
        self.assertEqual(model.signals, [])
        self.assertFalse(model.released)
        self.assertIsNone(job['finished_at'])

    def test_escalation_cannot_claim_cancelled_without_genuine_payload_receipt(self):
        model = self.model('no-receipt-escalation')
        job = model.run()
        self.assertEqual(job['status'], 'Unknown', model.events)
        self.assertFalse(model.receipt.exists())
        self.assertIsNone(job['finished_at'])
        self.assertFalse(model.released)
        self.assertTrue(job['cancel_requested'])
        self.assertEqual(job['resources'], model.job['resources'])


class PayloadReceiptTopologyTests(unittest.TestCase):
    def test_anchor_group_term_can_kill_receipt_writer_before_release(self):
        """Controlled demonstration of the shell hazard, not historical attribution."""
        with tempfile.TemporaryDirectory(prefix='cmw-receipt-topology-') as temporary:
            root = Path(temporary)
            endpoint = root/'control.sock'
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(endpoint))
            listener.listen(1)
            listener.settimeout(5)
            executable = root/'receipt-writer'
            executable.write_text('#!'+sys.executable+'\n'+'''import json,os,socket,sys
import psutil
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.connect(os.environ['FIXTURE_RECEIPT_SOCKET'])
s.sendall(json.dumps({'pid':os.getpid(),'birth':psutil.Process().create_time(),'pgid':os.getpgrp(),'sid':os.getsid(0)}).encode()+b'\\n')
s.recv(1)
raise SystemExit(0)
''')
            executable.chmod(0o700)
            receipt = root/'receipt.json'
            helper = Path(runtime.__file__).with_name('payload.sh')
            process = subprocess.Popen(['/bin/bash', str(helper), str(executable), '', str(receipt),
                                        str(root/'stdout'), str(root/'stderr'), '/bin/true'],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True,
                                       env={**os.environ, 'FIXTURE_RECEIPT_SOCKET': str(endpoint)})
            anchor = ownership.identity(process.pid)
            connection = None
            child = None
            try:
                process.stdin.write(b'GO\n'); process.stdin.flush()
                connection, _ = listener.accept(); connection.settimeout(5)
                child = json.loads(connection.recv(4096))
                self.assertEqual(child['pgid'], process.pid)
                self.assertEqual(child['sid'], process.pid)
                self.assertEqual(ownership.identity(process.pid), anchor)
                self.assertEqual(psutil.Process(child['pid']).create_time(), child['birth'])
                os.killpg(process.pid, signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 125)
                self.assertFalse(receipt.exists())
                self.assertFalse(ownership.group_exists(process.pid))
            finally:
                if connection is not None:
                    connection.close()
                if process.poll() is None:
                    # An assertion failure releases only this recorded fixture.
                    process.stdin.write(b'RELEASE\n'); process.stdin.flush()
                    process.wait(timeout=5)
                process.stdin.close()
                if child is not None:
                    with self.assertRaises(psutil.NoSuchProcess):
                        psutil.Process(child['pid']).create_time()
                    print('Fixture census: '+json.dumps({'anchor': anchor, 'receipt_child': child,
                        'anchor_exit_code': process.returncode, 'receipt_child_absent': True,
                        'group_absent': not ownership.group_exists(process.pid)}), flush=True)


if __name__ == '__main__':
    unittest.main()
