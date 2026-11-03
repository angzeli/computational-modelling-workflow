"""Read-only evidence for the experimental run-vasp.sh binary-manifest route.

The VASP port may select a relinked executable only through a reviewed private
manifest (``--binary std --binary-manifest NAME``; vasp-metal-backend
docs/CONTRACT-v0.3.md). This module reads that manifest, the runner's metadata
and the backend's aggregated attempt evidence as data. It never executes the
runner, the executable or the backend aggregator, and never initialises a GPU.

The verdicts stay separate: operational completion (the runner), backend
evidence completeness, CPU-only or GPU-consumed execution, the shadow check and
GPU qualification. None of them is scientific acceptance or a speed claim.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from .result_sources import SnapshotReader, SourceSnapshotError

MANIFEST_SCHEMA = 'vasp-port.binary-manifest.v1'
EVIDENCE_SCHEMA = 'vmb.attempt-evidence.v1'
REPORT_SCHEMA = 'vmb.rank-report.v1'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_TOOLCHAIN = ('PORT_GCC_VERSION', 'PORT_OPENMPI_VERSION', 'PORT_PRRTE_VERSION',
              'PORT_OPENBLAS_VERSION', 'PORT_SCALAPACK_VERSION', 'PORT_FFTW_VERSION')
_EVIDENCE = ('COMPLETE', 'INCOMPLETE', 'INVALID', 'AGGREGATION_FAILED')
STAGING_KEYS = frozenset({'binary_selection', 'binary_manifest', 'backend', 'backend_mode',
                          'backend_check', 'backend_attempt_id', 'backend_report_dir'})
TERMINAL_KEYS = frozenset({'backend_evidence', 'backend_evidence_status', 'backend_summary'})
MAX_EXECUTABLE_BYTES = 1024 ** 3


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def manifest_name(argv):
    """Name selected by an explicit ``--binary-manifest NAME`` pair, or None."""
    if not isinstance(argv, list) or '--binary-manifest' not in argv:
        return None
    index = argv.index('--binary-manifest')
    _require(argv.count('--binary-manifest') == 1 and index + 1 < len(argv),
             'Repeated or incomplete --binary-manifest')
    name = argv[index + 1]
    _require(isinstance(name, str) and NAME.fullmatch(name) is not None,
             '--binary-manifest must be a plain name of [A-Za-z0-9._-]')
    return name


def manifest_path(runner, name):
    """The only location the port reads: PORT_ROOT/private/binary-manifests."""
    return Path(runner).absolute().parent.parent / 'private/binary-manifests' / (name + '.json')


def _reject_constant(token):
    raise ValueError(f'non-finite JSON constant {token}')


def _read_json(path, role, limit=64 * 1024):
    reader = SnapshotReader(path, role, max_bytes=limit)
    with reader:
        text = ''.join(line.text for line in reader)
    _require(reader.record['stable'] and reader.record['coverage'] == 'complete',
             f'Incomplete or changing {role}')
    return json.loads(text, parse_constant=_reject_constant), reader.record


def file_digest(path, limit=MAX_EXECUTABLE_BYTES):
    """SHA-256 of a regular file, refusing symbolic links and concurrent change."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_size <= limit,
                 f'{path} is not a bounded regular file')
        digest = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        after = os.fstat(fd)
        _require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                 f'{path} changed while it was read')
        return digest.hexdigest(), before.st_size
    finally:
        os.close(fd)


def read_manifest(runner, name):
    """Validate a reviewed manifest's structure without reading the executable."""
    path = manifest_path(runner, name)
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022,
             'Binary manifest must be a regular file that is not group/world writable')
    data, record = _read_json(path, 'binary_manifest')
    _require(isinstance(data, dict) and data.get('schema') == MANIFEST_SCHEMA
             and data.get('name') == name and data.get('status') == 'reviewed',
             'Not a reviewed vasp-port.binary-manifest.v1 for this name')
    vasp, exe = data.get('vasp'), data.get('executable')
    _require(isinstance(vasp, dict) and vasp.get('version') == '6.6.1' and vasp.get('variant') == 'std',
             'Binary manifest must select VASP 6.6.1 std')
    _require(isinstance(exe, dict) and isinstance(exe.get('path'), str) and Path(exe['path']).is_absolute()
             and isinstance(exe.get('sha256'), str) and _DIGEST.fullmatch(exe['sha256'])
             and type(exe.get('bytes')) is int and exe['bytes'] > 0, 'Malformed manifest executable')
    backend = data.get('backend')
    _require(isinstance(backend, dict) and backend.get('name') == 'vasp-metal-backend'
             and backend.get('report_schema') == REPORT_SCHEMA
             and isinstance(backend.get('aggregator'), dict)
             and _DIGEST.fullmatch(str(backend['aggregator'].get('sha256'))),
             'Malformed manifest backend identity')
    toolchain = data.get('toolchain')
    _require(isinstance(toolchain, dict) and set(toolchain) == set(_TOOLCHAIN), 'Malformed manifest toolchain')
    deps = data.get('runtime_dependencies')
    _require(isinstance(deps, list) and all(isinstance(d, dict) and set(d) == {'path', 'resolved', 'sha256'}
             and _DIGEST.fullmatch(str(d['sha256'])) for d in deps), 'Malformed manifest dependencies')
    selection = data.get('selection')
    _require(isinstance(selection, dict) and selection.get('mode') in ('off', 'auto', 'force')
             and isinstance(selection.get('shadow_check'), bool), 'Malformed manifest backend selection')
    return data, path, record


def manifest_sources(runner, name):
    """Files a node profile pins for this selection (manifest, binary, aggregator, libraries)."""
    data, path, _record = read_manifest(runner, name)
    files = [path, Path(data['executable']['path']), Path(data['backend']['aggregator']['path'])]
    files += [Path(dep['resolved']) for dep in data['runtime_dependencies']]
    return files


def verify_selection(runner, name, toolchain=None):
    """Static identity check of the selected executable; nothing is executed."""
    data, path, record = read_manifest(runner, name)
    exe = data['executable']
    observed, size = file_digest(exe['path'])
    _require(observed == exe['sha256'] and size == exe['bytes'],
             'Selected executable differs from its reviewed manifest')
    aggregator = data['backend']['aggregator']
    _require(file_digest(aggregator['path'], 1024 * 1024)[0] == aggregator['sha256'],
             'Pinned backend aggregator differs from its manifest')
    for dep in data['runtime_dependencies']:
        _require(os.path.realpath(dep['path']) == dep['resolved']
                 and file_digest(dep['resolved'])[0] == dep['sha256'],
                 f"Runtime dependency {dep['path']} differs from its manifest")
    if toolchain is not None:
        _require({k: data['toolchain'][k] for k in _TOOLCHAIN} == toolchain,
                 "Manifest toolchain differs from the runner's toolchain")
    backend = data['backend']
    return {'manifest': str(path), 'manifest_sha256': record['sha256'], 'executable': exe['path'],
            'executable_sha256': exe['sha256'], 'vasp': data['vasp'],
            'backend': {key: backend.get(key) for key in ('version', 'source_commit', 'source_digest',
                                                          'source_dirty', 'library_sha256')},
            'mode': data['selection']['mode'], 'shadow_check': data['selection']['shadow_check'],
            'qualification': 'static identity only; runner, executable and aggregator not executed'}


def _metadata(path):
    reader = SnapshotReader(path, 'runner_record', max_bytes=64 * 1024)
    with reader:
        lines = [line.text.rstrip('\n') for line in reader]
    values = {}
    for line in lines:
        key, separator, value = line.partition(': ')
        if separator:
            values.setdefault(key, []).append(value)
    return values


def check_summary(summary):
    """Invariants of the compact aggregated summary recorded by the runner."""
    _require(isinstance(summary, dict) and summary.get('evidence') in _EVIDENCE[:3]
             and summary.get('execution') in ('GPU_CONSUMED', 'CPU_ONLY')
             and summary.get('shadow_check') in ('PASSED', 'FAILED', 'NOT_ENABLED', 'NO_CHECKS')
             and isinstance(summary.get('gpu_qualified'), bool), 'Malformed backend summary')
    counts = {k: summary.get(k) for k in ('calls', 'consumed', 'cpu_calls', 'gpu_submitted',
                                          'ranks_reported', 'ranks_with_consumed', 'world_size')}
    _require(all(type(v) is int and v >= 0 for v in counts.values()), 'Malformed backend summary counts')
    _require(counts['calls'] == counts['consumed'] + counts['cpu_calls']
             and counts['gpu_submitted'] >= counts['consumed']
             and counts['ranks_reported'] <= counts['world_size']
             and counts['ranks_with_consumed'] <= counts['ranks_reported']
             and (counts['consumed'] > 0) == (summary['execution'] == 'GPU_CONSUMED'),
             'Backend summary counters are inconsistent')
    ratio = summary.get('max_check_ratio')
    _require(ratio is None or ratio == 'nonfinite' or (type(ratio) in (int, float) and math.isfinite(ratio)
             and ratio >= 0), 'Malformed backend check ratio')
    qualified = (summary['evidence'] == 'COMPLETE' and counts['consumed'] > 0
                 and summary['shadow_check'] in ('PASSED', 'NOT_ENABLED'))
    _require(summary['gpu_qualified'] == qualified, 'Backend qualification contradicts its own evidence')
    return summary


def summarize(job, *, runner_root_override=None):
    """Concise backend state of one job; None for ordinary (non-manifest) routes."""
    argv = job.get('argv') if isinstance(job, dict) else None
    try:
        name = manifest_name(argv)
    except ValueError as exc:
        return {'route': 'binary-manifest', 'state': 'UNRESOLVED', 'problems': [str(exc)],
                'line': f'Backend: UNRESOLVED ({exc})'}
    if name is None:
        return None
    result = {'route': 'binary-manifest', 'manifest': name, 'experimental': True, 'state': 'NOT_STARTED',
              'selection': None, 'evidence_status': None, 'summary': None, 'problems': []}
    runner = Path(argv[0])
    try:
        data, path, record = read_manifest(runner, name)
        result['selection'] = {'manifest': str(path), 'manifest_sha256': record['sha256'],
                               'executable': data['executable']['path'],
                               'executable_sha256': data['executable']['sha256'],
                               'mode': data['selection']['mode'],
                               'shadow_check': data['selection']['shadow_check'],
                               'backend_version': data['backend'].get('version')}
    except (OSError, ValueError, SourceSnapshotError) as exc:
        result['problems'].append(f'manifest: {exc}')
    try:
        output = Path(argv[argv.index('--output') + 1])
        metadata_path = output / 'RUN_METADATA.txt'
        if os.path.lexists(metadata_path):
            meta = _metadata(metadata_path)
            first = {key: values[0] for key, values in meta.items()}
            states = meta.get('state', [])
            result['state'] = 'FINISHED' if states[-1:] == ['FINISHED'] else (
                'FAILED' if states and states[-1].startswith('FAILED') else 'RUNNING')
            if 'backend_attempt_id' in first:
                attempt, _, origin = first['backend_attempt_id'].partition('; origin=')
                result['attempt_id'] = attempt
                if job.get('attempt_id') and (origin != 'cmw' or attempt != job['attempt_id']):
                    result['problems'].append('backend reports are bound to a different attempt')
            selection = result['selection'] or {}
            if first.get('binary') and selection.get('executable') and first['binary'] != selection['executable']:
                result['problems'].append('runner launched a different executable than the manifest selects')
            status = first.get('backend_evidence_status')
            if status is not None:
                result['evidence_status'] = status
                if status not in _EVIDENCE:
                    result['problems'].append('unknown backend evidence status')
                summary = json.loads(first.get('backend_summary', 'null'), parse_constant=_reject_constant)
                evidence_path, _, digest = first.get('backend_evidence', '').partition('; sha256=')
                if status == 'AGGREGATION_FAILED':
                    result['problems'].append('backend aggregation failed: ' + str((summary or {}).get('error')))
                else:
                    result['summary'] = check_summary(summary)
                    _require(Path(evidence_path) == output / 'vmb-evidence.json', 'Evidence path moved')
                    evidence, evidence_record = _read_json(Path(evidence_path), 'backend_evidence', 1024 * 1024)
                    _require(evidence_record['sha256'] == digest and evidence.get('schema') == EVIDENCE_SCHEMA
                             and evidence.get('evidence') == status
                             and evidence.get('attempt_id') == result.get('attempt_id'),
                             'Backend evidence file differs from the runner record')
                    if status != 'COMPLETE':
                        result['problems'].append(f"backend evidence {status}: missing ranks "
                                                  f"{evidence.get('missing_ranks')}; "
                                                  + '; '.join(str(p) for p in (evidence.get('problems') or [])[:3]))
                    if summary['shadow_check'] == 'FAILED':
                        result['problems'].append('shadow check failed')
            elif result['state'] == 'FINISHED':
                result['problems'].append('finished without backend evidence')
    except (OSError, ValueError, KeyError, IndexError, SourceSnapshotError) as exc:
        result['problems'].append(f'evidence: {exc}')
    result['line'] = _line(result)
    return result


def _line(result):
    selection = result.get('selection') or {}
    head = (f"Backend: EXPERIMENTAL Metal ZGEMM via manifest {result['manifest']} "
            f"(mode={selection.get('mode', '?')}, check={int(bool(selection.get('shadow_check')))})")
    summary = result.get('summary')
    if summary:
        ratio = summary.get('max_check_ratio')
        body = (f"evidence {summary['evidence']} {summary['ranks_reported']}/{summary['world_size']} ranks; "
                f"{summary['execution']}: consumed {summary['consumed']} of {summary['calls']} calls on "
                f"{summary['ranks_with_consumed']} ranks; shadow {summary['shadow_check']}"
                + (f' (max ratio {ratio:.3g})' if isinstance(ratio, (int, float)) else '')
                + f"; GPU qualified {'yes' if summary['gpu_qualified'] else 'NO'}")
    else:
        body = {'NOT_STARTED': 'not started; no backend evidence yet',
                'RUNNING': 'running; evidence published at exit'}.get(result['state'],
                                                                       f"{result['state']}; no backend evidence")
    problems = result.get('problems') or []
    tail = f" — PROBLEM: {'; '.join(problems)}" if problems else ''
    return f'{head}: {body}{tail}'
