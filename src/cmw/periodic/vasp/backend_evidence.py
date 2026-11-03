"""Read-only evidence for the experimental run-vasp.sh binary-manifest route.

The VASP port may select a relinked executable only through a reviewed private
manifest (``--binary std --binary-manifest NAME``; the port's docs/LAUNCHER.md).
This module reads that manifest, the runner's metadata
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


_COUNTERS = ('calls', 'gpu_submitted', 'gpu_completed', 'consumed', 'cpu_calls', 'checked')
_STATUSES = ('ok', 'disabled', 'trivial', 'badarg', 'nonfinite', 'too_small', 'too_large',
             'no_device', 'gpu_error', 'gpu_nonfinite', 'check_failed', 'range', 'auto_cpu')
_MODES = ('off', 'auto', 'force')


def _count(value, label):
    _require(type(value) is int and value >= 0, f'{label} must be a non-negative integer')
    return value


def _ranks(value, label, world_size):
    _require(isinstance(value, list) and all(type(r) is int and 0 <= r < world_size for r in value)
             and value == sorted(set(value)), f'{label} must be sorted unique ranks below the world size')
    return value


def verify_aggregate(evidence, *, attempt_id, world_size, mode, check):
    """Validate a vmb.attempt-evidence.v1 document and return its compact summary.

    The checks re-derive every verdict from the structured fields, so a document
    whose counters, rank sets or verdicts disagree is rejected even when its
    digest is consistent. The aggregator itself is never executed.
    """
    _require(isinstance(evidence, dict) and evidence.get('schema') == EVIDENCE_SCHEMA,
             'Backend evidence is not vmb.attempt-evidence.v1')
    _require(evidence.get('attempt_id') == attempt_id, 'Backend evidence belongs to another attempt')
    _require(type(evidence.get('world_size')) is int and evidence['world_size'] == world_size,
             'Backend evidence world size differs from the runner ranks')
    status = evidence.get('evidence')
    _require(status in _EVIDENCE[:3], 'Unknown backend evidence status')
    problems = evidence.get('problems')
    _require(isinstance(problems, list) and all(isinstance(p, str) for p in problems),
             'Malformed backend evidence problems')
    reported = _ranks(evidence.get('ranks_reported'), 'ranks_reported', world_size)
    missing = _ranks(evidence.get('missing_ranks'), 'missing_ranks', world_size)
    consumed_ranks = _ranks(evidence.get('ranks_with_consumed'), 'ranks_with_consumed', world_size)
    claimed = _ranks(evidence.get('claimed_without_report'), 'claimed_without_report', world_size)
    _require(not set(reported) & set(missing) and set(consumed_ranks) <= set(reported)
             and set(claimed) <= set(missing), 'Backend evidence rank sets are contradictory')
    if status == 'COMPLETE':
        _require(not problems and not missing and reported == list(range(world_size)),
                 'COMPLETE evidence must cover every rank without problems')
    elif status == 'INCOMPLETE':
        _require(not problems and missing and sorted(reported + missing) == list(range(world_size)),
                 'INCOMPLETE evidence must list exactly the missing ranks and no problems')
    else:
        _require(bool(problems), 'INVALID evidence must state its problems')
    totals, statuses = evidence.get('totals'), evidence.get('statuses')
    if totals is None:
        _require(status == 'INVALID' and statuses is None and not reported,
                 'Only an unreadable INVALID attempt may omit counters')
        counts = dict.fromkeys(_COUNTERS, 0)
        outcome = dict.fromkeys(_STATUSES, 0)
    else:
        _require(isinstance(totals, dict) and set(totals) == set(_COUNTERS)
                 and isinstance(statuses, dict) and set(statuses) == set(_STATUSES),
                 'Malformed backend evidence counters')
        counts = {k: _count(totals[k], 'total ' + k) for k in _COUNTERS}
        outcome = {k: _count(statuses[k], 'status ' + k) for k in _STATUSES}
        _require(counts['calls'] == counts['consumed'] + counts['cpu_calls']
                 and counts['gpu_submitted'] >= counts['gpu_completed'] >= counts['consumed']
                 and sum(outcome.values()) == counts['calls'] and outcome['ok'] == counts['consumed']
                 and counts['gpu_submitted'] - counts['gpu_completed'] == outcome['gpu_error']
                 and counts['gpu_completed'] - counts['consumed']
                 == outcome['gpu_nonfinite'] + outcome['check_failed'],
                 'Backend evidence counters are inconsistent')
        _require(reported or all(v == 0 for v in counts.values()), 'Counters recorded without any rank report')
    _require((counts['consumed'] > 0) == bool(consumed_ranks), 'Consumed ranks contradict consumed calls')
    # Each listed rank consumed at least one GPU result.
    _require(len(consumed_ranks) <= counts['consumed'], 'Evidence lists more consumed ranks than consumed calls')
    ev_mode, ev_check = evidence.get('mode'), evidence.get('check')
    if reported:
        _require(ev_mode in _MODES and ev_mode == mode and type(ev_check) is bool and ev_check == check,
                 'Backend evidence mode/check differ from the runner record')
    else:
        _require(ev_mode is None and ev_check is None, 'Mode/check recorded without any rank report')
    if ev_mode == 'off':
        _require(counts['gpu_submitted'] == 0, 'Off-mode evidence records GPU submissions')
    if ev_check is False:
        _require(counts['checked'] == 0 and outcome['check_failed'] == 0, 'Checks recorded with the check off')
    elif ev_check is True:
        _require(counts['checked'] == counts['consumed'] + outcome['check_failed'],
                 'checked != consumed + check_failed')
    ratio = evidence.get('max_check_ratio')
    _require(ratio is None or ratio == 'nonfinite'
             or (type(ratio) in (int, float) and math.isfinite(ratio) and ratio >= 0),
             'Malformed backend check ratio')
    _require((ratio is None) == (counts['checked'] == 0), 'Check ratio contradicts the check count')
    execution = 'GPU_CONSUMED' if counts['consumed'] > 0 else 'CPU_ONLY'
    if ev_check is False:
        shadow = 'NOT_ENABLED'
    elif outcome['check_failed'] or ratio == 'nonfinite' or (ratio is not None and ratio > 1.0):
        shadow = 'FAILED'
    elif counts['checked'] == 0:
        shadow = 'NO_CHECKS'
    else:
        shadow = 'PASSED'
    qualified = status == 'COMPLETE' and counts['consumed'] > 0 and shadow in ('PASSED', 'NOT_ENABLED')
    _require(evidence.get('execution') == execution and evidence.get('shadow_check') == shadow
             and evidence.get('gpu_qualified') is qualified,
             'Backend evidence verdicts contradict its own counters')
    present = totals is not None
    return {'evidence': status, 'execution': execution, 'shadow_check': shadow, 'gpu_qualified': qualified,
            'mode': ev_mode, 'check': ev_check, 'world_size': world_size, 'max_check_ratio': ratio,
            'ranks_reported': len(reported), 'missing_ranks': missing,
            'ranks_with_consumed': len(consumed_ranks),
            'calls': counts['calls'] if present else None, 'consumed': counts['consumed'] if present else None,
            'cpu_calls': counts['cpu_calls'] if present else None,
            'gpu_submitted': counts['gpu_submitted'] if present else None,
            'range_fallbacks': outcome['range'] if present else None, 'problems': len(problems)}


def verify_record(values, output, *, attempt_id, world_size, mode, check):
    """Verify the runner's terminal backend keys against the evidence file's bytes.

    Raises ValueError for any integrity failure: a moved or missing file, a
    digest that does not match the bytes, malformed or contradictory content, or
    a compact summary that differs from the file. Honest INCOMPLETE, INVALID and
    AGGREGATION_FAILED records verify, but never as qualified GPU execution.
    """
    status = values.get('backend_evidence_status')
    _require(status in _EVIDENCE, 'Unknown backend evidence status')
    path, separator, digest = values.get('backend_evidence', '').partition('; sha256=')
    _require(separator and Path(path) == Path(output) / 'vmb-evidence.json',
             'Backend evidence must be OUTPUT/vmb-evidence.json')
    summary = json.loads(values.get('backend_summary', ''), parse_constant=_reject_constant)
    if status == 'AGGREGATION_FAILED':
        _require(digest == 'NONE' and isinstance(summary, dict) and set(summary) == {'error'}
                 and isinstance(summary['error'], str), 'Malformed aggregation-failure record')
        return {'evidence_status': status, 'verified': False, 'summary': None, 'gpu_qualified': False,
                'execution': None, 'shadow_check': None, 'evidence_sha256': None,
                'problems': ['backend aggregation failed: ' + summary['error']]}
    _require(_DIGEST.fullmatch(digest) is not None, 'Backend evidence digest is malformed')
    try:
        evidence, record = _read_json(Path(path), 'backend_evidence', 1024 * 1024)
    except SourceSnapshotError as exc:
        raise ValueError(f'Backend evidence file is unreadable: {exc}') from None
    _require(record['sha256'] == digest, 'Backend evidence bytes differ from the recorded digest')
    expected = verify_aggregate(evidence, attempt_id=attempt_id, world_size=world_size, mode=mode, check=check)
    _require(expected['evidence'] == status, 'Backend evidence status differs from the runner record')
    _require(summary == expected, 'Backend summary differs from the evidence file')
    problems = [f"backend evidence {status}: missing ranks {expected['missing_ranks']}; "
                + '; '.join(evidence['problems'][:3])] if status != 'COMPLETE' else []
    if expected['shadow_check'] == 'FAILED':
        problems.append('shadow check failed')
    return {'evidence_status': status, 'verified': True, 'summary': expected,
            'gpu_qualified': expected['gpu_qualified'], 'execution': expected['execution'],
            'shadow_check': expected['shadow_check'], 'evidence_sha256': digest, 'problems': problems}


def _metadata_selection(first):
    mode, check = first.get('backend_mode'), first.get('backend_check')
    _require(mode in _MODES and check in ('True', 'False'), 'Malformed backend mode/check metadata')
    return mode, check == 'True'


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
              'selection': None, 'evidence_status': None, 'verified': False, 'summary': None, 'problems': []}
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
        ranks = int(argv[argv.index('--ranks') + 1])
        metadata_path = output / 'RUN_METADATA.txt'
        if os.path.lexists(metadata_path):
            meta = _metadata(metadata_path)
            first = {key: values[0] for key, values in meta.items()}
            states = meta.get('state', [])
            result['state'] = 'FINISHED' if states[-1:] == ['FINISHED'] else (
                'FAILED' if states and states[-1].startswith('FAILED') else 'RUNNING')
            attempt = None
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
                mode, check = _metadata_selection(first)
                if selection and (selection.get('mode'), selection.get('shadow_check')) != (mode, check):
                    result['problems'].append('runner mode/check differ from the manifest selection')
                facts = verify_record(first, output, attempt_id=attempt, world_size=ranks, mode=mode, check=check)
                result.update(verified=facts['verified'], summary=facts['summary'])
                result['problems'] += facts['problems']
            elif result['state'] == 'FINISHED':
                result['problems'].append('finished without backend evidence')
    except (OSError, ValueError, KeyError, IndexError, TypeError, SourceSnapshotError) as exc:
        # An unverifiable record never becomes a verdict: no CPU_ONLY, no qualification.
        result.update(verified=False, summary=None)
        result['problems'].append(f'evidence UNVERIFIED: {exc}')
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
    elif result.get('evidence_status'):
        body = f"evidence {result['evidence_status']}; no verified backend verdict"
    else:
        body = {'NOT_STARTED': 'not started; no backend evidence yet',
                'RUNNING': 'running; evidence published at exit'}.get(result['state'],
                                                                       f"{result['state']}; no backend evidence")
    problems = result.get('problems') or []
    tail = f" — PROBLEM: {'; '.join(problems)}" if problems else ''
    return f'{head}: {body}{tail}'
