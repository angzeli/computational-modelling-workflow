"""Explicit selected-node diagnostics through the existing read-only endpoint.

The doctor operation is its own schema negotiation. Older endpoints reject it
before constructing a backend; probing ordinary capabilities first would run
unrelated execution-profile checks on those older endpoints.
"""
from __future__ import annotations

from copy import deepcopy
import time

from cmw.nodes.connections import connect
from cmw.nodes.protocol import NodeError, target_identity


NODE_FINDING_GUIDANCE = {
    'NODE_CONFIGURATION_UNAVAILABLE': 'Inspect the selected node enrollment and its fixed endpoint command; do not change it automatically.',
    'RUNTIME_MISMATCH': 'Compare the selected endpoint implementation with its enrolled pin and queue compatibility; retain active worker runtimes.',
    'INCOMPATIBLE_PROTOCOL': 'Inspect the selected endpoint supported protocol without switching an active runtime.',
    'UNSUPPORTED_OPERATION': 'Inspect the selected endpoint installation for doctor schema 1 support; establish queue compatibility before any runtime switch.',
    'ENDPOINT_CHANGED': 'Read the selected node, host and queue identities; explicitly reselect only after establishing ownership.',
    'IDENTITY_CHANGED': 'Read the selected node, host and queue identities; explicitly reselect only after establishing ownership.',
    'WRONG_NODE': 'Inspect the captured node and queue target; do not resolve the same display ID on another node.',
    'WRONG_TARGET': 'Inspect the captured node and queue target; do not resolve the same display ID on another node.',
    'ATTEMPT_CHANGED': 'Reselect the exact immutable attempt after reading the selected owning queue.',
    'QUEUE_CHANGED': 'Inspect the owning endpoint queue identity and enrollment read-only; do not initialize or migrate the queue.',
    'INVALID_QUEUE': 'Inspect the selected enrolled queue location on its owning endpoint.',
    'QUEUE_SCHEMA_UNSUPPORTED': 'Inspect the queue using its recorded compatible runtime; do not migrate or point older workers at it.',
    'NODE_REFUSED': 'Read the selected endpoint diagnostic and queue schema without changing configuration or state.',
    'REMOTE_OBSERVATION_CACHED': 'Retry this explicit selected-node doctor to obtain fresh owning-node evidence.',
    'FRONTEND_ENDPOINT_RUNTIME_DIFFER': 'Compare supported capabilities on each runtime; a difference alone does not establish worker incompatibility.',
    'UNREACHABLE': 'Check connectivity to the explicitly enrolled endpoint with the fixed read-only doctor route.',
    'DISCONNECTED': 'Retry the selected read-only endpoint observation; retain last-reported timestamp qualifications.',
    'INVALID_RESPONSE': 'Inspect the selected endpoint protocol and target identity; discard the rejected response.',
    'UNKNOWN_NODE': 'Read the explicit frontend node registry and select one enrolled alias.',
    'UNKNOWN_QUEUE': 'Read the selected node enrollment and choose one enrolled queue alias.',
    'UNSAFE_CONFIGURATION': 'Inspect the selected configuration ownership and mode; diagnostics do not change permissions.',
    'INVALID_CONNECTION': 'Inspect the explicit selected endpoint connection fields without executing replacement commands.',
    'INVALID_PARAMS': 'Review the selected capability, bounded job identifier and immutable target.',
}


def _finding(code, domain, summary, evidence, *, severity='ERROR'):
    return {'code': code, 'domain': domain, 'scope': 'endpoint', 'severity': severity,
            'summary': summary, 'evidence': evidence,
            'next_check': NODE_FINDING_GUIDANCE.get(code, NODE_FINDING_GUIDANCE['NODE_CONFIGURATION_UNAVAILABLE']),
            'capability': 'doctor', 'caused_by': None}


def _base(alias, queue, capability):
    from .service import inspect_local
    report = inspect_local()
    report['caller_runtime'] = deepcopy(report['runtime'])
    report['caller_capabilities'] = report['capabilities']
    report['capabilities'] = []
    report['endpoint_runtime'] = None
    report['scope'] = 'node'
    report['target'] = {'node_alias': alias, 'queue_alias': queue, 'node_id': None,
                        'queue_id': None, 'job_id': None, 'attempt_id': None}
    report['selected_capability'] = capability
    for finding in report['findings']:
        finding['scope'] = 'caller'
    return report


def _registration(backend, endpoint_runtime=None):
    # Registry schema 1 records a fixed executable path for SSH. Observe its
    # resulting interpreter flags on the owner; never read or execute a wrapper.
    policy = (endpoint_runtime or {}).get('bytecode_policy') or {}
    entry = getattr(backend, 'connection', {})
    return {'path': entry.get('executable'), 'transport': entry.get('transport'),
            'launch_flags': policy.get('launch_flags')}


def _refusal(report, exc, cached=None):
    code = getattr(exc, 'code', 'NODE_CONFIGURATION_UNAVAILABLE')
    if 'Unsupported Jobs state schema' in str(exc):
        code = 'QUEUE_SCHEMA_UNSUPPORTED'
    domain = 'transport'
    if code in {'RUNTIME_MISMATCH', 'INCOMPATIBLE_PROTOCOL', 'UNSUPPORTED_OPERATION'}:
        domain = 'runtime'
    elif code in {'ENDPOINT_CHANGED', 'IDENTITY_CHANGED', 'WRONG_NODE', 'WRONG_TARGET', 'ATTEMPT_CHANGED'}:
        domain = 'identity'
    elif code in {'QUEUE_CHANGED', 'INVALID_QUEUE', 'NODE_REFUSED', 'QUEUE_SCHEMA_UNSUPPORTED'}:
        domain = 'storage'
    report['findings'].append(_finding(code, domain, 'Selected endpoint diagnosis is unavailable.',
                                      {'error': str(exc)[:4096]}))
    observation = report.setdefault('observation', {})
    observation.update(status='UNAVAILABLE', observed_at=None,
                       transport={'status': 'unavailable', 'cached': False})
    if cached:
        observation['status'] = 'CACHED'
        observation['transport'] = {
            'status': 'disconnected', 'cached': True,
            'last_reported': {key: cached.get(key) for key in (
                'node_id', 'queue_id', 'session_id', 'sequence', 'observed_at', 'clock_id')},
            'qualification': 'Last verified envelope only; no fresh endpoint or worker observation. Receipt age is unknown.'}
        report['endpoint_runtime'] = {'identity': deepcopy(cached.get('runtime')),
                                      'qualification': 'LAST_REPORTED'}
        report['findings'].append(_finding('REMOTE_OBSERVATION_CACHED', 'transport',
            'Only previously verified endpoint evidence is available.',
            observation['transport']['last_reported'], severity='WARNING'))
    return report


def inspect_connected(backend, *, target=None, job_id=None, capability=None):
    """Inspect one existing connection, retaining cached provenance on refusal.

    The caller owns and closes an existing backend. A supplied target is never
    resolved again from a display ID, even if the selected job has changed.
    """
    report = _base(backend.alias, backend.queue, capability)
    report['target'].update(node_id=backend.node_id, queue_id=backend.queue_id)
    if target is not None:
        report['target'].update(deepcopy(target_identity(target)))
    report['target']['requested_job'] = job_id
    report['registered_executable'] = _registration(backend)
    cached = deepcopy(backend.last_envelope)
    params = {}
    if job_id is not None:
        params['job_id'] = job_id
    if capability is not None:
        params['capability'] = capability
    started = time.monotonic()
    try:
        envelope = backend.call('doctor.inspect', params, target=target)
        return compose_report(backend, envelope, target=target,
            request_seconds=max(0., time.monotonic() - started), caller_report=report)
    except (NodeError, OSError, ValueError, RuntimeError) as exc:
        return _refusal(report, exc, cached)


def compose_refusal(backend, error, *, target=None, cached=None, capability=None):
    """Compose a failed serial-lane request without retrying or reopening it."""
    report = _base(backend.alias, backend.queue, capability)
    report['registered_executable'] = _registration(backend)
    report['target'].update(node_id=backend.node_id, queue_id=backend.queue_id)
    if target is not None:
        report['target'].update(deepcopy(target_identity(target)))
    if cached is not None and (cached.get('node_id') != backend.node_id
                               or cached.get('queue_id') != backend.queue_id):
        cached = None
    return _refusal(report, error, deepcopy(cached))


def compose_report(backend, envelope, *, target=None, request_seconds=0., received_at=None, caller_report=None):
    """Compose a captured verified reply without opening or calling a connection."""
    from cmw.nodes.protocol import validate_envelope
    validate_envelope(envelope, 'doctor.inspect')
    if envelope['node_id'] != backend.node_id or envelope['queue_id'] != backend.queue_id:
        raise NodeError('WRONG_NODE', 'Doctor reply does not belong to the selected connection')
    host = envelope['host']
    if str(host.get('platform')) + ':' + str(host.get('hardware_id')) != backend.connection['host_id']:
        raise NodeError('ENDPOINT_CHANGED', 'Doctor reply does not match the pinned host')
    if envelope['runtime'].get('fingerprint') != backend.connection['runtime_id']:
        raise NodeError('RUNTIME_MISMATCH', 'Doctor reply does not match the pinned endpoint runtime')
    endpoint = deepcopy(envelope['result']['report'])
    selected = endpoint['target']
    if target is not None:
        target_identity(target)
        if any(selected.get(key) != value for key, value in target.items()):
            raise NodeError('INVALID_RESPONSE', 'Doctor response changed the selected immutable target')
    report = caller_report if caller_report is not None else _base(backend.alias, backend.queue, None)
    endpoint['registered_executable'] = _registration(backend, endpoint['runtime'])
    endpoint['endpoint_runtime'] = endpoint['runtime']
    endpoint['runtime'] = report['runtime']
    endpoint['caller_runtime'] = report['caller_runtime']
    endpoint['caller_capabilities'] = report['caller_capabilities']
    endpoint['scope'] = 'node'
    endpoint['target'].update(node_alias=backend.alias, queue_alias=backend.queue)
    endpoint['observation']['transport'] = {
        'status': 'fresh', 'cached': False, 'request_seconds': max(0., request_seconds),
        'received_at': time.time() if received_at is None else received_at,
        **{key: envelope[key] for key in ('node_id', 'queue_id', 'session_id', 'sequence', 'observed_at', 'clock_id')},
        'qualification': 'Observation timestamps belong to the endpoint clock; receipt does not refresh source evidence.'}
    for finding in endpoint['findings']:
        finding['scope'] = 'endpoint'
    endpoint['findings'] = report['findings'] + endpoint['findings']
    caller_id = (report['runtime'].get('identity') or {}).get('fingerprint')
    endpoint_id = (endpoint['endpoint_runtime'].get('identity') or {}).get('fingerprint')
    if caller_id and endpoint_id and caller_id != endpoint_id:
        endpoint['findings'].append(_finding('FRONTEND_ENDPOINT_RUNTIME_DIFFER', 'runtime',
            'Frontend and selected endpoint load different implementations.',
            {'caller_fingerprint': caller_id, 'endpoint_fingerprint': endpoint_id},
            severity='INFO'))
    return endpoint


def inspect_node(alias, queue=None, *, nodes_config=None, job_id=None, capability=None):
    """Connect only to the explicitly selected enrolled node and queue."""
    try:
        backend = connect(alias, root=nodes_config, queue=queue)
    except (NodeError, OSError, ValueError, RuntimeError) as exc:
        return _refusal(_base(alias, queue, capability), exc)
    try:
        return inspect_connected(backend, job_id=job_id, capability=capability)
    finally:
        backend.close()
