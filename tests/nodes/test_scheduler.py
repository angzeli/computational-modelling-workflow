"""Read-only PBS-backed nodes (#81 Phase 1): enrollment, readiness and status.

PBS output here is synthetic. Its shape follows the probe summarized in
docs/architecture/scheduler-nodes.md section 6; every name is invented.
"""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.nodes.endpoint import default_backend, serve
from cmw.nodes.identity import check_node, enroll, load_node
from cmw.nodes import scheduler
from cmw.nodes.protocol import MAX_FRAME, NodeError, encode, validate_envelope
from cmw.nodes.scheduler import HELPER, MAX_JOBS, MAX_OUTPUT, QUERY_TIMEOUT, SchedulerBackend, pbs_status
from cmw.nodes.transport_events import compatible_capabilities
from tests.nodes.test_identity import RUNTIME

HOST = {'platform': 'linux', 'hardware_id': 'ab' * 16}
USER = 'tester'
JOBS = {
    '97.server1': {'Job_Name': 'other-user', 'Job_Owner': 'someone@login1', 'job_state': 'R'},
    '98.server1': {'Job_Name': 'never-ran', 'Job_Owner': 'tester@login1', 'job_state': 'F', 'substate': 91,
                 'queue': 'large', 'server': 'server1',
                 'Resource_List': {'ncpus': 96, 'mem': '256gb', 'walltime': '72:00:00'},
                 'comment': 'Not Running: Placement set is too small and terminated', 'run_count': 0},
    '99.server1': {'Job_Name': 'relax-b', 'Job_Owner': 'tester@login1', 'job_state': 'F', 'substate': 92,
                 'queue': 'workq', 'server': 'server1', 'Exit_status': 0,
                 'Resource_List': {'ncpus': 64, 'mem': '128gb', 'mpiprocs': 64, 'walltime': '48:00:00',
                                   'select': '2:ncpus=32:mpiprocs=32:mem=64gb', 'nodect': 2, 'place': 'free'},
                 'resources_used': {'walltime': '37:26:00', 'cput': '2396:00:00', 'mem': '61gb',
                                    'ncpus': 64, 'cpupercent': 6390, 'vmem': '70gb'},
                 'comment': 'Job run at Tue Nov 17 at 16:34 and finished', 'stime': 'Tue Nov 17 16:34:25 2026',
                 'obittime': 'Thu Nov 19 06:01:27 2026', 'qtime': 'Tue Nov 17 16:30:01 2026', 'run_count': 1},
    '101.server1': {'Job_Name': 'relax-c\x1b[31m', 'Job_Owner': 'tester@login1', 'job_state': 'R', 'substate': 42,
                  'queue': 'workq', 'server': 'server1',
                  'Resource_List': {'ncpus': 64, 'mem': '128gb', 'walltime': '48:00:00'},
                  'resources_used': {'walltime': '07:02:00', 'ncpus': 64},
                  'stime': 'Fri Nov 20 10:09:28 2026', 'run_count': 1},
}
QSELECT = '97.server1\n98.server1\n99.server1\n101.server1\n100.server1\n'


def document(ids):
    return {'timestamp': 1795194770, 'pbs_version': '2026.1.0', 'pbs_server': 'server1,server2',
            'Jobs': {job_id: JOBS[job_id] for job_id in ids if job_id in JOBS}}


class FakePBS:
    def __init__(self, qselect=QSELECT, qstat=None, qselect_code=0, qstat_code=0, timeout=False):
        self.qselect, self.qstat, self.calls = qselect, qstat, []
        self.qselect_code, self.qstat_code, self.timeout = qselect_code, qstat_code, timeout

    def __call__(self, argv, timeout):
        # argv is the helper's: /bin/bash HELPER LIMIT CLIENT ARG...
        self.calls.append((list(argv), timeout))
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        if os.path.basename(argv[3]) == 'qselect':
            return subprocess.CompletedProcess(argv, self.qselect_code, self.qselect.encode(), b'qselect: refused')
        output = (json.dumps(document(argv[8:])).encode() if self.qstat is None else self.qstat)
        return subprocess.CompletedProcess(argv, self.qstat_code, output, b'qstat: Unknown Job Id 100.server1')


class PBSNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        tools = self.root / 'opt-pbs' / 'bin_enabled'
        tools.mkdir(parents=True)
        (self.root / 'opt-pbs' / 'bin').symlink_to(tools)
        for name in ('qselect', 'qstat'):
            path = tools / name
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        self.bin = self.root / 'opt-pbs' / 'bin'
        self.config_root = self.root / 'node'
        self.spec = {'kind': 'pbs', 'queue': 'main',
                     'scheduler': {'qselect': str(self.bin / 'qselect'), 'qstat': str(self.bin / 'qstat')}}
        self.providers = {'host_provider': lambda: dict(HOST), 'runtime_provider': lambda: dict(RUNTIME)}

    def enrolled(self):
        return enroll(self.config_root, self.spec, **self.providers)

    def test_enrollment_records_identities_and_pinned_clients_without_a_store(self):
        config = self.enrolled()
        self.assertEqual((config['schema'], config['kind']), (3, 'pbs'))
        self.assertEqual(load_node(self.config_root), config)
        self.assertEqual(stat.S_IMODE((self.config_root / 'node.json').stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.config_root.iterdir()), ['node.json'])
        queue = config['queues']['main']
        self.assertEqual((queue['node_id'], queue['host_id'], queue['kind']), (config['node_id'], 'linux:' + 'ab' * 16, 'pbs'))
        self.assertEqual(config['scheduler']['qstat'], {
            'path': str(self.bin / 'qstat'), 'resolved_path': str(self.root / 'opt-pbs' / 'bin_enabled' / 'qstat')})
        self.assertNotIn('storages', config)
        with self.assertRaisesRegex(ValueError, 'never overwritten'):
            self.enrolled()

    def test_enrollment_and_validation_refuse_extra_authority(self):
        with self.assertRaises(ValueError):
            enroll(self.root / 'darwin', self.spec, host_provider=lambda: {
                'platform': 'darwin', 'hardware_id': '12345678-1234-1234-1234-123456789abc'},
                runtime_provider=lambda: dict(RUNTIME))
        for change in ({'storages': {}}, {'queue': 'bad alias'}, {'scheduler': {'qstat': 'qstat'}},
                       {'scheduler': {'qselect': str(self.bin / 'qselect'), 'qstat': 'qstat'}},
                       {'scheduler': {'qselect': str(self.bin / 'qselect'), 'qstat': str(self.root / 'missing')}}):
            with self.subTest(change=change), self.assertRaises((ValueError, OSError)):
                enroll(self.root / 'refused', {**self.spec, **change}, **self.providers)
        config = self.enrolled()
        for mutate in (lambda value: value.update(storages={}),
                       lambda value: value['queues'].update(extra=deepcopy(value['queues']['main'])),
                       lambda value: value['queues']['main'].update(schema=True),
                       lambda value: value['queues']['main'].update(node_id='cd' * 16),
                       lambda value: value['scheduler']['qstat'].update(path='qstat'),
                       lambda value: value.update(kind='slurm')):
            changed = deepcopy(config)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                check_node(changed, **self.providers)

    def test_readiness_pins_clients_host_and_refuses_execution(self):
        config = self.enrolled()
        ready = check_node(config, **self.providers)
        self.assertTrue(ready['ready'], ready)
        self.assertEqual(ready['queues'], config['queues'])
        self.assertFalse(check_node(config, operation='execution', queue_alias='main', **self.providers)['ready'])
        moved = check_node(config, **{**self.providers, 'host_provider': lambda: {**HOST, 'hardware_id': 'cd' * 16}})
        self.assertFalse(moved['identity_ready'])
        replacement = self.root / 'opt-pbs' / 'bin_disabled'
        replacement.mkdir()
        for name in ('qselect', 'qstat'):
            (replacement / name).write_text('#!/bin/sh\nexit 0\n')
            (replacement / name).chmod(0o755)
        (self.root / 'opt-pbs' / 'bin').unlink()
        (self.root / 'opt-pbs' / 'bin').symlink_to(replacement)
        retargeted = check_node(config, **self.providers)
        self.assertEqual((retargeted['identity_ready'], retargeted['scheduler_ready'], retargeted['ready']), (True, False, False))

    def test_status_lists_own_jobs_verbatim(self):
        fake = FakePBS()
        pbs = pbs_status('/pbs/qselect', '/pbs/qstat', USER, run=fake)
        helper = ['/bin/bash', str(HELPER), str(MAX_OUTPUT)]
        self.assertEqual(fake.calls[0], (helper + ['/pbs/qselect', '-x', '-u', USER], QUERY_TIMEOUT))
        self.assertEqual(fake.calls[1][0], helper + ['/pbs/qstat', '-x', '-f', '-F', 'json',
                                            '97.server1', '98.server1', '99.server1', '100.server1', '101.server1'])
        self.assertEqual([row['pbs_id'] for row in pbs['jobs']], ['101.server1', '99.server1', '98.server1'])
        self.assertEqual((pbs['foreign_skipped'], pbs['missing'], pbs['truncated']), (1, 1, False))
        self.assertEqual(pbs['counts'], {'queued': 0, 'running': 1, 'finished': 2, 'other': 0})
        running, finished, never = pbs['jobs']
        self.assertIsNone(never['exit_status'])
        self.assertEqual((never['substate'], never['phase']), (91, 'finished'))
        self.assertEqual(finished['exit_status'], 0)
        self.assertEqual(finished['requested']['select'], '2:ncpus=32:mpiprocs=32:mem=64gb')
        self.assertNotIn('vmem', finished['used'])
        self.assertEqual(running['name'], 'relax-c')
        self.assertEqual((pbs['reported_at'], pbs['servers']), (1795194770, 'server1,server2'))

    def test_status_fails_closed_on_scheduler_evidence(self):
        cases = {'malformed id': FakePBS(qselect='98.server1\n; rm -rf /\n'),
                 'qselect refused': FakePBS(qselect_code=1),
                 'qstat not json': FakePBS(qstat=b'Job Id: 98.server1\n'),
                 'qstat refused': FakePBS(qstat=b'', qstat_code=153),
                 'qstat jobs not a mapping': FakePBS(qstat=b'{"Jobs": []}'),
                 'helper refused': FakePBS(qselect_code=125),
                 'timeout': FakePBS(timeout=True)}
        for name, fake in cases.items():
            with self.subTest(name), self.assertRaises(NodeError) as caught:
                pbs_status('/pbs/qselect', '/pbs/qstat', USER, run=fake)
            self.assertEqual(caught.exception.code, 'SCHEDULER_UNAVAILABLE')
        partial = FakePBS(qstat_code=153)
        self.assertEqual(len(pbs_status('/pbs/qselect', '/pbs/qstat', USER, run=partial)['jobs']), 3)
        empty = FakePBS(qselect='')
        self.assertEqual(pbs_status('/pbs/qselect', '/pbs/qstat', USER, run=empty)['jobs'], [])
        self.assertEqual(len(empty.calls), 1)
        many = FakePBS(qselect=''.join(f'{number}.server1\n' for number in range(1, MAX_JOBS + 6)))
        result = pbs_status('/pbs/qselect', '/pbs/qstat', USER, run=many)
        self.assertTrue(result['truncated'])
        self.assertEqual(many.calls[1][0][8], '6.server1')
        self.assertEqual(len(many.calls[1][0]) - 8, MAX_JOBS)

    def backend(self, config, readiness=None, **options):
        readiness = readiness or check_node(config, **self.providers)
        return SchedulerBackend(self.config_root, 'main', clock=lambda: 100.,
            identity_loader=lambda _: deepcopy(config), identity_checker=lambda _: deepcopy(readiness),
            boot_provider=lambda: 'boot-1', run=options.pop('run', FakePBS()), user=USER, **options)

    def test_backend_envelopes_are_compatible_and_read_only(self):
        config = self.enrolled()
        backend = self.backend(config)
        capabilities = backend.call('capabilities')
        validate_envelope(capabilities, 'capabilities')
        compatible_capabilities(capabilities)
        self.assertEqual(capabilities['result']['operations'], ['capabilities', 'status'])
        self.assertFalse(capabilities['result']['mutations_supported'])
        self.assertEqual(capabilities['result']['scheduler']['poll_interval_seconds'], 30)
        status = backend.call('status')
        validate_envelope(status, 'status')
        self.assertEqual((status['node_id'], status['queue_id'], status['sequence']),
                         (config['node_id'], config['queues']['main']['queue_id'], 2))
        self.assertEqual(status['result']['state']['jobs'], [])
        pbs = status['result']['state']['pbs']
        self.assertEqual((len(pbs['jobs']), pbs['total'], pbs['next_offset'], pbs['size_limited']), (3, 3, None, False))
        second = backend.call('status', {'offset': 1, 'limit': 1})['result']['state']['pbs']
        self.assertEqual(([row['pbs_id'] for row in second['jobs']], second['next_offset']), (['99.server1'], 2))
        for bad in ({'offset': -1}, {'offset': MAX_JOBS + 1}, {'limit': 0}, {'limit': True}, {'offset': '1'}):
            with self.subTest(params=bad), self.assertRaises(NodeError) as caught:
                backend.call('status', bad)
            self.assertEqual(caught.exception.code, 'INVALID_PARAMS')
        for operation, options, code in (('submit', {}, 'UNSUPPORTED_OPERATION'),
                                         ('lookup', {'params': {'id': 1}}, 'UNSUPPORTED_OPERATION'),
                                         ('control', {}, 'UNSUPPORTED_OPERATION'),
                                         ('not-an-operation', {}, 'UNSUPPORTED_OPERATION'),
                                         ('status', {'target': {'node_id': 'a' * 32}}, 'INVALID_TARGET'),
                                         ('status', {'params': {'unexpected': 1}}, 'INVALID_PARAMS')):
            with self.subTest(operation=operation, options=options), self.assertRaises(NodeError) as caught:
                backend.call(operation, **options)
            self.assertEqual(caught.exception.code, code)

    def test_full_status_page_fits_one_protocol_frame(self):
        config = self.enrolled()
        records = {f'{number}.server1': {'Job_Name': 'n' * 256, 'Job_Owner': 'tester@login1', 'job_state': 'F',
                                          'comment': 'c' * 512, 'queue': 'q' * 64, 'server': 's' * 64,
                                          'Resource_List': {key: 'r' * 64 for key in scheduler.REQUESTED},
                                          'resources_used': {key: 'u' * 64 for key in scheduler.USED},
                                          'qtime': 't' * 64, 'stime': 't' * 64, 'obittime': 't' * 64}
                   for number in range(1, MAX_JOBS + 1)}
        ids = ''.join(f'{job_id}\n' for job_id in records)
        fake = FakePBS(qselect=ids, qstat=json.dumps({'timestamp': 1, 'Jobs': records}).encode())
        backend = self.backend(config, run=fake)
        first = backend.call('status')
        self.assertLessEqual(len(encode(first)), MAX_FRAME)
        pbs = first['result']['state']['pbs']
        self.assertTrue(pbs['size_limited'])
        self.assertEqual((pbs['total'], pbs['next_offset']), (MAX_JOBS, len(pbs['jobs'])))
        self.assertEqual(pbs['jobs'][0]['pbs_id'], f'{MAX_JOBS}.server1')
        rest = backend.call('status', {'offset': pbs['next_offset']})['result']['state']['pbs']
        self.assertEqual(rest['jobs'][0]['pbs_id'], f'{MAX_JOBS - len(pbs["jobs"])}.server1')

    def test_backend_refuses_unready_or_changed_identity(self):
        config = self.enrolled()
        ready = check_node(config, **self.providers)
        unready = {**ready, 'scheduler_ready': False, 'ready': False, 'errors': ['qstat: missing']}
        backend = self.backend(config, unready)
        self.assertFalse(backend.call('capabilities')['result']['readiness']['ready'])
        with self.assertRaises(NodeError) as caught:
            backend.call('status')
        self.assertEqual(caught.exception.code, 'NODE_NOT_READY')
        for readiness, code in (({**ready, 'identity_ready': False, 'identity_errors': ['moved']}, 'IDENTITY_CHANGED'),
                                ({**ready, 'runtime_ready': False, 'runtime_errors': ['differs']}, 'RUNTIME_MISMATCH')):
            with self.subTest(code=code), self.assertRaises(NodeError) as caught:
                self.backend(config, readiness).call('capabilities')
            self.assertEqual(caught.exception.code, code)
        backend = self.backend(config)
        backend.call('capabilities')
        backend._boot_provider = lambda: 'boot-2'
        with self.assertRaises(NodeError) as caught:
            backend.call('status')
        self.assertEqual(caught.exception.code, 'IDENTITY_CHANGED')
        local = {key: value for key, value in config.items() if key != 'kind'}
        with self.assertRaises(NodeError) as caught:
            self.backend(local, ready).call('capabilities')
        self.assertEqual(caught.exception.code, 'INVALID_QUEUE')

    def test_endpoint_serves_the_enrolled_pbs_kind(self):
        config = self.enrolled()
        self.assertIsInstance(default_backend(self.config_root, 'main'), SchedulerBackend)
        with self.assertRaises((ValueError, OSError)):
            default_backend(self.root / 'missing', 'main')
        request = {'protocol': 1, 'request_id': 'f' * 32, 'operation': 'status', 'target': None, 'params': {}}
        outgoing = io.BytesIO()
        serve(self.config_root, 'main', input_stream=io.BytesIO(json.dumps(request).encode() + b'\n'),
              output_stream=outgoing, backend_factory=lambda root, alias: self.backend(config))
        reply = json.loads(outgoing.getvalue())
        self.assertTrue(reply['ok'], reply)
        validate_envelope(reply['result'], 'status')

    def test_pinned_fake_clients_run_through_subprocess(self):
        fixture = self.root / 'qstat.json'
        fixture.write_text(json.dumps(document(JOBS)))
        clients = self.root / 'clients'
        clients.mkdir()
        ids = self.root / 'qselect.txt'
        ids.write_text(QSELECT)
        (clients / 'qselect').write_text(f'#!/bin/bash\n[[ "$*" == "-x -u {USER}" ]] || exit 2\n'
                                         f'cat {json.dumps(str(ids))}\n')
        (clients / 'qstat').write_text(f'#!/bin/bash\n[[ "$1 $2 $3 $4" == "-x -f -F json" ]] || exit 2\n'
                                       f'cat {json.dumps(str(fixture))}\n')
        for name in ('qselect', 'qstat'):
            (clients / name).chmod(0o755)
        pbs = pbs_status(str(clients / 'qselect'), str(clients / 'qstat'), USER)
        self.assertEqual([row['pbs_id'] for row in pbs['jobs']], ['101.server1', '99.server1', '98.server1'])

    def client(self, name, body):
        directory = self.root / 'helper-clients'
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)
        return str(path)

    def test_helper_bounds_output_and_stderr_and_refuses_other_clients(self):
        flood = self.client('qstat', 'exec yes')
        started = time.monotonic()
        with patch.object(scheduler, 'MAX_OUTPUT', 4096), self.assertRaises(NodeError) as caught:
            scheduler._query(flood, ['-x'], scheduler.run_helper)
        self.assertIn('exceeds its bound', str(caught.exception))
        self.assertLess(time.monotonic() - started, 10)
        noisy = self.client('qselect', 'head -c 20000 /dev/zero | tr "\\0" e >&2; exit 3')
        completed = scheduler._query(noisy, [], scheduler.run_helper)
        self.assertEqual((completed.returncode, len(completed.stderr)), (3, 4096))
        with self.assertRaises(NodeError) as caught:
            own = self.client('qselect', 'exit 3')
            scheduler.own_job_ids(own, USER)
        self.assertIn('qselect exited 3', str(caught.exception))
        for client in (self.client('echo', 'echo 1.server1'), 'qselect', str(self.root / 'helper-clients' / 'absent')):
            with self.subTest(client=client), self.assertRaises(NodeError) as caught:
                scheduler._query(client, [], scheduler.run_helper)
            self.assertIn('refused', str(caught.exception))

    def test_helper_timeout_stops_the_client_process_group(self):
        marker = self.root / 'client.pid'
        slow = self.client('qselect', f'echo $$ > {json.dumps(str(marker))}; exec sleep 30')
        with patch.object(scheduler, 'QUERY_TIMEOUT', 1), self.assertRaises(NodeError) as caught:
            scheduler.own_job_ids(slow, USER)
        self.assertIn('timed out', str(caught.exception))
        pid = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            self.fail('PBS client survived the helper timeout')


if __name__ == '__main__':
    unittest.main()
