"""Strict, standard-library wire models for the proposed multi-host protocol.

These models do not migrate the existing ``cmw.nodes`` protocol. Structural
objects are closed. Capability names may be omitted (unsupported), and unknown
capabilities are retained as inert data for same-major feature negotiation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
import json
import math
import re
from typing import Any, ClassVar


class ContractError(ValueError):
    """Malformed or rejected contract data; messages never include input payloads."""

    def __init__(self, message: str, *, code: str = 'invalid_request'):
        super().__init__(message)
        self.code = code


class UnsupportedVersionError(ContractError):
    def __init__(self, version: str):
        super().__init__('Unsupported contract MAJOR version', code='protocol_incompatible')
        self.version = version


class CancelMode(str, Enum):
    GRACEFUL = 'graceful'
    SOFT = 'soft'
    HARD = 'hard'


class ErrorCode(str, Enum):
    INVALID_REQUEST = 'invalid_request'
    PROTOCOL_INCOMPATIBLE = 'protocol_incompatible'
    CAPABILITY_UNSUPPORTED = 'capability_unsupported'
    NOT_FOUND = 'not_found'
    CONFLICT = 'conflict'
    VALIDATION_FAILED = 'validation_failed'
    INTERNAL_ERROR = 'internal_error'


ERROR_EXIT_CODES = dict(zip(ErrorCode, (2, 10, 11, 20, 21, 22, 40)))
COMMANDS = ('hello', 'snapshot', 'receive', 'submit', 'cancel', 'tail')
INPUT_PATHS = ('inputs/INCAR', 'inputs/KPOINTS', 'inputs/POSCAR', 'inputs/POTCAR')
VERSION_PATTERN = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
NODE_PATTERN = r'[a-z0-9_]{1,32}'
HASH_PATTERN = r'[0-9a-f]{64}'
UUID_PATTERN = r'(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})'
UTC_PATTERN = r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z'


def _fail(name):
    raise ContractError(f'Malformed {name}') from None


def _object(value, required, name, optional=()):
    if (type(value) is not dict or set(required) - value.keys()
            or value.keys() - set(required) - set(optional)):
        _fail(name + ' fields')
    return value


def _text(value, name, pattern=None):
    if (type(value) is not str or not value or any(ord(c) < 32 for c in value)
            or (pattern is not None and re.fullmatch(pattern, value) is None)):
        _fail(name)
    return value


def _integer(value, name, minimum=1):
    if (type(value) not in (int, float) or (type(value) is float and not math.isfinite(value))
            or value < minimum or int(value) != value):
        _fail(name)
    return int(value)


def _boolean(value, name):
    if type(value) is not bool:
        _fail(name)
    return value


def _enum(value, enum, name):
    if type(value) is not str:
        _fail(name)
    try:
        return enum(value)
    except ValueError:
        _fail(name)


def _array(value, name, *, nonempty=False):
    if type(value) is not list or (nonempty and not value):
        _fail(name)
    return value


def _strings(value, name):
    result = tuple(_text(item, name) for item in _array(value, name))
    if len(result) != len(set(result)):
        _fail(name + ' duplicates')
    return result


def parse_version(value: str) -> tuple[int, int]:
    _text(value, 'version', VERSION_PATTERN)
    major, minor = value.split('.')
    try:
        return int(major), int(minor)
    except ValueError:
        _fail('version integer range')


def _version(value):
    if parse_version(value)[0] != 1:
        raise UnsupportedVersionError(value)
    return value


def _utc(value, name):
    _text(value, name, UTC_PATTERN)
    try:
        datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:
        _fail(name)
    return value


def _json_value(value, name='JSON value'):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item, name)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item, name)
        return
    _fail(name)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail('duplicate JSON key')
        result[key] = value
    return result


def _wire(value):
    if isinstance(value, JsonModel):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    return value


class JsonModel:
    REQUIRED: ClassVar[tuple[str, ...]]

    @classmethod
    def from_json(cls, payload: str | bytes):
        try:
            value = json.loads(payload, object_pairs_hook=_pairs,
                               parse_constant=lambda _: _fail('non-finite JSON number'))
            _json_value(value)
            return cls.from_dict(value)
        except ContractError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ContractError('Malformed JSON payload') from exc

    def to_dict(self):
        return {item.name: _wire(getattr(self, item.name)) for item in fields(self)}

    def to_json(self):
        value = self.to_dict()
        # Construction by Python callers cannot bypass the wire contract.
        type(self).from_dict(value)
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class ErrorObject(JsonModel):
    code: ErrorCode
    message: str
    details: dict[str, Any]
    REQUIRED = ('code', 'message', 'details')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'error')
        if type(value['details']) is not dict:
            _fail('error details')
        _json_value(value['details'])
        return cls(_enum(value['code'], ErrorCode, 'error code'),
                   _text(value['message'], 'error message'), dict(value['details']))


@dataclass(frozen=True)
class Build(JsonModel):
    build_id: str
    version: str
    executables: tuple[str, ...]
    experimental: bool
    default: bool
    REQUIRED = ('build_id', 'version', 'executables', 'experimental', 'default')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'build')
        return cls(_text(value['build_id'], 'build ID'), _text(value['version'], 'build version'),
                   _strings(value['executables'], 'executables'),
                   _boolean(value['experimental'], 'experimental'),
                   _boolean(value['default'], 'default'))


@dataclass(frozen=True)
class EngineCapabilities(JsonModel):
    builds: tuple[Build, ...]
    REQUIRED = ('builds',)

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'engine capabilities')
        builds = tuple(Build.from_dict(item) for item in _array(value['builds'], 'builds'))
        if len({item.build_id for item in builds}) != len(builds):
            _fail('duplicate build IDs')
        return cls(builds)


@dataclass(frozen=True)
class ReceiveCapability(JsonModel):
    max_bundle_bytes: int
    REQUIRED = ('max_bundle_bytes',)

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'receive capability')
        return cls(_integer(value['max_bundle_bytes'], 'maximum bundle bytes'))


@dataclass(frozen=True)
class Capabilities(JsonModel):
    submit: bool | None = None
    receive: ReceiveCapability | None = None
    cancel_modes: dict[str, tuple[CancelMode, ...]] | None = None
    engines: dict[str, EngineCapabilities] | None = None
    extensions: dict[str, Any] = field(default_factory=dict)
    REQUIRED = ()
    KNOWN: ClassVar[tuple[str, ...]] = ('submit', 'receive', 'cancel_modes', 'engines')

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict:
            _fail('capabilities')
        _json_value(value)
        modes = engines = None
        if 'cancel_modes' in value:
            if type(value['cancel_modes']) is not dict:
                _fail('cancel modes')
            modes = {}
            for engine, offered in value['cancel_modes'].items():
                _text(engine, 'engine')
                modes[engine] = tuple(_enum(mode, CancelMode, 'cancel mode')
                                      for mode in _array(offered, 'cancel modes'))
                if len(modes[engine]) != len(set(modes[engine])):
                    _fail('duplicate cancel modes')
        if 'engines' in value:
            if type(value['engines']) is not dict:
                _fail('engines')
            engines = {_text(name, 'engine'): EngineCapabilities.from_dict(offer)
                       for name, offer in value['engines'].items()}
        return cls(_boolean(value['submit'], 'submit capability') if 'submit' in value else None,
                   ReceiveCapability.from_dict(value['receive']) if 'receive' in value else None,
                   modes, engines, {key: item for key, item in value.items() if key not in cls.KNOWN})

    def to_dict(self):
        if set(self.extensions) & set(self.KNOWN):
            _fail('capability extension shadows known feature')
        result = dict(self.extensions)
        result.update({name: _wire(getattr(self, name)) for name in self.KNOWN
                       if getattr(self, name) is not None})
        return result


@dataclass(frozen=True)
class CoreTier(JsonModel):
    """Native hardware facts; tier names carry no scheduler interpretation."""
    level: int
    name: str
    physical_cores: int
    REQUIRED = ('level', 'name', 'physical_cores')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'core tier')
        return cls(_integer(value['level'], 'core tier level', 0),
                   _text(value['name'], 'native core tier name'),
                   _integer(value['physical_cores'], 'core tier physical cores'))


@dataclass(frozen=True)
class Hardware(JsonModel):
    physical_cores: int
    core_tiers: tuple[CoreTier, ...]
    recommended_max_parallelism: int | None
    REQUIRED = ('physical_cores', 'core_tiers', 'recommended_max_parallelism')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'hardware')
        recommendation = value['recommended_max_parallelism']
        return cls(_integer(value['physical_cores'], 'physical cores'),
                   tuple(CoreTier.from_dict(item) for item in _array(value['core_tiers'], 'core tiers')),
                   None if recommendation is None else
                   _integer(recommendation, 'recommended maximum parallelism'))


@dataclass(frozen=True)
class HelloResult(JsonModel):
    node_id: str
    protocol_version: str
    cmw_version: str
    cmw_commit: str
    node_time_utc: str
    hardware: Hardware
    capabilities: Capabilities
    REQUIRED = ('node_id', 'protocol_version', 'cmw_version', 'cmw_commit',
                'node_time_utc', 'hardware', 'capabilities')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'hello result')
        return cls(_text(value['node_id'], 'node ID', NODE_PATTERN),
                   _version(value['protocol_version']), _text(value['cmw_version'], 'CMW version'),
                   _text(value['cmw_commit'], 'CMW commit'), _utc(value['node_time_utc'], 'node UTC time'),
                   Hardware.from_dict(value['hardware']), Capabilities.from_dict(value['capabilities']))


@dataclass(frozen=True)
class Envelope(JsonModel):
    protocol_version: str
    node_id: str
    command: str
    ok: bool
    result: dict[str, Any] | None
    error: ErrorObject | None
    request_id: str | None = None
    REQUIRED = ('protocol_version', 'node_id', 'command', 'ok', 'result', 'error')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'envelope', ('request_id',))
        version = _version(value['protocol_version'])
        node = _text(value['node_id'], 'node ID', NODE_PATTERN)
        command = _text(value['command'], 'command')
        if command not in COMMANDS:
            _fail('command')
        ok = _boolean(value['ok'], 'ok')
        if ok:
            if type(value['result']) is not dict or value['error'] is not None:
                _fail('successful envelope result/error')
            _json_value(value['result'])
            error = None
            if command == 'hello':
                hello = HelloResult.from_dict(value['result'])
                if hello.node_id != node or hello.protocol_version != version:
                    _fail('hello/envelope identity or version disagreement')
        else:
            if value['result'] is not None:
                _fail('failed envelope result')
            error = ErrorObject.from_dict(value['error'])
        request_id = _text(value['request_id'], 'request ID') if 'request_id' in value else None
        return cls(version, node, command, ok, value['result'], error, request_id)

    def to_dict(self):
        value = super().to_dict()
        if self.request_id is None:
            del value['request_id']
        return value


@dataclass(frozen=True)
class BuildSelection(JsonModel):
    build_id: str | None
    allow_experimental: bool
    REQUIRED = ('build_id', 'allow_experimental')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'build selection')
        selected = value['build_id']
        return cls(None if selected is None else _text(selected, 'build ID'),
                   _boolean(value['allow_experimental'], 'allow experimental'))


@dataclass(frozen=True)
class Execution(JsonModel):
    mpi_ranks: int
    omp_threads: int
    REQUIRED = ('mpi_ranks', 'omp_threads')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'execution')
        return cls(*(_integer(value[name], name) for name in cls.REQUIRED))


@dataclass(frozen=True)
class ManifestFile(JsonModel):
    path: str
    size: int
    sha256: str
    REQUIRED = ('path', 'size', 'sha256')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'manifest file')
        if value['path'] not in INPUT_PATHS:
            _fail('manifest input path')
        return cls(value['path'], _integer(value['size'], 'file size', 0),
                   _text(value['sha256'], 'file SHA-256', HASH_PATTERN))


@dataclass(frozen=True)
class PotcarIdentity(JsonModel):
    element: str
    variant: str
    titel: str
    sha256: str
    REQUIRED = ('element', 'variant', 'titel', 'sha256')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'POTCAR identity')
        return cls(_text(value['element'], 'element', r'[A-Z][a-z]?'),
                   _text(value['variant'], 'potential variant'), _text(value['titel'], 'TITEL identity'),
                   _text(value['sha256'], 'dataset SHA-256', HASH_PATTERN))


@dataclass(frozen=True)
class Provenance(JsonModel):
    preparation_record_sha256: str
    potcar_identity: tuple[PotcarIdentity, ...]
    REQUIRED = ('preparation_record_sha256', 'potcar_identity')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'provenance')
        return cls(_text(value['preparation_record_sha256'], 'preparation record SHA-256', HASH_PATTERN),
                   tuple(PotcarIdentity.from_dict(item) for item in
                         _array(value['potcar_identity'], 'POTCAR identities', nonempty=True)))


@dataclass(frozen=True)
class Manifest(JsonModel):
    schema_version: str
    request_id: str
    source_node_id: str
    created_utc: str
    engine: str
    executable: str
    build: BuildSelection
    execution: Execution
    files: tuple[ManifestFile, ...]
    provenance: Provenance
    REQUIRED = ('schema_version', 'request_id', 'source_node_id', 'created_utc',
                'engine', 'executable', 'build', 'execution', 'files', 'provenance')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'manifest')
        version = _version(value['schema_version'])
        if value['engine'] != 'vasp':
            _fail('manifest engine')
        if value['executable'] not in ('vasp_std', 'vasp_gam', 'vasp_ncl'):
            _fail('VASP executable')
        entries = tuple(ManifestFile.from_dict(item) for item in _array(value['files'], 'manifest files'))
        if len(entries) != 4 or {item.path for item in entries} != set(INPUT_PATHS):
            _fail('manifest requires each of the four VASP inputs exactly once')
        return cls(version, _text(value['request_id'], 'request UUID', UUID_PATTERN),
                   _text(value['source_node_id'], 'source node ID', NODE_PATTERN),
                   _utc(value['created_utc'], 'creation UTC time'), 'vasp', value['executable'],
                   BuildSelection.from_dict(value['build']), Execution.from_dict(value['execution']),
                   entries, Provenance.from_dict(value['provenance']))


@dataclass(frozen=True)
class CancelEvent(JsonModel):
    mode: CancelMode
    requested_utc: str
    by_node_id: str
    REQUIRED = ('mode', 'requested_utc', 'by_node_id')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'cancel history event')
        return cls(_enum(value['mode'], CancelMode, 'cancel mode'),
                   _utc(value['requested_utc'], 'cancel request UTC time'),
                   _text(value['by_node_id'], 'requesting node ID', NODE_PATTERN))


@dataclass(frozen=True)
class CancelState(JsonModel):
    """The inner ``cancel`` object; absence of cancellation is outside this model."""
    mode: CancelMode
    state: str
    history: tuple[CancelEvent, ...]
    REQUIRED = ('mode', 'state', 'history')

    @classmethod
    def from_dict(cls, value):
        _object(value, cls.REQUIRED, 'cancel state')
        mode = _enum(value['mode'], CancelMode, 'cancel mode')
        if value['state'] not in ('requested', 'in_progress', 'completed', 'failed'):
            _fail('cancel state')
        history = tuple(CancelEvent.from_dict(item) for item in
                        _array(value['history'], 'cancel history', nonempty=True))
        order = tuple(CancelMode)
        positions = [order.index(item.mode) for item in history]
        if (any(left >= right for left, right in zip(positions, positions[1:]))
                or history[-1].mode != mode):
            _fail('cancel history escalation or effective mode')
        return cls(mode, value['state'], history)
