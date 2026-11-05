"""Bounded versioned JSON-lines protocol; no executable objects on the wire."""
from __future__ import annotations

import json
import math
import re
from uuid import uuid4

PROTOCOL = 1
MAX_FRAME = 2 * 1024 * 1024
MAX_DEPTH = 24
OPERATIONS = frozenset({
    'capabilities', 'status', 'lookup', 'show', 'observe', 'progress', 'history',
    'history.release', 'logs', 'locations', 'sources.inspect', 'sources.validate',
    'sources.activate', 'sources.clear', 'request', 'submit.check', 'submit.preview', 'submit',
    'transfer.begin', 'transfer.publish', 'transfer.lookup', 'control.preview', 'control',
    'fetch.prepare', 'fetch.complete', 'doctor.inspect',
    'review.preview', 'review.start', 'review.poll', 'review.cancel',
    'successor.plan', 'successor.materialize', 'successor.register',
    'study.plan', 'study.materialize', 'study.register', 'study.compare', 'study.view',
    'catalogue.query', 'catalogue.show', 'catalogue.relations', 'catalogue.edit', 'catalogue.relation.add', 'catalogue.relation.retract',
})
MUTATIONS = frozenset({'study.materialize', 'study.register', 'successor.materialize', 'successor.register', 'submit', 'transfer.begin', 'transfer.publish', 'control',
                       'fetch.prepare', 'fetch.complete', 'catalogue.edit',
                       'catalogue.relation.add', 'catalogue.relation.retract'})


def is_mutation(operation, params=None):
    """Review export writes on its owner; ordinary inspection stays read-only."""
    options = params.get('options') if isinstance(params, dict) else None
    return operation in MUTATIONS or (operation == 'study.compare' and
        isinstance(params, dict) and params.get('export_directory') is not None) or (operation == 'review.start' and
        isinstance(options, dict) and options.get('export_path') is not None)


class NodeError(ValueError):
    """A refused or uncertain operation, never an implied lifecycle transition."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def identifier(value, name='identity'):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise NodeError('INVALID_REQUEST', f'{name} must be a lowercase UUID hex identity')
    return value


def target_identity(value):
    if not isinstance(value, dict) or set(value) != {'node_id', 'queue_id', 'job_id', 'attempt_id'}:
        raise NodeError('INVALID_TARGET', 'Target requires node, queue, internal job and attempt identities')
    for key in ('node_id', 'queue_id', 'attempt_id'):
        identifier(value[key], key)
    if type(value['job_id']) is not int or value['job_id'] < 1:
        raise NodeError('INVALID_TARGET', 'Internal job ID must be a positive integer')
    return value


def bounded_value(value, depth=0):
    if depth > MAX_DEPTH:
        raise NodeError('INVALID_FRAME', 'Message nesting exceeds the protocol limit')
    if value is None or type(value) in (bool, int):
        if type(value) is int and abs(value) > 2**64:
            raise NodeError('INVALID_FRAME', 'Integer outside supported range')
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NodeError('INVALID_FRAME', 'Non-finite number')
        return
    if isinstance(value, str):
        if len(value.encode('utf-8')) > MAX_FRAME or '\0' in value:
            raise NodeError('INVALID_FRAME', 'Invalid or oversized string')
        return
    if isinstance(value, list):
        if len(value) > 4096:
            raise NodeError('INVALID_FRAME', 'Too many list entries')
        for item in value:
            bounded_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 128 or any(not isinstance(key, str) or len(key) > 256 for key in value):
            raise NodeError('INVALID_FRAME', 'Invalid object keys')
        for item in value.values():
            bounded_value(item, depth + 1)
        return
    raise NodeError('INVALID_FRAME', 'Only JSON data is supported')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise NodeError('INVALID_FRAME', 'Duplicate JSON object key')
        result[key] = value
    return result


def decode(data):
    if not isinstance(data, bytes) or not data.endswith(b'\n') or len(data) > MAX_FRAME:
        raise NodeError('INVALID_FRAME', 'Missing newline or oversized protocol frame')
    try:
        value = json.loads(data, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite')))
        bounded_value(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, NodeError):
            raise
        raise NodeError('INVALID_FRAME', 'Malformed JSON protocol frame') from exc
    if not isinstance(value, dict):
        raise NodeError('INVALID_FRAME', 'Frame must be an object')
    return value


def encode(value):
    bounded_value(value)
    data = (json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n').encode()
    if len(data) > MAX_FRAME:
        raise NodeError('FRAME_TOO_LARGE', 'Result exceeds the protocol frame limit; narrow the request')
    return data


def request(operation, params=None, target=None, request_id=None):
    value = dict(protocol=PROTOCOL, request_id=request_id or uuid4().hex,
                 operation=operation, target=target, params={} if params is None else params)
    validate_request(value)
    return value


def validate_request(value):
    if set(value) != {'protocol', 'request_id', 'operation', 'target', 'params'}:
        raise NodeError('INVALID_REQUEST', 'Unexpected request fields')
    if type(value['protocol']) is not int or value['protocol'] != PROTOCOL:
        raise NodeError('INCOMPATIBLE_PROTOCOL', 'Endpoint requires protocol 1')
    identifier(value['request_id'], 'request ID')
    if not isinstance(value['operation'], str) or value['operation'] not in OPERATIONS:
        raise NodeError('UNSUPPORTED_OPERATION', 'Operation is not available')
    if not isinstance(value['params'], dict):
        raise NodeError('INVALID_REQUEST', 'Parameters must be an object')
    if value['target'] is not None:
        target_identity(value['target'])
    bounded_value(value)
    return value


def response(value, request_id):
    if not isinstance(value, dict) or value.get('protocol') != PROTOCOL or type(value.get('protocol')) is not int:
        raise NodeError('INCOMPATIBLE_PROTOCOL', 'Response protocol differs')
    if value.get('request_id') != request_id or type(value.get('ok')) is not bool:
        raise NodeError('INVALID_RESPONSE', 'Response does not match the outstanding request')
    expected = {'protocol', 'request_id', 'ok', 'result' if value['ok'] else 'error'}
    if set(value) != expected:
        raise NodeError('INVALID_RESPONSE', 'Unexpected response fields')
    if not value['ok']:
        error = value['error']
        if not isinstance(error, dict) or set(error) != {'code', 'message'} or any(not isinstance(v, str) for v in error.values()):
            raise NodeError('INVALID_RESPONSE', 'Malformed endpoint error')
        raise NodeError(error['code'], error['message'])
    if not isinstance(value['result'], dict):
        raise NodeError('INVALID_RESPONSE', 'Endpoint result must be an object')
    return value['result']


def validate_envelope(value, operation=None):
    """Reject malformed observations before they can replace frontend cache state."""
    fields = {'schema', 'node_id', 'queue_id', 'queue_alias', 'session_id', 'sequence',
              'observed_at', 'clock_id', 'runtime', 'host', 'result'}
    if not isinstance(value, dict) or set(value) != fields:
        raise NodeError('INVALID_RESPONSE', 'Malformed observation envelope fields')
    if type(value['schema']) is not int or value['schema'] != 1:
        raise NodeError('INVALID_RESPONSE', 'Unsupported observation envelope schema')
    for key in ('node_id', 'queue_id', 'session_id'):
        identifier(value[key], key)
    if type(value['sequence']) is not int or value['sequence'] < 1:
        raise NodeError('INVALID_RESPONSE', 'Invalid observation sequence')
    if type(value['observed_at']) not in (int, float) or not math.isfinite(value['observed_at']):
        raise NodeError('INVALID_RESPONSE', 'Invalid node-local observation timestamp')
    if not isinstance(value['queue_alias'], str) or not 1 <= len(value['queue_alias']) <= 64:
        raise NodeError('INVALID_RESPONSE', 'Invalid queue alias')
    if not isinstance(value['clock_id'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['clock_id']):
        raise NodeError('INVALID_RESPONSE', 'Invalid endpoint clock identity')
    for key in ('runtime', 'host', 'result'):
        if not isinstance(value[key], dict):
            raise NodeError('INVALID_RESPONSE', f'Invalid {key} observation')
    bounded_value(value)
    result = value['result']
    if operation == 'doctor.inspect':
        report = result.get('report')
        if (set(result) != {'report'} or not isinstance(report, dict)
                or type(report.get('schema')) is not int or report['schema'] != 1
                or not isinstance(report.get('target'), dict)
                or not isinstance(report.get('runtime'), dict)
                or not isinstance(report.get('findings'), list)):
            raise NodeError('INVALID_RESPONSE', 'Unsupported or malformed doctor report')
        _validate_doctor_report(report)
        selected = report['target']
        if any(selected.get(key) != value[key] for key in ('node_id', 'queue_id')):
            raise NodeError('INVALID_RESPONSE', 'Doctor report belongs to another node or queue')
        if selected.get('job_id') is not None:
            target_identity({key: selected.get(key) for key in ('node_id', 'queue_id', 'job_id', 'attempt_id')})
        if report['runtime'].get('identity') != value['runtime']:
            raise NodeError('INVALID_RESPONSE', 'Doctor runtime contradicts its endpoint envelope')
    if operation == 'status':
        state = result.get('state')
        if not isinstance(state, dict) or not isinstance(state.get('jobs'), list) or not isinstance(state.get('controller'), dict):
            raise NodeError('INVALID_RESPONSE', 'Invalid status payload')
        if len(state['jobs']) > 1000:
            raise NodeError('INVALID_RESPONSE', 'Status exceeds its row bound')
        counts = result.get('status_counts')
        if counts is not None:
            from cmw.jobs.catalogue_models import STATUSES
            if (type(counts) is not dict or set(counts) - STATUSES or
                    any(type(count) is not int or count < 0 for count in counts.values()) or
                    type(result.get('job_count')) is not int or sum(counts.values()) != result['job_count']):
                raise NodeError('INVALID_RESPONSE', 'Invalid unfiltered operational counts')
        for job in state['jobs']:
            if not isinstance(job, dict) or not isinstance(job.get('name'), str) or not isinstance(job.get('status'), str):
                raise NodeError('INVALID_RESPONSE', 'Invalid managed job row')
            target = target_identity(job.get('target'))
            if target['node_id'] != value['node_id'] or target['queue_id'] != value['queue_id']:
                raise NodeError('INVALID_RESPONSE', 'Status row belongs to another node or queue')
            if job.get('id') != target['job_id'] or job.get('attempt_id') != target['attempt_id']:
                raise NodeError('INVALID_RESPONSE', 'Status row identity contradicts its target')
    if operation and operation.startswith('study.'):
        from .study import validate_result
        validate_result(value, operation)
    if operation and operation.startswith('successor.'):
        from .successor import validate_result
        validate_result(value, operation)
    if operation and operation.startswith('catalogue.'):
        from .catalogue import validate_result
        validate_result(value, operation)
    if operation in {'review.preview', 'review.start', 'review.poll', 'review.cancel'}:
        from .codec import decode_typed
        from cmw.jobs.review_models import ReviewRecord
        try:
            if set(result) != {'review'}:
                raise ValueError('Unexpected review fields')
            review = decode_typed(result['review'], ReviewRecord)
            if (review.node_id, review.queue_id) != (value['node_id'], value['queue_id']):
                raise ValueError('Review belongs to another node or queue')
        except (KeyError, ValueError, TypeError) as exc:
            raise NodeError('INVALID_RESPONSE', 'Malformed or misbound result review payload') from exc
    if operation in {'progress', 'history'}:
        from .codec import decode_typed
        from cmw.progress.models import ProgressSnapshot, HistoryPage
        try:
            decode_typed(result['snapshot'], ProgressSnapshot)
            if operation == 'history':
                decode_typed(result['page'], HistoryPage)
        except (KeyError, ValueError, TypeError) as exc:
            raise NodeError('INVALID_RESPONSE', 'Malformed scientific observation payload') from exc
    return value


def _validate_doctor_report(report):
    """Doctor schema is negotiated independently of scientific dataclass codecs."""
    fields = {'scope', 'worker', 'capabilities', 'observation', 'scientific_policy'}
    if (not fields <= set(report) or not isinstance(report['scope'], str)
            or report['scope'] not in {'local', 'job', 'node'}
            or report['scientific_policy'] != 'NOT_ASSESSED'):
        raise NodeError('INVALID_RESPONSE', 'Doctor report lacks its scope or policy qualification')
    if 'bytecode_policy' in report['runtime']:
        policy = report['runtime']['bytecode_policy']
        if (not isinstance(policy, dict) or type(policy.get('isolated')) is not int
                or policy['isolated'] not in (0, 1) or type(policy.get('startup_dont_write_bytecode')) is not int
                or policy['startup_dont_write_bytecode'] not in (0, 1) or type(policy.get('dont_write_bytecode')) is not bool
                or policy.get('launch_flags') not in ([], ['-B'], ['-I'], ['-B', '-I'])
                or policy.get('owner_cache_access') not in ('UNKNOWN', 'WRITABLE', 'READ_ONLY')):
            raise NodeError('INVALID_RESPONSE', 'Malformed observer bytecode policy')
    worker = report['worker']
    if (not isinstance(worker, dict) or not {'status', 'runtime'} <= set(worker)
            or not isinstance(worker['status'], str) or worker['status'] not in {'UNKNOWN', 'RECORDED'}
            or not (worker['runtime'] is None or isinstance(worker['runtime'], dict))
            or worker['status'] == 'UNKNOWN' and worker['runtime'] is not None
            or worker['status'] == 'RECORDED' and worker['runtime'] is None):
        raise NodeError('INVALID_RESPONSE', 'Malformed recorded worker runtime evidence')
    if worker['runtime'] is not None:
        from .identity import _validate_runtime
        try:
            _validate_runtime(worker['runtime'])
        except (ValueError, TypeError) as exc:
            raise NodeError('INVALID_RESPONSE', 'Malformed recorded worker implementation identity') from exc
    capabilities = report['capabilities']
    if not isinstance(capabilities, list) or len(capabilities) > 64:
        raise NodeError('INVALID_RESPONSE', 'Malformed doctor capability list')
    for capability in capabilities:
        if (not isinstance(capability, dict) or not {'name', 'status', 'qualification'} <= set(capability)
                or any(not isinstance(capability[key], str) for key in ('name', 'status', 'qualification'))):
            raise NodeError('INVALID_RESPONSE', 'Malformed doctor capability')
    observation = report['observation']
    required = {'status', 'observed_at', 'coverage', 'transport', 'progress', 'binding', 'controller'}
    if (not isinstance(observation, dict) or not required <= set(observation)
            or not isinstance(observation['status'], str) or not isinstance(observation['coverage'], str)
            or not isinstance(observation['transport'], dict)
            or any(observation[key] is not None and not isinstance(observation[key], dict)
                   for key in ('progress', 'binding', 'controller'))):
        raise NodeError('INVALID_RESPONSE', 'Malformed doctor observation qualifications')
    timestamp = observation['observed_at']
    if timestamp is not None and (type(timestamp) not in (int, float) or not math.isfinite(timestamp)):
        raise NodeError('INVALID_RESPONSE', 'Invalid doctor observation timestamp')
    fields = {'code', 'domain', 'scope', 'severity', 'summary', 'evidence', 'next_check', 'capability', 'caused_by'}
    if len(report['findings']) > 128:
        raise NodeError('INVALID_RESPONSE', 'Doctor findings exceed the bounded report limit')
    for finding in report['findings']:
        if (not isinstance(finding, dict) or set(finding) != fields
                or any(not isinstance(finding[key], str) for key in (
                    'code', 'domain', 'scope', 'severity', 'summary', 'next_check'))
                or finding['severity'] not in {'INFO', 'WARNING', 'ERROR'}
                or not isinstance(finding['evidence'], dict)
                or any(finding[key] is not None and not isinstance(finding[key], str)
                       for key in ('capability', 'caused_by'))):
            raise NodeError('INVALID_RESPONSE', 'Malformed actionable doctor finding')
