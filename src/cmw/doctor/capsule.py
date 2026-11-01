"""Explicit, bounded exports of allowlisted doctor facts; never a file collector."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterator

from cmw.jobs.snapshot import HandoffSnapshot


DEFAULT_FILE_LIMIT = 256 * 1024
DEFAULT_TOTAL_LIMIT = 1024 * 1024
MAX_ITEMS = 256
MAX_TEXT = 4096
_EXCLUSIONS = (
    'Environment, credentials, SSH material and complete command arguments',
    'Raw logs, arbitrary text excerpts, coordinates and scientific payloads',
    'POTCAR, GBW, WAVECAR, CHGCAR and complete databases',
    'Unknown report fields and unrecognized nested evidence',
    'Archive contents; archives are never opened or extracted',
    'Original paths, target identifiers and free-form diagnostic text',
)
_QUALIFICATIONS = (
    'Private allowlisted diagnostic snapshot; not a full replay corpus.',
    'Component timestamps and coverage are retained; capture is not atomic.',
    'Aliases retain equality within this bundle and are not reversible here.',
    'No raw excerpt selection or import is supported by this exporter.',
    'Allowlists and redaction do not prove arbitrary user-selected text secret-free.',
    'Scientific acceptance is not assessed.',
)
_STATUS = frozenset(('OK', 'PASS', 'FAIL', 'UNKNOWN', 'UNAVAILABLE', 'AVAILABLE', 'MISSING',
    'PRESENT', 'SUPPORTED', 'UNSUPPORTED', 'INCOMPATIBLE', 'MISMATCH', 'MATCH', 'PENDING',
    'EMPTY', 'PARTIAL', 'COMPLETE', 'STALE', 'CACHED', 'FRESH', 'RECORDED', 'UNINSTALLED',
    'SOURCE_ONLY', 'INSTALLED', 'READABLE', 'UNREADABLE', 'NOT_ASSESSED', 'NOT_SELECTED',
    'NOT_CONFIGURED', 'NOT_REQUESTED', 'NOT_PROBED', 'OBSERVED', 'LOCAL',
    'UNDECLARED', 'SOURCE_LOCAL', 'NOT_YET_PARSED', 'MALFORMED', 'UNRESOLVED',
    'UNPARSED', 'PARTIALLY_MATCHED', 'UNBOUND', 'MATCHED', 'DIRTY_SOURCE', 'EXACT_CHECKOUT', 'UNCHANGED', 'SOURCE_CONFLICT',
    'UNSUPPORTED_DIALECT', 'UNSUPPORTED_MODE', 'ADAPTER_ERROR', 'ADAPTER_UNAVAILABLE',
    'fresh', 'unavailable', 'disconnected', 'OFFLINE', 'ONLINE', 'STOPPED', 'RUNNING', 'ABSENT', 'DECLARED',
    'UNVERIFIED', 'OBSERVATIONAL', 'BOUND', 'CONFLICT', 'NOT_CREATED', 'READ_ONLY',
    'Run', 'Queue', 'Hold', 'Done', 'Fail', 'Cancelled', 'Unknown', 'Suspended'))
_DOMAINS = frozenset(('runtime', 'transport', 'controller', 'ownership', 'storage', 'binding',
    'parser', 'scientific_policy', 'capability', 'dependency', 'provider', 'protocol', 'source', 'projection', 'identity'))
_CAPABILITIES = frozenset(('core', 'jobs', 'jobs-ui', 'watch', 'progress', 'vasp', 'orca',
    'multiwfn', 'thermal', 'nodes', 'files', 'suspend', 'continue', 'doctor', 'capsule',
    'vasp-progress', 'orca-progress', 'multiwfn-progress', 'remote-observation',
    'progress:VASP', 'progress:ORCA', 'progress:MULTIWFN', 'vasp-runner',
    'scientific preparation', 'structure import', 'scientific adapters', 'configuration'))
_DEPENDENCIES = frozenset(('computational-modelling-workflow', 'ase', 'gemmi', 'numpy',
    'PyYAML', 'psutil', 'textual', 'rich', 'macmon'))
_SOURCE_ROLES = frozenset(('stdout', 'stderr', 'outcar', 'oszicar', 'poscar', 'input',
    'output', 'runner', 'stage', 'workflow', 'source', 'manifest', 'incar',
    'orca_output', 'multiwfn_session', 'multiwfn_events', 'multiwfn_validation', 'multiwfn_metadata'))

_ASSETS = frozenset(('runtime_python.sh', 'multiwfn/multiwfn_runtime.sh',
    'workflows/multiwfn_cube_workflow.sh', 'orca/run_orca.sh', 'workflows/run_opt_freq_sp.sh', 'multiwfn/run_ifct.sh',
    'workflows/generate_homo_lumo_cubes.sh', 'workflows/generate_esp_cubes.sh',
    'workflows/generate_igmh_cubes.sh'))
_FIXED_CONTEXT = frozenset((
    'Read-only observation, not execution readiness or scientific acceptance. Component observations are bounded and not an atomic snapshot.',
    'Implementation present; configuration and scientific grammar are separate.',
    'One bounded poll of explicitly selected sources; no log corpus or full replay.',
    'Current launcher and observer imports do not identify an existing worker.',
    'Current launcher and observer imports do not identify an existing worker. Submission runtime is an expectation, not observed worker imports.',
    'Last verified envelope only; no fresh endpoint or worker observation. Receipt age is unknown.',
    'Observation timestamps belong to the endpoint clock; receipt does not refresh source evidence.',
    'LAST_REPORTED'))

_COMMANDS = frozenset(('cleanup-attempts', 'migrate-execution-layout', 'execution-layout-status',
    'jobs', 'nodes', 'vasp', 'structure', 'doctor'))
_GRAMMARS = {
    'VASP': ('6.6.1 ordinary static/fixed-cell; bounded DAV/RMM and force tables',),
    'ORCA': ('6.1.x demonstrated DIIS/SOSCF and labelled geometry tables',),
    'MULTIWFN': ('2026.7.15 FMO/ESP/IGMH/IFCT bounded native and sequenced runner markers',)}

# Shape is deliberately independent of arbitrary dictionaries inside a snapshot.
_IDENTITY = {'algorithm': ('cmw-runtime-sha256-v1',), 'package_sha256': 'hash',
    'fingerprint': 'hash', 'version': 'version', 'python': 'python',
    'package_origin': 'path', 'executable': 'path',
    'dependencies': {name: 'version' for name in _DEPENDENCIES}}
_RUNTIME = {'identity': _IDENTITY,
    'distribution': {'name': _DEPENDENCIES, 'version': 'version', 'status': _STATUS},
    'origins': {'package': 'path', 'cli': 'path', 'executable': 'path'},
    'build_source': {'status': _STATUS, 'commit': 'commit', 'dirty': 'boolean', 'root': 'path'},
    'qualification': 'context', 'identity_error': 'context',
    'assets': [{'name': _ASSETS, 'status': _STATUS, 'path': 'path'}],
    'protocols': {'node': 'number', 'jobs': ['number'], 'cleanup_tool': ('cmw-attempt-cleanup/1',), 'doctor': 'number'},
    'commands': [_COMMANDS], 'scientific_grammars': _GRAMMARS,
    'dependencies': [{'name': _DEPENDENCIES, 'version': 'version',
                      'required_for': _CAPABILITIES, 'status': _STATUS}]}
_TARGET = {'state': 'path', 'node_id': 'identity', 'queue_id': 'identity',
    'job_id': 'identity', 'attempt_id': 'identity', 'engine': ('VASP', 'ORCA', 'Multiwfn', 'MULTIWFN'),
    'status': _STATUS, 'queue_schema': 'number', 'node_alias': 'identity',
    'queue_alias': 'identity', 'requested_job': 'identity'}
_SOURCE = {'role': _SOURCE_ROLES, 'path': 'path', 'identity': 'identity', 'status': _STATUS,
    'read_health': _STATUS, 'health': _STATUS, 'generation': 'number', 'offset': 'number',
    'has_more': 'boolean', 'last_change_at': 'number', 'size': 'number', 'bytes_read': 'number', 'total_bytes_read': 'number',
    'parsed_lines': 'number', 'complete_rows': 'number', 'usable_rows': 'number',
    'last_complete_row_at': 'number', 'last_attempt_at': 'number', 'last_success_at': 'number', 'last_growth_at': 'number',
    'observed_at': 'number', 'age_seconds': 'number', 'stale': 'boolean', 'reason': 'context'}
_COVERAGE = {'status': _STATUS, 'complete': 'boolean', 'partial': 'boolean',
    'bounded': 'boolean', 'bytes_read': 'number', 'limit_bytes': 'number',
    'source_count': 'number', 'observed_at': 'number', 'reason': 'context'}
_EVIDENCE = {**_TARGET, **_SOURCE, 'expected': 'identity', 'actual': 'identity',
    'declared_runtime': 'hash', 'observer_runtime': 'hash', 'caller_fingerprint': 'hash',
    'endpoint_fingerprint': 'hash', 'expected_fingerprint': 'hash', 'actual_fingerprint': 'hash', 'fingerprint': 'hash',
    'version': 'version', 'expected_version': 'version', 'actual_version': 'version',
    'schema': 'number', 'supported_schemas': ['number'], 'available': 'boolean',
    'name': _DEPENDENCIES | _ASSETS, 'required_for': _CAPABILITIES, 'error_type': 'context',
    'binding_id': 'identity', 'generation': 'number', 'source_count': 'number', 'declared_count': 'number',
    'adapter': 'context', 'metric_count': 'number', 'group_count': 'number',
    'error': 'context', 'detail': 'context', 'qualification': 'context', 'coverage': _COVERAGE}
_ENVELOPE = {'node_id': 'identity', 'queue_id': 'identity', 'session_id': 'identity',
    'sequence': 'number', 'observed_at': 'number', 'clock_id': 'identity'}
_SCOPES = ('local', 'node', 'job', 'capability', 'caller', 'endpoint')
_REPORT = {'schema': 'number', 'scope': _SCOPES, 'captured_at': 'number',
    'qualification': 'context', 'runtime': _RUNTIME, 'caller_runtime': _RUNTIME, 'endpoint_runtime': _RUNTIME,
    'worker': {'status': _STATUS, 'runtime': _IDENTITY, 'qualification': 'context', 'expected_runtime_id': 'hash'},
    'target': _TARGET, 'selected_capability': _CAPABILITIES,
    'capabilities': [{'name': _CAPABILITIES, 'status': _STATUS, 'qualification': 'context'}],
    'caller_capabilities': [{'name': _CAPABILITIES, 'status': _STATUS, 'qualification': 'context'}],
    'findings': [{'code': 'code', 'domain': _DOMAINS, 'scope': _SCOPES,
        'severity': ('info', 'warning', 'error', 'INFO', 'WARNING', 'ERROR'),
        'summary': 'context', 'evidence': _EVIDENCE, 'next_check': 'context',
        'capability': _CAPABILITIES, 'caused_by': 'code'}],
    'observation': {'status': _STATUS, 'observed_at': 'number', 'coverage': _COVERAGE,
        'transport': {**_ENVELOPE, 'status': _STATUS, 'observed_at': 'number', 'age_seconds': 'number',
            'last_reported': _ENVELOPE, 'qualification': 'context', 'request_seconds': 'number', 'received_at': 'number',
            'stale': 'boolean', 'cached': 'boolean', 'node_id': 'identity', 'reason': 'context'},
        'progress': {'status': _STATUS, 'read_health': _STATUS, 'completeness': _STATUS,
            'adapter': 'context', 'observed_at': 'number', 'last_success_at': 'number',
            'last_observation_at': 'number', 'last_new_evidence_at': 'number',
            'last_source_change_at': 'number', 'last_file_growth_at': 'number',
            'sources': [_SOURCE], 'source_diagnostics': [_SOURCE], 'coverage': _COVERAGE,
            'metric_ids': ['context'], 'projection_group_count': 'number'},
        'binding': {'status': _STATUS, 'sources': [_SOURCE], 'reason': 'context',
                    'binding_id': 'identity', 'generation': 'number', 'source_count': 'number', 'declared_count': 'number',
                    'state_identity': 'identity', 'job_id': 'identity', 'attempt_id': 'identity'},
        'controller': {'status': _STATUS, 'online': 'boolean', 'ownership': _STATUS,
                       'dispatch': 'boolean', 'heartbeat': 'number', 'owner_recorded': 'boolean',
                       'observed_at': 'number', 'reason': 'context'}},
    'scientific_policy': ('NOT_ASSESSED',)}


class _Projection:
    def __init__(self, guidance: dict[str, str | tuple[str, ...]]) -> None:
        self.guidance = guidance
        self.aliases: dict[str, str] = {}
        self.omitted = 0

    def alias(self, value: str, kind: str) -> str:
        if kind == 'context' and value in _FIXED_CONTEXT:
            return value
        if len(value) > MAX_TEXT:
            raise ValueError('Doctor text exceeds the capsule projection limit')
        if value not in self.aliases:
            self.aliases[value] = f'<{kind} {len(self.aliases) + 1}>'
        return self.aliases[value]

    def value(self, value: Any, spec: Any) -> Any:
        if value is None:
            return None
        if isinstance(spec, dict):
            if not isinstance(value, dict):
                return self.alias(value, 'context') if isinstance(value, str) else None
            if len(value) > MAX_ITEMS:
                raise ValueError('Doctor mapping exceeds the capsule projection limit')
            self.omitted += len(value.keys() - spec.keys())
            result = {key: self.value(value[key], child) for key, child in spec.items() if key in value}
            code = value.get('code')
            if 'next_check' in spec and isinstance(code, str) and code in self.guidance:
                trusted = self.guidance[code]
                choices = (trusted,) if isinstance(trusted, str) else trusted
                if value.get('next_check') in choices:
                    result['next_check'] = value['next_check']
            return result
        if isinstance(spec, list):
            if not isinstance(value, (list, tuple)):
                return None
            if len(value) > MAX_ITEMS:
                raise ValueError('Doctor collection exceeds the capsule projection limit')
            return [self.value(item, spec[0]) for item in value]
        if isinstance(spec, (tuple, frozenset)):
            return value if isinstance(value, str) and value in spec else (
                self.alias(value, 'context') if isinstance(value, str) else None)
        if spec == 'number':
            return value if type(value) in (int, float) and abs(value) <= 1e30 and math.isfinite(value) else None
        if spec == 'boolean':
            return value if type(value) is bool else None
        if spec == 'identity' and type(value) is int and 0 <= value < 2 ** 64:
            return self.alias(str(value), 'identity')
        if spec == 'identity' and isinstance(value, (tuple, list)) and len(value) <= 8 and all(type(item) is int and abs(item) < 2 ** 64 for item in value):
            return self.alias(json.dumps(value), 'identity')
        if not isinstance(value, str):
            return None
        if len(value) > MAX_TEXT:
            raise ValueError('Doctor text exceeds the capsule projection limit')
        if spec == 'hash' and re.fullmatch(r'[0-9a-f]{64}', value):
            return value
        if spec == 'commit' and re.fullmatch(r'[0-9a-f]{40}', value):
            return value
        if spec == 'version' and value in {'uninstalled', 'unavailable'}:
            return value
        if spec == 'version' and re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}(?:(?:a|b|rc|post|dev)[0-9]+)?', value):
            return value
        if spec == 'python':
            match = re.match(r'^([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)', value)
            if match:
                return match[1]
        if spec == 'code' and value in self.guidance:
            return value
        return self.alias(value, spec if spec in {'identity', 'path'} else 'context')


def _finding_guidance() -> dict[str, str | tuple[str, ...]]:
    # These mappings are code-owned prose, never supplied by a report or peer.
    from .service import FINDING_GUIDANCE
    from .nodes import NODE_FINDING_GUIDANCE
    merged: dict[str, str | tuple[str, ...]] = dict(FINDING_GUIDANCE)
    for code, guidance in NODE_FINDING_GUIDANCE.items():
        previous = merged.get(code)
        merged[code] = (previous, guidance) if isinstance(previous, str) and previous != guidance else guidance
    return merged


def project_capsule(report: dict[str, Any]) -> HandoffSnapshot:
    """Freeze explicit safe fields without reading paths or writing any files."""
    if not isinstance(report, dict) or type(report.get('schema')) is not int or report['schema'] != 1:
        raise ValueError('Unsupported doctor report schema')
    projection = _Projection(_finding_guidance())
    payload = projection.value(report, _REPORT)
    payload['capsule_projection'] = {'schema': 1, 'aliases': len(projection.aliases),
        'omitted_fields': projection.omitted, 'exclusions': list(_EXCLUSIONS),
        'qualifications': list(_QUALIFICATIONS)}
    return HandoffSnapshot(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))


def _destination_parts(destination: str | os.PathLike[str]) -> tuple[str, ...]:
    raw = os.fspath(destination)
    if not isinstance(raw, str) or not raw.startswith('/') or any(
            part in {'.', '..'} for part in raw.split('/')):
        raise ValueError('Capsule destination must be absolute without traversal components')
    path = Path(raw)
    desktop = Path.home() / 'Desktop'
    if not (desktop in path.parents or any(part == 'Scratch' for part in path.parts[:-1])):
        raise ValueError('Capsule destination must be a new directory under Desktop or Scratch')
    return path.parts[1:]


@contextmanager
def _parent_directory(parts: tuple[str, ...]) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('/', flags)
    try:
        for component in parts[:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def export_capsule(report: dict[str, Any], destination: str | os.PathLike[str], *,
                   per_file_limit: int = DEFAULT_FILE_LIMIT,
                   total_limit: int = DEFAULT_TOTAL_LIMIT) -> dict[str, Any]:
    """Create a private, no-clobber directory of bounded diagnostic JSON only.

    Existing parent directories must be real directories. No report path is read,
    and no arbitrary file or archive can be selected for inclusion.
    """
    for value, maximum in ((per_file_limit, DEFAULT_FILE_LIMIT), (total_limit, DEFAULT_TOTAL_LIMIT)):
        if type(value) is not int or value <= 0 or value > maximum:
            raise ValueError('Capsule limits must be positive and within the built-in maximum')
    parts = _destination_parts(destination)
    snapshot = project_capsule(report)
    data = (json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False, allow_nan=False,
                       sort_keys=True) + '\n').encode('utf-8')
    manifest = {'schema': 1, 'kind': 'cmw-private-support-capsule',
        'files': [{'name': 'doctor.json', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}],
        'limits': {'per_file_bytes': per_file_limit, 'total_bytes': total_limit},
        'exclusions': list(_EXCLUSIONS), 'qualifications': list(_QUALIFICATIONS),
        'hash_scope': 'SHA-256 covers the exact included doctor.json bytes; manifest cannot hash itself.'}
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8')
    files = (('doctor.json', data), ('manifest.json', manifest_data))
    if any(len(content) > per_file_limit for _, content in files) or sum(len(content) for _, content in files) > total_limit:
        raise ValueError('Capsule exceeds its per-file or total byte limit')
    with _parent_directory(parts) as parent:
        os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
        descriptor = None
        created = []
        try:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.fchmod(descriptor, 0o700)
            for name, content in files:
                file_descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o600, dir_fd=descriptor)
                created.append(name)
                with os.fdopen(file_descriptor, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(content)
            return manifest
        except BaseException:
            if descriptor is not None:
                for name in created:
                    os.unlink(name, dir_fd=descriptor)
            os.rmdir(parts[-1], dir_fd=parent)
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
