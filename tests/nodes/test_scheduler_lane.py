"""Scheduler-backed node lanes (#81 Phase 1b): session transport, endpoint lifetime and the portal lane.

Everything is synthetic. `ssh`, `qselect` and `qstat` are fake executables or
in-process fakes, and every host, server, queue, user and path is invented.
Two site shapes show the code is not tied to one cluster: one server with
fully populated records, and two servers whose suffixed job IDs share sequence
numbers, with sparse records and integers printed as strings.
"""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from cmw.cli import main
from cmw.nodes import scheduler
from cmw.nodes.connections import ConnectedBackend, pinned_cadence, validate_connection
from cmw.nodes.contracts import (NodeEvent, NodeState, Reachability, Staleness, classify_staleness,
                                 scheduler_poll_interval, transition)
from cmw.nodes.endpoint import idle_exit_seconds, serve
from cmw.nodes.identity import check_node, enroll
from cmw.nodes.protocol import NodeError, validate_envelope
from cmw.nodes.scheduler import SchedulerBackend, page, pbs_status
from cmw.nodes.scheduler_report import report_text, validate_report
from cmw.nodes.transport import (SESSION_CHECK, ProcessTransport, SessionRequired, SessionTransport,
                                 control_socket, open_session_command, session_command)
from cmw.nodes.transport_events import FailureKind, classify_failure
from tests.nodes.test_identity import RUNTIME

try:
    from cmw.nodes.portal import NodesApp, command_lines, scheduler_key
    from textual.widgets import Static
    from tests.nodes.test_portal import FakeBackend
except ImportError:
    NodesApp = None

HOST = {'platform': 'linux', 'hardware_id': 'cd' * 16}
OPEN = 'ssh -M -S /tmp/cmw-session/m.sock -fN login1.cluster.example'


class Site:
    """In-process PBS clients behind the shell helper's argv for one invented site."""

    def __init__(self, user, listing, records, meta):
        self.user, self.listing, self.records, self.meta, self.calls = user, listing, records, meta, []

    def single_call(self):
        """What `qstat -f -F json -u USER` would return on a server that honours it."""
        return {key: value for key, value in self.records.items()
                if value.get('Job_Owner', '').split('@')[0] == self.user}

    def __call__(self, argv, timeout):
        client, arguments = os.path.basename(argv[3]), argv[4:]
        self.calls.append((client, arguments))
        if client == 'qselect':
            assert arguments == ['-x', '-u', self.user], arguments
            return subprocess.CompletedProcess(argv, 0, self.listing.encode(), b'')
        assert arguments[:4] == ['-x', '-f', '-F', 'json'], arguments
        found = {job: self.records[job] for job in arguments[4:] if job in self.records}
        status = 0 if len(found) == len(arguments) - 4 else 35
        body = {**self.meta, **({'Jobs': found} if found else {})}
        return subprocess.CompletedProcess(argv, status, json.dumps(body).encode(),
                                           b'qstat: Unknown Job Id' if status else b'')


def one_server_site():
    """One server; every optional field present."""
    full = {'Job_Owner': 'tester@login1.cluster.example', 'server': 'server1', 'queue': 'workq',
            'Resource_List': {'ncpus': 8, 'mem': '16gb', 'walltime': '02:00:00', 'select': '1:ncpus=8'},
            'qtime': 'Mon Nov 23 09:00:00 2026', 'run_count': 1}
    records = {
        '6.server1': {**full, 'Job_Name': 'done', 'job_state': 'F', 'substate': 92, 'Exit_status': 0,
                      'resources_used': {'walltime': '01:10:00', 'ncpus': 8, 'mem': '9gb', 'cput': '09:20:00',
                                         'cpupercent': 790},
                      'stime': 'Mon Nov 23 09:01:00 2026', 'obittime': 'Mon Nov 23 10:11:00 2026',
                      'comment': 'Job run at Mon Nov 23 at 09:01 and finished'},
        '7.server1': {**full, 'Job_Name': 'running', 'job_state': 'R', 'substate': 42,
                      'resources_used': {'walltime': '00:20:00', 'ncpus': 8}, 'stime': 'Mon Nov 23 10:20:00 2026'},
        '8.server1': {**full, 'Job_Name': 'waiting', 'job_state': 'Q', 'substate': 10, 'run_count': 0,
                      'comment': 'Not Running: Insufficient amount of resource: ncpus'},
    }
    return Site('tester', '6.server1\n7.server1\n8.server1\n', records,
                {'timestamp': 1795400000, 'pbs_version': '2099.0.0', 'pbs_server': 'server1'})


def two_server_site():
    """Two servers; suffixed IDs share sequence numbers; sparse records; integers as strings."""
    records = {
        '41.server1': {'Job_Owner': 'u0001@login2.cluster.example', 'job_state': 'Q'},
        '41.server2': {'Job_Owner': 'u0001@login1.cluster.example', 'job_state': 'F', 'Exit_status': '271',
                       'run_count': '1', 'Job_Name': 'walltime-ended',
                       'comment': 'Job run at Tue Nov 24 at 08:00 and\nterminated'},
        '12[].server2': {'Job_Owner': 'u0001@login1.cluster.example', 'job_state': 'B', 'Job_Name': 'array'},
        '40.server2': {'Job_Owner': 'u0002@login1.cluster.example', 'job_state': 'R'},
    }
    return Site('u0001', '41.server1\n41.server2\n12[].server2\n42.server2\n40.server2\n', records,
                {'timestamp': '1795400000'})


def scheduler_report(site=None):
    site = site or one_server_site()
    report = pbs_status('/opt/sched/bin/qselect', '/opt/sched/bin/qstat', site.user, run=site)
    report.update(page(report.pop('jobs'), 0, scheduler.MAX_JOBS))
    return report


class SiteShapeTests(unittest.TestCase):
    def test_one_server_site_with_every_optional_field(self):
        site = one_server_site()
        report = validate_report(scheduler_report(site))
        self.assertEqual([row['id'] for row in report['jobs']], ['8.server1', '7.server1', '6.server1'])
        self.assertEqual(report['counts'], {'queued': 1, 'running': 1, 'finished': 1, 'other': 0})
        done = report['jobs'][2]
        self.assertEqual((done['exit_status'], done['elapsed'], done['used']['cpupercent']), (0, '01:10:00', 790))
        self.assertEqual((report['version'], report['servers'], report['reported_at']),
                         ('2099.0.0', 'server1', 1795400000))
        self.assertEqual([call[0] for call in site.calls], ['qselect', 'qstat'])

    def test_two_server_site_with_suffixed_ids_and_sparse_records(self):
        site = two_server_site()
        report = validate_report(scheduler_report(site))
        # Same sequence number on two servers: distinct opaque IDs, scheduler order kept.
        self.assertEqual([row['id'] for row in report['jobs']], ['12[].server2', '41.server2', '41.server1'])
        self.assertEqual((report['missing'], report['foreign_skipped']), (1, 1))
        queued = report['jobs'][2]
        self.assertEqual((queued['name'], queued['queue'], queued['server'], queued['requested'], queued['used'],
                          queued['elapsed'], queued['exit_status'], queued['run_count']),
                         (None, None, None, {}, {}, None, None, None))
        ended = report['jobs'][1]
        self.assertEqual((ended['exit_status'], ended['run_count']), (271, 1))
        self.assertEqual(ended['comment'], 'Job run at Tue Nov 24 at 08:00 and terminated')
        self.assertEqual(report['jobs'][0]['phase'], 'running')
        self.assertEqual((report['version'], report['servers'], report['reported_at']), (None, None, 1795400000))
        self.assertEqual(report['counts'], {'queued': 1, 'running': 1, 'finished': 1, 'other': 0})

    def test_two_call_path_matches_a_server_that_answers_one_call(self):
        site = two_server_site()
        report = scheduler_report(site)
        self.assertEqual([call[0] for call in site.calls], ['qselect', 'qstat'])
        self.assertEqual({row['id'] for row in report['jobs']}, set(site.single_call()))

    def test_job_ids_are_opaque_tokens(self):
        bare = Site('tester', '5012\n5013\n', {
            '5012': {'Job_Owner': 'tester@host', 'job_state': 'R'},
            '5013': {'Job_Owner': 'tester@host', 'job_state': 'X'}}, {})
        self.assertEqual([row['id'] for row in scheduler_report(bare)['jobs']], ['5013', '5012'])
        for listing in ('-x\n', '1 2\n', '1;id\n', '7.server1\x1b[31m\n', '.server1\n'):
            with self.subTest(listing=listing), self.assertRaises(NodeError) as caught:
                pbs_status('/q/qselect', '/q/qstat', 'tester', run=Site('tester', listing, {}, {}))
            self.assertEqual(caught.exception.code, 'SCHEDULER_UNAVAILABLE')

    def test_report_text_and_validation_are_scheduler_neutral(self):
        text = report_text(scheduler_report(two_server_site()))
        self.assertIn('SCHEDULER JOBS — PBS', text)
        self.assertIn('41.server2', text)
        self.assertIn('3 of 3 shown', text)
        self.assertIn("1 left the scheduler's history between calls", text)
        self.assertIn('No scheduler jobs reported', report_text(scheduler.report('other', [])))
        good = scheduler_report()
        for mutate in (lambda value: value.update(schema=2), lambda value: value['counts'].pop('other'),
                       lambda value: value['jobs'][0].update(id='1 2'),
                       lambda value: value['jobs'][0].update(phase='done'),
                       lambda value: value['jobs'][0].update(name='a\nb'),
                       lambda value: value['jobs'][0].update(used=[])):
            changed = deepcopy(good)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(NodeError):
                validate_report(changed)


class CadenceAndStateTests(unittest.TestCase):
    def test_scheduler_interval_bounds_and_staleness_ratio(self):
        for value in (30, 600):
            self.assertEqual(scheduler_poll_interval(value), value)
        for value in (29, 601, 30.0, True, '30', None):
            with self.subTest(value=value), self.assertRaises(NodeError):
                scheduler_poll_interval(value)
        self.assertEqual(classify_staleness(0., 90., poll_interval=30), Staleness.FRESH)
        self.assertEqual(classify_staleness(0., 90.001, poll_interval=30), Staleness.STALE)
        self.assertEqual(classify_staleness(0., 15.001), Staleness.STALE)

    def test_auth_required_is_immediate_and_never_counts_toward_d10(self):
        for state in (None, *NodeState):
            with self.subTest(state=state):
                required = transition(Reachability(state, 2, 5.), NodeEvent.SESSION_REQUIRED)
                self.assertEqual((required.state, required.consecutive_failures, required.received_at),
                                 (NodeState.AUTH_REQUIRED, 0, 5.))
                self.assertTrue(required.requires_handshake)
        current = Reachability()
        for _ in range(5):
            current = transition(current, NodeEvent.SESSION_REQUIRED)
        self.assertEqual((current.state, current.consecutive_failures), (NodeState.AUTH_REQUIRED, 0))
        self.assertEqual(transition(current, NodeEvent.HANDSHAKE_SUCCESS, received_at=9.).state, NodeState.REACHABLE)
        failure = classify_failure(SessionRequired(OPEN), operation='capabilities')
        self.assertEqual((failure.kind, failure.event, failure.controls_refused, failure.outcome_unknown),
                         (FailureKind.SESSION, NodeEvent.SESSION_REQUIRED, True, False))


class SessionConnectionTests(unittest.TestCase):
    def entry(self, **changes):
        value = {'transport': 'ssh', 'auth': 'session', 'ssh_alias': 'login1.cluster.example',
                 'control_path': '/tmp/cmw-session/m.sock', 'executable': '/scratch/u0001/cmw/bin/cmw-endpoint',
                 'config_root': '/scratch/u0001/cmw/node', 'node_id': 'a' * 32, 'host_id': 'linux:' + 'cd' * 16,
                 'runtime_id': 'b' * 64, 'queues': {'main': 'c' * 32}, 'default_queue': 'main',
                 'read_only': True, 'poll_interval_seconds': 30}
        value.update(changes)
        return {key: value for key, value in value.items() if value is not None}

    def test_session_argv_attaches_to_the_users_master_only(self):
        argv = session_command('login1.cluster.example', '/tmp/cmw-session/m.sock', '/x/cmw', '/x/node', 'main')
        options = [argv[index + 1] for index, value in enumerate(argv) if value == '-o']
        for option in ('BatchMode=yes', 'ControlMaster=no', 'ControlPath=/tmp/cmw-session/m.sock',
                       'ProxyCommand=/usr/bin/false', 'ServerAliveInterval=5', 'ForwardAgent=no'):
            self.assertIn(option, options)
        self.assertNotIn('-M', argv)
        self.assertNotIn('ControlPath=none', options)
        self.assertEqual(argv[-3:-1], ['--', 'login1.cluster.example'])
        channel = SessionTransport({**self.entry(), 'queue': 'main'})
        self.assertIsNone(channel.host_key)
        self.assertIsNone(channel.process)
        self.assertEqual(channel.open_command, 'ssh -M -S /tmp/cmw-session/m.sock -o ServerAliveInterval=30 '
                                               '-o ServerAliveCountMax=3 -fN login1.cluster.example')

    def test_connection_validation(self):
        validate_connection('cluster', self.entry())
        for changes in ({'ssh_host_key': 'SHA256:' + 'a' * 43}, {'control_path': 'relative.sock'},
                        {'control_path': '/tmp/with space/m.sock'}, {'control_path': '/tmp/%h.sock'},
                        {'control_path': '/' + 'a' * 100}, {'control_path': '/tmp/../m.sock'},
                        {'control_path': None}, {'auth': 'password'}, {'poll_interval_seconds': 5},
                        {'poll_interval_seconds': 601}, {'poll_interval_seconds': True}):
            with self.subTest(changes=changes), self.assertRaises(NodeError):
                validate_connection('cluster', self.entry(**changes))
        with self.assertRaises(NodeError):
            validate_connection('cluster', self.entry(auth=None, ssh_host_key='SHA256:' + 'a' * 43))
        with self.assertRaises(NodeError):
            validate_connection('cluster', self.entry(transport='local', ssh_alias=None, executable=None,
                                                      control_path=None))
        backend = ConnectedBackend('cluster', self.entry())
        self.assertIsInstance(backend.transport, SessionTransport)
        self.assertEqual(backend.poll_interval, 30)
        self.assertEqual(control_socket('/tmp/cmw-session/m.sock'), '/tmp/cmw-session/m.sock')

    def test_cadence_must_match_the_endpoint_enrollment(self):
        envelope = {'result': {'scheduler': {'poll_interval_seconds': 60}}}
        pinned_cadence({'poll_interval_seconds': 60}, envelope)
        pinned_cadence({}, {'result': {}})
        for entry, value in (({'poll_interval_seconds': 30}, envelope), ({}, envelope),
                             ({'poll_interval_seconds': 30}, {'result': {}})):
            with self.subTest(entry=entry, value=value), self.assertRaises(NodeError) as caught:
                pinned_cadence(entry, value)
            self.assertEqual(caught.exception.code, 'INCOMPATIBLE_CAPABILITIES')


class EnrolledNode:
    """A `pbs` enrollment with invented, executable fake clients."""

    def __init__(self, root, site, *, interval=None):
        self.root, self.site = root, site
        clients = root / 'opt-sched' / 'bin'
        clients.mkdir(parents=True)
        listing, document = root / 'qselect.txt', root / 'qstat.json'
        listing.write_text(site.listing)
        document.write_text(json.dumps({**site.meta, 'Jobs': site.records}))
        (clients / 'qselect').write_text(f'#!/bin/bash\n[[ "$*" == "-x -u {site.user}" ]] || exit 2\n'
                                         f'cat {json.dumps(str(listing))}\n')
        (clients / 'qstat').write_text('#!/bin/bash\n[[ "$1 $2 $3 $4" == "-x -f -F json" ]] || exit 2\n'
                                       f'cat {json.dumps(str(document))}\n')
        for name in ('qselect', 'qstat'):
            (clients / name).chmod(0o755)
        tools = {'qselect': str(clients / 'qselect'), 'qstat': str(clients / 'qstat')}
        if interval is not None:
            tools['poll_interval_seconds'] = interval
        self.config_root = root / 'node'
        providers = {'host_provider': lambda: dict(HOST), 'runtime_provider': lambda: dict(RUNTIME)}
        self.config = enroll(self.config_root, {'kind': 'pbs', 'queue': 'main', 'scheduler': tools}, **providers)
        self.readiness = check_node(self.config, **providers)

    def backend(self, **options):
        return SchedulerBackend(self.config_root, 'main', identity_loader=lambda _: deepcopy(self.config),
                                identity_checker=lambda _: deepcopy(self.readiness),
                                boot_provider=lambda: 'boot-1', user=self.site.user, defer_check=True, **options)


class EndpointLifetimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_enrolled_interval_sets_capability_and_idle_exit(self):
        default = EnrolledNode(self.root / 'default', one_server_site())
        self.assertNotIn('poll_interval_seconds', default.config['scheduler'])
        self.assertEqual(idle_exit_seconds(default.config_root), 120)
        slower = EnrolledNode(self.root / 'slower', two_server_site(), interval=60)
        self.assertEqual(slower.config['scheduler']['poll_interval_seconds'], 60)
        capability = slower.backend().call('capabilities')['result']['scheduler']
        self.assertEqual((capability['poll_interval_seconds'], capability['idle_exit_seconds']), (60, 240))
        self.assertEqual(idle_exit_seconds(slower.config_root), 240)
        self.assertEqual(idle_exit_seconds(self.root / 'missing'), scheduler.DEFAULT_IDLE_EXIT_SECONDS)
        with patch('cmw.nodes.identity.load_node', return_value={'schema': 2}):
            self.assertIsNone(idle_exit_seconds(self.root / 'mac'))
        for interval in (29, 601, True):
            with self.subTest(interval=interval), self.assertRaises((ValueError, NodeError)):
                EnrolledNode(self.root / f'refused-{interval}', one_server_site(), interval=interval)

    def serve_pipe(self, node, idle, frames=b''):
        read, write = os.pipe()
        if frames:
            os.write(write, frames)
        outgoing = io.BytesIO()
        started = time.monotonic()
        with os.fdopen(read, 'rb') as incoming:
            code = serve(node.config_root, 'main', input_stream=incoming, output_stream=outgoing,
                         backend_factory=lambda root, queue: node.backend(), idle_seconds=idle)
        elapsed = time.monotonic() - started
        os.close(write)  # The client never closed its end: only the idle bound ended the endpoint.
        return code, outgoing.getvalue(), elapsed

    def test_idle_endpoint_exits_while_its_client_holds_the_stream_open(self):
        node = EnrolledNode(self.root / 'node-root', one_server_site())
        code, output, elapsed = self.serve_pipe(node, .3)
        self.assertEqual((code, output), (0, b''))
        self.assertLess(elapsed, 5)
        request = {'protocol': 1, 'request_id': 'e' * 32, 'operation': 'status', 'target': None, 'params': {}}
        code, output, _ = self.serve_pipe(node, .3, json.dumps(request).encode() + b'\n{"partial')
        replies = output.splitlines()
        self.assertEqual((code, len(replies)), (0, 1))
        reply = json.loads(replies[0])
        validate_envelope(reply['result'], 'status')
        self.assertEqual(len(reply['result']['result']['state']['scheduler_report']['jobs']), 3)


class FakeSession:
    """A fake OpenSSH client, a fake endpoint executable and a real control socket."""

    def __init__(self, root, node, *, idle=30.):
        base = Path(tempfile.gettempdir())
        directory = Path(tempfile.mkdtemp(prefix='cmw', dir=base if len(str(base)) < 60 else '/tmp')).resolve()
        self.socket_dir = directory
        directory.chmod(0o700)
        self.control_path = str(directory / 'm.sock')
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.control_path)
        self.alive, self.drop, self.refuse = root / 'alive', root / 'drop', root / 'refuse'
        self.log = root / 'ssh.log'
        self.log.write_text('')
        config, readiness = root / 'config.json', root / 'readiness.json'
        config.write_text(json.dumps(node.config))
        readiness.write_text(json.dumps(node.readiness))
        script = root / 'endpoint.py'
        script.write_text(f'''import json, sys
from cmw.nodes.endpoint import serve
from cmw.nodes.scheduler import SchedulerBackend
config, readiness = (json.loads(open(path).read()) for path in sys.argv[1:3])
arguments = sys.argv[4:]
root, queue = arguments[arguments.index('--config') + 1], arguments[arguments.index('--queue') + 1]
def factory(root, queue):
    return SchedulerBackend(root, queue, identity_loader=lambda _: config, identity_checker=lambda _: readiness,
                            boot_provider=lambda: 'boot-1', user={node.site.user!r}, defer_check=True)
sys.exit(serve(root, queue, backend_factory=factory, idle_seconds=float(sys.argv[3])))
''')
        self.executable = root / 'cmw-endpoint'
        self.executable.write_text(f'#!/bin/bash\nexec {json.dumps(sys.executable)} -B {json.dumps(str(script))} '
                                   f'{json.dumps(str(config))} {json.dumps(str(readiness))} {idle} "$@"\n')
        self.ssh = root / 'ssh'
        self.ssh.write_text(f'''#!/bin/bash
printf '%s\\n' "$*" >> {json.dumps(str(self.log))}
if [[ " $* " == *" -O check "* ]]; then
  [[ -e {json.dumps(str(self.alive))} ]] && exit 0
  echo 'Control socket connect: No such file or directory' >&2; exit 255
fi
for option in BatchMode=yes ControlMaster=no ControlPath={self.control_path} ProxyCommand=/usr/bin/false; do
  [[ " $* " == *" -o $option "* ]] || exit 97
done
# A non-empty drop/refuse marker makes the failure wait for the request first.
if [[ -e {json.dumps(str(self.drop))} ]]; then
  [[ -s {json.dumps(str(self.drop))} ]] && read -r _
  rm -f {json.dumps(str(self.alive))}; echo 'mux_client_request_session: read from master failed' >&2; exit 255
fi
if [[ -e {json.dumps(str(self.refuse))} ]]; then
  [[ -s {json.dumps(str(self.refuse))} ]] && read -r _
  echo 'Session open refused by peer' >&2; exit 255
fi
[[ -e {json.dumps(str(self.alive))} ]] || {{ echo 'Connection closed by UNKNOWN port 65535' >&2; exit 255; }}
echo 'debug1: mux_client_request_session: master session id: 2' >&2
exec /bin/bash -c "${{@: -1}}"
''')
        for path in (self.executable, self.ssh):
            path.chmod(0o755)
        queue = node.config['queues']['main']
        self.entry = {'transport': 'ssh', 'auth': 'session', 'ssh_alias': 'login1.cluster.example',
                      'control_path': self.control_path, 'executable': str(self.executable),
                      'config_root': str(node.config_root), 'node_id': node.config['node_id'],
                      'host_id': node.config['host_id'], 'runtime_id': RUNTIME['fingerprint'],
                      'queues': {'main': queue['queue_id']}, 'default_queue': 'main', 'read_only': True,
                      'poll_interval_seconds': 30, 'timeout': 20}

    def close(self):
        self.listener.close()
        for path in self.socket_dir.iterdir():
            path.unlink()
        self.socket_dir.rmdir()

    def lines(self):
        return self.log.read_text().splitlines()

    def backend(self):
        return ConnectedBackend('cluster', self.entry,
                                transport=SessionTransport({**self.entry, 'queue': 'main'}, ssh=str(self.ssh)))


class SessionCheckHelperTests(unittest.TestCase):
    """The shell helper owns the `ssh -O check` launch; Python only reads its exit status."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.log = self.root / 'argv'
        self.ssh = self.root / 'ssh'
        self.ssh.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {json.dumps(str(self.log))}\n'
                            f'[[ -t 0 ]] && exit 98\nexit "${{CHECK_STATUS:-0}}"\n')
        self.ssh.chmod(0o755)

    def check(self, *arguments, status=0):
        return subprocess.run(['/bin/bash', str(SESSION_CHECK), *arguments], stdin=subprocess.DEVNULL,
                              capture_output=True, env={**os.environ, 'CHECK_STATUS': str(status)},
                              timeout=10).returncode

    def test_exit_status_contract(self):
        self.assertEqual(self.check(str(self.ssh), '/tmp/cmw-session/m.sock', 'login1.cluster.example'), 0)
        self.assertEqual(self.log.read_text().splitlines(),
                         ['-o', 'BatchMode=yes', '-o', 'ControlPath=/tmp/cmw-session/m.sock', '-O', 'check',
                          '--', 'login1.cluster.example'])
        self.assertEqual(self.check(str(self.ssh), '/tmp/cmw-session/m.sock', 'login1.cluster.example',
                                    status=255), 1)
        self.log.unlink()
        for arguments in (('ssh', '/tmp/m.sock', 'cluster'), (str(self.root), '/tmp/m.sock', 'cluster'),
                          (str(self.ssh), 'm.sock', 'cluster'), (str(self.ssh), '/tmp/m.sock', '-oProxyCommand=x'),
                          (str(self.ssh), '/tmp/m.sock'), (str(self.ssh), '/tmp/m.sock', 'cluster', 'extra')):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.check(*arguments), 125)
        self.assertFalse(self.log.exists())  # A refused argument never reaches ssh.


class SessionTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.node = EnrolledNode(self.root / 'site', one_server_site())
        self.session = FakeSession(self.root, self.node)
        self.addCleanup(self.session.close)

    def refused(self, backend, operation='capabilities'):
        try:
            with self.assertRaises(NodeError) as caught:
                backend.call(operation)
        finally:
            backend.close()
        return caught.exception

    def test_missing_or_dead_master_is_auth_required_without_any_attempt(self):
        refusal = self.refused(self.session.backend())
        self.assertIsInstance(refusal, SessionRequired)
        self.assertEqual(refusal.code, 'AUTH_REQUIRED')
        self.assertIn(f'-S {self.session.control_path}', refusal.command)
        self.assertTrue(all(' -O check ' in f' {line} ' for line in self.session.lines()), self.session.lines())
        self.session.socket_dir.chmod(0o755)
        before = len(self.session.lines())
        self.assertEqual(self.refused(self.session.backend()).code, 'AUTH_REQUIRED')
        self.assertEqual(len(self.session.lines()), before)  # Not even a check through a shared directory.

    def test_live_master_serves_capabilities_and_status_through_the_helper(self):
        self.session.alive.touch()
        backend = self.session.backend()
        try:
            capabilities = backend.call('capabilities')
            self.assertEqual(capabilities['result']['scheduler']['poll_interval_seconds'], 30)
            status = backend.call('status')
        finally:
            backend.close()
        report = status['result']['state']['scheduler_report']
        self.assertEqual([row['id'] for row in report['jobs']], ['8.server1', '7.server1', '6.server1'])
        sessions = [line for line in self.session.lines() if ' -O check ' not in f' {line} ']
        self.assertEqual(len(sessions), 1)  # One channel, two requests.
        self.assertIn(f'ControlPath={self.session.control_path}', sessions[0])

    @staticmethod
    def exit_first():
        """Hold the first write until the fake ssh has exited, as a fast refusal can."""
        start = ProcessTransport._start

        def started(transport):
            fresh = transport.process is None
            start(transport)
            if fresh:
                transport.process.wait(timeout=10)
        return patch.object(ProcessTransport, '_start', started)

    def test_master_lost_between_check_and_attempt(self):
        # ssh may fail before or after the request is written; both orders are covered.
        for order, marker in (('after write', 'read first'), ('before write', '')):
            with self.subTest(order=order):
                ordering = nullcontext if marker else self.exit_first
                self.session.alive.touch()
                self.session.drop.write_text(marker)
                with ordering():
                    self.assertEqual(self.refused(self.session.backend()).code, 'AUTH_REQUIRED')
                self.session.drop.unlink()
                self.session.alive.touch()
                self.session.refuse.write_text(marker)  # The master still answers: an ordinary transport failure.
                with ordering():
                    refusal = self.refused(self.session.backend())
                self.session.refuse.unlink()
                # A broken pipe on the write is UNREACHABLE and an end of output is DISCONNECTED;
                # once ssh has exited first, the selector may report either one.
                self.assertIn(refusal.code, {'DISCONNECTED'} if marker else {'DISCONNECTED', 'UNREACHABLE'})
                failure = classify_failure(refusal)
                self.assertEqual((failure.kind, failure.event), (FailureKind.TRANSPORT, NodeEvent.TRANSPORT_FAILURE))

    def test_cadence_mismatch_is_incompatible(self):
        self.session.alive.touch()
        self.session.entry['poll_interval_seconds'] = 60
        self.assertEqual(self.refused(self.session.backend()).code, 'INCOMPATIBLE_CAPABILITIES')

    def call(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()), \
                patch('cmw.nodes.transport.SSH', str(self.session.ssh)):
            code = main(arguments)
        return code, output.getvalue()

    def test_cli_connect_check_and_status(self):
        frontend = self.root / 'frontend'
        spec = self.root / 'connection.json'
        spec.write_text(json.dumps(self.session.entry))
        code, output = self.call(['nodes', 'connect', 'cluster', '--config', str(frontend), '--spec', str(spec)])
        self.assertEqual(code, 2)
        refusal = json.loads(output)
        self.assertEqual(refusal['code'], 'AUTH_REQUIRED')
        self.assertTrue(refusal['command'].startswith('ssh -M -S '))
        self.assertFalse((frontend / 'connections.json').exists())
        self.session.alive.touch()
        code, output = self.call(['nodes', 'connect', 'cluster', '--config', str(frontend), '--spec', str(spec)])
        self.assertEqual(code, 0, output)
        self.assertEqual(stat.S_IMODE((frontend / 'connections.json').stat().st_mode), 0o600)
        code, output = self.call(['nodes', 'check', 'cluster', '--config', str(frontend), '--json'])
        self.assertEqual(code, 0, output)
        self.assertFalse(json.loads(output)['result']['mutations_supported'])
        code, output = self.call(['jobs', '--nodes-config', str(frontend), '--node', 'cluster', 'status'])
        self.assertEqual(code, 0, output)
        self.assertIn('SCHEDULER JOBS — PBS 2099.0.0', output)
        self.assertIn('7.server1', output)
        code, output = self.call(['jobs', '--nodes-config', str(frontend), '--node', 'cluster', 'status', '--json'])
        self.assertEqual(json.loads(output)['result']['state']['scheduler_report']['counts']['running'], 1)
        self.session.alive.unlink()
        code, output = self.call(['nodes', 'check', 'cluster', '--config', str(frontend)])
        self.assertEqual((code, json.loads(output)['code']), (2, 'AUTH_REQUIRED'))

    def test_reconnect_lands_beside_a_stale_endpoint(self):
        # The stale endpoint's client dropped without closing the stream, as a
        # silently lost network leaves sshd holding the channel open.
        stale_root = self.root / 'stale'
        stale_root.mkdir()
        stale_session = FakeSession(stale_root, self.node, idle=1.5)
        self.addCleanup(stale_session.close)
        before = sorted(path.name for path in self.node.config_root.iterdir())
        started = time.monotonic()
        stale = subprocess.Popen([str(stale_session.executable), 'nodes', 'endpoint', '--config',
                                  str(self.node.config_root), '--queue', 'main'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: stale.poll() is None and stale.kill())
        self.session.alive.touch()
        backend = self.session.backend()
        try:
            backend.call('capabilities')
            first = backend.call('status')
            backend.reconnect()
            backend.call('capabilities')
            second = backend.call('status')
        finally:
            backend.close()
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(first['result']['state']['scheduler_report']['jobs'],
                         second['result']['state']['scheduler_report']['jobs'])  # A dropped session lost nothing.
        self.assertEqual(stale.wait(timeout=30), 0)  # It left on its own idle bound.
        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        self.assertEqual(stale.stdout.read(), b'')
        stale.stdin.close()
        stale.stdout.close()
        self.assertEqual(sorted(path.name for path in self.node.config_root.iterdir()), before)


class FakeSchedulerBackend:
    """A connected scheduler-backed lane whose status is a real adapter report."""

    def __init__(self, report, *, interval=30):
        self.alias, self.queue = 'cluster', 'main'
        self.node_id, self.queue_id = uuid4().hex, uuid4().hex
        self.connection = {'transport': 'ssh', 'auth': 'session', 'read_only': True,
                           'poll_interval_seconds': interval}
        self.poll_interval, self.report = interval, report
        self.session, self.sequence, self.error, self.calls = uuid4().hex, 0, None, []

    def call(self, operation, params=None, target=None, **kwargs):
        self.calls.append(operation)
        if self.error is not None:
            raise self.error
        self.sequence += 1
        if operation == 'capabilities':
            result = {'operations': ['capabilities', 'status'], 'mutations_supported': False,
                      'readiness': {'identity_ready': True, 'runtime_ready': True, 'ready': True},
                      'scheduler': {'schema': 1, 'kind': 'pbs', 'read_only': True,
                                    'poll_interval_seconds': self.poll_interval}}
        elif operation == 'status':
            result = {'state': {'jobs': [], 'scheduler_report': deepcopy(self.report),
                                'controller': {'schema': 1, 'kind': 'scheduler', 'online': False,
                                               'dispatch': 'scheduler-owned'}},
                      'job_count': 0, 'status_counts': {}, 'offset': 0, 'has_more': False}
        else:
            raise NodeError('UNSUPPORTED_OPERATION', 'Scheduler-backed nodes support only capabilities and status')
        return {'schema': 1, 'node_id': self.node_id, 'queue_id': self.queue_id, 'queue_alias': self.queue,
                'session_id': self.session, 'sequence': self.sequence, 'observed_at': 112.,
                'clock_id': 'f' * 64, 'runtime': {'fingerprint': 'runtime'}, 'host': HOST, 'result': result}

    def reconnect(self):
        self.session = uuid4().hex

    def close(self):
        pass


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class SchedulerLaneTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 100., 1000.
        self.backend = FakeSchedulerBackend(scheduler_report())
        self.app = NodesApp({'cluster': self.backend}, clock=lambda: self.now, wall_clock=lambda: self.wall)
        self.app.render_state = Mock()
        self.lane = self.app.lanes['cluster']

    def reply(self, operation='status', error=None):
        self.lane.request_started, self.lane.request_wall_started = self.now, self.wall
        self.app.accept('cluster', (operation, {}, None, None, False),
                        None if error else self.backend.call(operation), error)

    def test_lane_polls_at_the_pinned_interval_with_three_interval_staleness(self):
        self.assertEqual(self.lane.poll_interval, 30)
        self.reply('capabilities')
        self.reply()
        for age, fresh in ((0., True), (90., True), (90.001, False)):
            with self.subTest(age=age):
                self.assertEqual(self.lane.fresh(self.now + age), fresh)
        with patch.object(self.app, '_running', True), patch.object(self.app, 'run_worker') as worker:
            self.app.tick()
            self.assertEqual((self.lane.active_request[0], self.lane.next_poll), ('status', 130.))
            self.lane.active_request, self.lane.busy = None, False
            self.now = 129.9
            self.app.tick()
            self.assertEqual(worker.call_count, 1)
        self.reply(error=NodeError('DISCONNECTED', 'synthetic loss'))
        self.assertEqual(self.lane.next_poll, self.now + 30)
        self.reply('capabilities', NodeError('RUNTIME_MISMATCH', 'synthetic refusal'))
        self.assertEqual(self.lane.next_poll, self.now + 30)

    def test_auth_required_shows_the_command_and_never_becomes_unreachable(self):
        self.reply('capabilities')
        self.reply()
        for _ in range(4):
            self.reply('capabilities', SessionRequired(OPEN))
            self.assertEqual((self.lane.reachability.state, self.lane.reachability.consecutive_failures),
                             (NodeState.AUTH_REQUIRED, 0))
            self.assertEqual(self.lane.next_poll, self.now + 30)
        self.assertEqual(self.lane.session_command, OPEN)
        self.assertIsNone(self.lane.refusal)
        self.assertFalse(self.lane.fresh(self.now))
        self.assertTrue(self.app.connection_text('cluster').startswith('session required'))
        self.reply('capabilities')
        self.reply()
        self.assertEqual(self.lane.reachability.state, NodeState.REACHABLE)
        self.assertIsNone(self.lane.session_command)
        self.assertTrue(self.lane.fresh(self.now))

    def test_dropped_session_reconnects_without_losing_rows(self):
        self.reply('capabilities')
        self.reply()
        rows = deepcopy(self.lane.state['scheduler_report'])
        self.backend.reconnect()
        self.reply()  # A reply from a new endpoint session needs a fresh handshake first.
        self.assertFalse(self.lane.fresh(self.now))
        self.reply('capabilities')
        self.reply()
        self.assertTrue(self.lane.fresh(self.now))
        self.assertEqual(self.lane.state['scheduler_report'], rows)


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class SchedulerPortalRenderTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, pilot, predicate):
        from tests import waiting
        await waiting.until(predicate, pilot=pilot, timeout=20, interval=.05, what='Portal condition did not become true')

    async def test_cluster_lane_beside_a_mac_lane_is_read_only(self):
        cluster = FakeSchedulerBackend(scheduler_report(two_server_site()))
        app = NodesApp({'MBP': FakeBackend('MBP'), 'cluster': cluster})
        async with app.run_test(size=(120, 30)) as pilot:
            keys = [scheduler_key(cluster, job) for job in cluster.report['jobs']]
            await self.until(pilot, lambda: all(key in app.rows for key in keys) and len(app.rows) == 8)
            summary = str(app.query_one('#node-summary', Static).render())
            self.assertIn('PBS queued 1 · running 1 · finished 1', summary)
            self.assertIn('scheduler dispatch', summary)
            owner, row = app.rows[keys[0]]
            self.assertEqual((owner, row['status'], row['display_id']), ('cluster', 'running B', '12[].server2'))
            app.select(keys[1])
            app.render_state()
            self.assertIsNone(app.selected())
            self.assertFalse(app.check_action('files', ()))
            self.assertFalse(app.check_action('cancel', ()))
            selected = str(app.query_one('#selected', Static).render())
            self.assertIn('41.server2', selected)
            self.assertIn('exit 271', selected)
            self.assertIn('read-only', str(app.query_one('#shortcuts', Static).render()))
            self.assertEqual([call for call in cluster.calls if call not in {'capabilities', 'status'}], [])
            cluster.error = SessionRequired(OPEN)
            app.refuse_lane('cluster', cluster.error, 'status')
            app.render_state()
            summary = str(app.query_one('#node-summary', Static).render())
            self.assertIn('cluster Session required · run:\n' + OPEN, summary)
            # A command wider than the window is wrapped between arguments, never cut short.
            long = open_session_command('login1.cluster.example', '/tmp/cmw-u0001/' + 'session-' * 9 + 'm.sock')
            app.refuse_lane('cluster', SessionRequired(long), 'status')
            app.render_state()
            await pilot.pause()
            widget = app.query_one('#node-summary', Static)
            lines = str(widget.render()).splitlines()
            heading = lines.index('cluster Session required · run:')
            command = lines[heading + 1:]
            self.assertGreater(len(long), 118)
            self.assertGreater(len(command), 1)
            self.assertTrue(all(len(line) <= 118 for line in command), command)
            self.assertEqual(shlex.split('\n'.join(command).replace(' \\\n', ' ')), shlex.split(long))
            self.assertEqual(widget.size.height, len(lines))  # The summary grows to show every line.

    def test_command_lines_break_only_between_arguments(self):
        self.assertEqual(command_lines(OPEN, 200), [OPEN])
        lines = command_lines(OPEN, 30)
        self.assertEqual(lines, ['ssh -M -S \\', '    /tmp/cmw-session/m.sock \\', '    -fN \\',
                                 '    login1.cluster.example'])
        self.assertEqual(shlex.split('\n'.join(lines).replace(' \\\n', ' ')), shlex.split(OPEN))
        self.assertEqual(command_lines("ssh -S '/tmp/a b' x", 8), ['ssh -S \\', "    '/tmp/a b' \\", '    x'])

    async def test_watch_lane_through_the_session_transport(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        node = EnrolledNode(root / 'site', two_server_site())
        session = FakeSession(root, node)
        self.addCleanup(session.close)
        session.alive.touch()
        backend = session.backend()
        app = NodesApp({'cluster': backend})
        async with app.run_test(size=(120, 30)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 3 and app.lanes['cluster'].fresh(app.clock()))
            self.assertEqual(app.lanes['cluster'].poll_interval, 30)
            self.assertEqual({row['display_id'] for _, row in app.rows.values()},
                             {'12[].server2', '41.server2', '41.server1'})
        self.assertIsNone(backend.transport.process)  # Closing the portal closed the endpoint channel.


if __name__ == '__main__':
    unittest.main()
