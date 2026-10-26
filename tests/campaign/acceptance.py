"""One bounded installed-candidate soak built from existing acceptance fixtures.

The deterministic mode uses local pipes and no listener. The explicit soak mode
uses the existing private SSH/SFTP fixture and a real terminal. Neither executes
a calculation payload, discovers research processes, or writes production state.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import cmw
import psutil

from cmw.jobs.review_models import ReviewRecord
from cmw.jobs.store import Store
from cmw.nodes.client import fetch
from cmw.nodes.codec import decode_typed
from cmw.nodes.identity import runtime_identity
from cmw.nodes.protocol import NodeError
from cmw.progress.models import ProgressSnapshot
from tests.campaign.loopback import LoopbackSSHD
from tests.campaign.support import (BUDGETS, assert_native, assert_review, catalogue_query, changing_log, connect, fixture,
                                    owned_sample, resource_sample, summary)


def review(client, target, *, relaxation=False):
    before = client.call('review.preview', {}, target)['result']['review']
    assert decode_typed(before, ReviewRecord).status in {'PREVIEW', 'CACHED'}
    started = time.perf_counter()
    value = client.call('review.start', {'options': {
        'max_bytes': BUDGETS['review_per_source_bytes'],
        'timeout_seconds': BUDGETS['review_deadline_seconds']}}, target)['result']['review']
    record = decode_typed(value, ReviewRecord)
    deadline = time.monotonic()+BUDGETS['review_deadline_seconds']+3
    while record.status == 'PENDING':
        assert time.monotonic() < deadline, 'Selected review exceeded its fixed deadline'
        time.sleep(.1)
        record = decode_typed(client.call('review.poll', {'review_id': record.review_id},
            target)['result']['review'], ReviewRecord)
    assert_review(record, relaxation=relaxation)
    return {'seconds': time.perf_counter()-started, 'record': record.to_dict(),
            'source_evidence_bytes_read': sum(s.get('bytes_read', 0) for s in json.loads(record.sources_json)),
            'source_evidence_bytes_hashed': sum(s.get('bytes_read', 0) for s in json.loads(record.sources_json) if s.get('sha256'))}


def sftp_command(server):
    return ['/usr/bin/sftp', '-F', '/dev/null', '-v', '-b', '-', '-P', str(server.port),
        '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', f'UserKnownHostsFile={server.root/"known_hosts"}',
        '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'IdentitiesOnly=yes',
        '-o', f'IdentityFile={server.root/"user_ed25519"}', '-o', 'ForwardAgent=no',
        '-o', 'ClearAllForwardings=yes', '-o', 'ControlMaster=no',
        '-o', 'ConnectTimeout=3', f'{server.user}@127.0.0.1']


def fetch_source(client, row, root, server):
    locations = client.call('locations', target=row['target'])['result']['locations']
    location = next(item for item in locations if item['node_path'] == row['science'])
    spec = {'schema': 1, 'storage': 'scratch', 'purpose': 'summaries',
        'capture': 'observation', 'large_files': False,
        'files': [{'location_id': location['location_id'], 'relative': 'OSZICAR', 'path': 'OSZICAR'}]}
    with patch('cmw.nodes.fetch.sftp_command', lambda connection: sftp_command(server)):
        value = fetch(client, row['target'], spec, root/'replica', root=root/'requests')['result']
    source = Path(row['science'])/'OSZICAR'
    replica = root/'replica/OSZICAR'
    assert replica.read_bytes() == source.read_bytes()
    assert value['status'] == 'FETCHED'
    assert source.stat().st_size <= BUDGETS['sftp_payload_bytes']
    return {'actual_sftp': True, 'payload_bytes': source.stat().st_size,
            'source': str(source), 'replica': str(replica), 'result': value,
            'wire_bytes': None, 'wire_note': 'SFTP payload bytes are measured; encrypted SFTP overhead is not inferred.'}


def run(root, mode, expected_runtime):
    root.mkdir(mode=0o700)
    (root/'tmp').mkdir(mode=0o700)
    (root/'budgets.json').write_text(json.dumps(BUDGETS, indent=2)+'\n')
    report = {'schema': 1, 'mode': mode, 'budgets': BUDGETS,
        'runtime': runtime_identity(), 'expected_runtime_fingerprint': expected_runtime['fingerprint'], 'python': sys.executable, 'package': cmw.__file__,
        'qualification': 'Synthetic native-shaped files and metadata; no scientific acceptance or physical-node claim.',
        'payloads_started': 0, 'histories': [], 'resources': [], 'checks': {}, 'passed': False,
        'source_byte_scope': 'Progress reader actual pread counters and deduplicated review source evidence; not total OS or repeated parser I/O.'}
    clients, records, protected = [], [], {}
    server = None
    sampling = threading.Event()
    own = psutil.Process()
    initial = resource_sample(own)
    started = time.monotonic()
    sampler = None
    old_handler = signal.getsignal(signal.SIGALRM)
    def timeout(signum, frame):
        raise TimeoutError('Fixed campaign wall deadline exceeded')
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, BUDGETS['wall_seconds'])
    try:
        assert report['runtime']['fingerprint'] == expected_runtime['fingerprint']
        load = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        report['host_preflight'] = {'load_average_1_5_15_minutes': load,
            'logical_cpus': cpu_count, 'load_1m_per_cpu': load[0]/cpu_count,
            'available_memory_bytes': psutil.virtual_memory().available}
        report['changing_long_log'] = changing_log(root/'changing-long-log')
        with ExitStack() as stack:
            for count in BUDGETS['history_rows']:
                row = fixture(root/f'history-{count}', count)
                records.append(row)
                path = Store(row['state']).path
                protected[str(path)] = path.read_bytes()
                protected.update({str(p): p.read_bytes() for p in Path(row['science']).iterdir()})
            if mode == 'soak':
                server = stack.enter_context(LoopbackSSHD(root/'ssh', sftp=True))
                key = subprocess.run(['/usr/bin/ssh-keygen', '-lf', str(server.root/'host_ed25519.pub'), '-E', 'sha256'],
                    capture_output=True, text=True, check=True, timeout=10).stdout.split()[1]
                server.fingerprint = key
            clients = [connect(record, server=server) for record in records]
            def sample():
                while not sampling.wait(.1):
                    roots = [own]
                    if server and server.process and server.process.poll() is None:
                        roots.append(psutil.Process(server.process.pid))
                    report['resources'].append({'at_seconds': time.monotonic()-started,
                        'processes': owned_sample(roots)})
            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
            for client, row in zip(clients, records):
                capabilities = client.call('capabilities')
                assert capabilities['result']['readiness']['identity_ready']
                count = row['rows']
                cycles = BUDGETS['cycles_per_history'] if mode == 'soak' else 2
                progress_bytes, page_bytes, timings = [], [], []
                selected = row['target']
                for _ in range(cycles):
                    tick = time.perf_counter()
                    status = client.call('status', {'limit': BUDGETS['page_rows']})['result']
                    assert status['job_count'] == count
                    assert len(status['state']['jobs']) == BUDGETS['page_rows']
                    page = catalogue_query(client)
                    assert page.total_count == count
                    second = catalogue_query(client, cursor=page.next_cursor)
                    assert not {r.job_id for r in page.records} & {r.job_id for r in second.records}
                    page_bytes.append(len(json.dumps(asdict(page)).encode()))
                    snap = decode_typed(client.call('progress', target=selected)['result']['snapshot'], ProgressSnapshot)
                    progress_deadline = time.monotonic()+5
                    while not snap.metrics and time.monotonic() < progress_deadline:
                        time.sleep(.05)
                        snap = decode_typed(client.call('progress', target=selected)['result']['snapshot'], ProgressSnapshot)
                    assert snap.context.attempt_id == selected['attempt_id']
                    assert_native(snap, 2 if count == 10000 else 1)
                    progress_bytes.append(sum(s.total_bytes_read for s in snap.sources))
                    timings.append(time.perf_counter()-tick)
                    if mode == 'soak':
                        time.sleep(BUDGETS['cycle_pause_seconds'])
                reviewed = review(client, selected, relaxation=count == 10000)
                wrong = {**selected, 'queue_id': 'f'*32}
                try:
                    client.call('show', target=wrong)
                except NodeError:
                    pass
                else:
                    raise AssertionError('Wrong queue target was accepted')
                # An error deliberately closes the transport. Reconnect through
                # capabilities before any later mutation such as explicit fetch.
                client.call('capabilities')
                report['histories'].append({'rows': count, 'cycles': cycles,
                    'cycle_seconds': summary(timings), 'page_json_bytes': summary(page_bytes),
                    'progress_total_bytes': progress_bytes, 'selected_review': reviewed})
            report['checks']['bounded_pages_exact_selected_native_reads'] = True
            report['checks']['wrong_queue_refused'] = True
            if server is not None:
                # Two endpoints during observation; close one before the
                # separate SFTP connection so the fixture keeps two clients.
                clients[-1].close()
                report['sftp'] = fetch_source(clients[0], records[0], root, server)
                report['checks']['actual_sftp_native_replica'] = True
                for client in clients:
                    client.close()
                from tests.campaign.pty import run as run_terminal
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unavailable:
                    unavailable.bind(('127.0.0.1', 0))
                    settings = root/'terminal-settings.json'
                    settings.write_text(json.dumps({'view': 'nodes', 'fixtures': records,
                        'unavailable_port': unavailable.getsockname()[1]}))
                    report['pty'] = run_terminal(settings, root/'pty')
                settings.write_text(json.dumps({'view': 'local', 'fixtures': records}))
                report['local_pty'] = run_terminal(settings, root/'local-pty')
                report['checks']['native_endpoint_to_real_terminal_review_and_unavailable_owner'] = True
            assert all(Path(path).read_bytes() == data for path, data in protected.items())
            for row in records:
                state = Store(row['state']).snapshot()
                assert len(state['jobs']) == row['rows']
                assert all(job['status'] == 'Hold' for job in state['jobs'])
                assert not state['controller']['online'] and not state['controller']['dispatch']
            report['checks']['queues_and_sources_unchanged_no_dispatch'] = True
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        for client in clients:
            client.close()
        if server:
            server.close()
        sampling.set()
        if sampler:
            sampler.join(timeout=2)
        samples = [sample for client in clients for sample in client.transport.samples]
        pty_report = report.get('pty', {}).get('metrics', {})
        samples += [sample for rows in pty_report.get('requests', {}).values() for sample in rows]
        report['requests'] = samples
        report['protocol'] = {'encoded_attempted_request_bytes': sum(s['request_bytes'] for s in samples),
            'encoded_successful_response_bytes': sum(s['response_bytes'] for s in samples),
            'latency_by_operation': {operation: summary([s['seconds'] for s in samples if s['operation'] == operation and s['outcome'] == 'ok'])
                for operation in sorted({s['operation'] for s in samples})},
            'ssh_wire_sessions': [wire for client in clients for wire in client.transport.wire_sessions]
                + pty_report.get('ssh_wire_sessions', [])}
        identities = [owner for client in clients for owner in client.transport.owners]
        identities += pty_report.get('owned_transports', [])
        identities += [process for sample in report['resources'] for process in sample['processes']
                       if process['pid'] != own.pid]
        if server:
            identities += server.identities
        remaining = []
        checked_identities = set()
        for owner in identities:
            key = (owner['pid'], owner['birth'])
            if key in checked_identities:
                continue
            checked_identities.add(key)
            try:
                process = psutil.Process(owner['pid'])
                if process.create_time() == owner['birth'] and process.status() != psutil.STATUS_ZOMBIE:
                    remaining.append(owner)
            except psutil.NoSuchProcess:
                pass
        listener_closed = None
        if server:
            with socket.socket() as sock:
                sock.settimeout(.3)
                listener_closed = sock.connect_ex(('127.0.0.1', server.port)) != 0
        report['cleanup'] = {'remaining_owned_processes': remaining, 'listener_closed': listener_closed,
            'sampler_stopped': sampler is None or not sampler.is_alive(),
            'frontend_before': initial, 'frontend_after': resource_sample(own)}
        report['peak_owned_subprocesses'] = max((sum(p['pid'] != own.pid for p in sample['processes'])
            for sample in report['resources']), default=0)
        report['wall_seconds'] = time.monotonic()-started
        violations = []
        if remaining or listener_closed is False or not report['cleanup']['sampler_stopped']:
            violations.append('Owned process, listener or sampling thread remained')
        if report['peak_owned_subprocesses'] > BUDGETS['owned_subprocesses']:
            violations.append('Owned subprocess count budget exceeded')
        all_resources = [p for sample in report['resources'] for p in sample['processes']]
        if any(p['rss_bytes'] > BUDGETS['process_rss_bytes'] or p['fds'] > BUDGETS['process_fds'] for p in all_resources):
            violations.append('Per-process RSS/FD budget exceeded')
        if report['cleanup']['frontend_after']['fds']-initial['fds'] > BUDGETS['frontend_fd_growth']:
            violations.append('Frontend descriptor growth exceeded')
        if any(max(s['request_bytes'], s['response_bytes']) > BUDGETS['protocol_frame_bytes'] for s in samples):
            violations.append('Protocol frame budget exceeded')
        if sum(s['request_bytes']+s['response_bytes'] for s in samples) > BUDGETS['protocol_total_bytes']:
            violations.append('Protocol traffic budget exceeded')
        for operation, timing in report['protocol']['latency_by_operation'].items():
            if operation in {'status', 'catalogue.query', 'progress'} and timing.get('p95', 0) > BUDGETS['request_p95_seconds']:
                violations.append(operation+' latency budget exceeded')
        report['budget_violations'] = violations
        report['passed'] = 'error' not in report and not violations
        (root/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    assert report['passed'], report['budget_violations']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('deterministic', 'soak'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--artifact-report', type=Path, required=True)
    args = parser.parse_args()
    assert sys.flags.isolated, 'Use an isolated interpreter and copied test-only driver'
    assert Path(cmw.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), cmw.__file__
    assert not (Path(__file__).resolve().parents[2]/'src').exists()
    artifact = json.loads(args.artifact_report.read_text())
    assert artifact['kind'] == 'jobs' and artifact['successful']
    assert Path(artifact['python']).resolve() == Path(sys.executable).resolve()
    assert Path(artifact['package_origin']).resolve() == Path(cmw.__file__).resolve()
    assert artifact['runtime']['fingerprint'] == runtime_identity()['fingerprint']
    result = run(args.output.resolve(), args.mode, artifact['runtime'])
    print(json.dumps({'passed': result['passed'], 'mode': args.mode,
        'wall_seconds': result['wall_seconds'], 'report': str(args.output/'result.json')}))


if __name__ == '__main__':
    main()
