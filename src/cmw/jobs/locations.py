"""Read-only declarations of where an immutable Jobs attempt keeps its files.

These declarations confer neither process ownership nor scientific acceptance.
Resolvers receive a persisted job mapping and return session-local descriptors;
no engine-specific dispatch belongs in this module or in the folder opener.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import errno
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Callable, Iterable, Mapping

from cmw.core.execution_layout import resolve_recorded_layout
from cmw.core.provenance import stable_hash
from .store import JobsError, Store


@dataclass(frozen=True)
class LocationDescriptor:
    role: str
    path: str | Path | None
    kind: str = field(default='directory', kw_only=True)
    stage: str | None = field(default=None, kw_only=True)
    origin: str = field(default='explicit-caller', kw_only=True)
    qualification: str = field(default='declared', kw_only=True)
    detail: str = field(default='', kw_only=True)
    expected_creation: bool = field(default=False, kw_only=True)

    def __post_init__(self):
        for name in ('role', 'origin', 'qualification', 'stage'):
            value = getattr(self, name)
            if name == 'stage' and value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 256 or not value.isprintable():
                raise ValueError(f'Location {name} must be nonempty printable text of at most 256 characters')
        if not isinstance(self.expected_creation, bool):
            raise ValueError('Expected creation must be an explicit boolean')
        if self.kind not in {'directory', 'file'}:
            raise ValueError('Location kind must be directory or file')


@dataclass(frozen=True)
class Location:
    key: str
    state_directory: str
    state_identity: tuple[int, int]
    job_id: int
    attempt_id: str
    role: str
    path: str | None
    declared_path: str | None
    kind: str
    stage: str | None
    origin: str
    accessibility: str
    qualification: str
    detail: str
    target_identity: tuple[int, int] | None = None
    directory_identity: tuple[int, int] | None = None
    process_association: str = 'not-established'
    scientific_validation: str = 'not-assessed'
    process_ownership: bool = False
    ambiguity: bool = False

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class LocationResult:
    state_directory: str
    job_id: int
    display_id: str
    attempt_id: str
    status: str
    locations: tuple[Location, ...]
    preferred_key: str | None
    diagnostics: tuple[str, ...] = ()

    def to_dict(self):
        return {'schema': 1, **asdict(self)}


Resolver = Callable[[Mapping], Iterable[LocationDescriptor]]


def read_location_metadata(path, *, max_bytes=1024 * 1024):
    """Read one explicitly linked, bounded regular JSON object without FIFO waits."""
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f'Location metadata is not a regular file: {path}')
        if before.st_size > max_bytes:
            raise ValueError(f'Location metadata exceeds {max_bytes} bytes: {path}')
        data = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
    if len(data) > max_bytes:
        raise ValueError(f'Location metadata exceeds {max_bytes} bytes: {path}')
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(after):
        raise ValueError(f'Location metadata changed while reading: {path}')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f'Location metadata must be a JSON object: {path}')
    return value


def _read_job(store, job_id):
    """Reuse Store's identifier rules without its controller/process snapshot."""
    if str(job_id).startswith('E'):
        raise JobsError('External observations have no managed Jobs file identity')
    try:
        before = store.path.stat()
        con = sqlite3.connect(store.path.as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            con.execute('BEGIN')
            Store.control(con)  # Reject unsupported schema without migration.
            job = Store.get(con, job_id)
        finally:
            con.close()
        after = store.path.stat()
    except FileNotFoundError as exc:
        raise JobsError(f'No such job: {job_id}') from exc
    except sqlite3.Error as exc:
        raise JobsError(f'Queue database unavailable: {exc}') from exc
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise JobsError('Jobs state changed while reading; select the job again')
    return job, (after.st_dev, after.st_ino)


def _absolute_path(path, cwd):
    value = os.fspath(path)
    if not isinstance(value, str) or not value or '\0' in value or '://' in value:
        raise ValueError('Location must be a filesystem path, not a URI or empty value')
    result = Path(value)
    return Path(os.path.abspath(result if result.is_absolute() else Path(cwd) / result))


def _file_identity(info):
    return info.st_dev, info.st_ino


def _missing_status(path, *, expected_creation=False):
    # An absent mount point is evidence of unavailable storage; a missing file
    # below a present volume is merely missing. Never mount or substitute paths.
    if len(path.parts) >= 3 and path.parts[1] == 'Volumes':
        try:
            Path('/Volumes', path.parts[2]).stat()
        except FileNotFoundError:
            return 'VOLUME_UNAVAILABLE'
        except PermissionError:
            return 'PERMISSION_DENIED'
    return 'NOT_CREATED_YET' if expected_creation else 'NOT_FOUND'


def inspect_path(path, kind, *, expected_creation=False):
    """Return current canonical target, accessibility and non-content identity."""
    canonical = str(path)
    try:
        resolved = path.resolve(strict=False)
        canonical = str(resolved)
        info = resolved.stat()
        if kind == 'directory' and not stat.S_ISDIR(info.st_mode):
            return canonical, 'LOCATION_UNKNOWN', None, None, 'Declared directory is not a directory'
        if kind == 'file' and not stat.S_ISREG(info.st_mode):
            return canonical, 'LOCATION_UNKNOWN', None, None, 'Declared source is not a regular file'
        if kind == 'file' and not os.access(resolved, os.R_OK):
            return canonical, 'PERMISSION_DENIED', None, None, 'Declared source file is not readable'
        directory = resolved if kind == 'directory' else resolved.parent
        # stat alone does not establish directory traversal/read permission.
        with os.scandir(directory):
            pass
        if not os.access(directory, os.R_OK | os.X_OK):
            return canonical, 'PERMISSION_DENIED', None, None, 'Directory is not readable/searchable'
        return canonical, 'AVAILABLE', _file_identity(info), _file_identity(directory.stat()), ''
    except FileNotFoundError:
        status = _missing_status(Path(canonical), expected_creation=expected_creation)
        return canonical, status, None, None, f'{status}: {canonical}'
    except PermissionError as exc:
        return canonical, 'PERMISSION_DENIED', None, None, str(exc)
    except (RuntimeError, ValueError) as exc:
        return canonical, 'LOCATION_UNKNOWN', None, None, str(exc)
    except OSError as exc:
        status = 'LOCATION_UNKNOWN' if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EINVAL} else 'OPEN_FAILED'
        return canonical, status, None, None, str(exc)


def _layout_descriptors(job):
    reference = job.get('layout')
    if not reference:
        return ()
    metadata = _absolute_path(reference['path'], job['cwd'])
    record = read_location_metadata(metadata)
    layout = resolve_recorded_layout(record, metadata_path=metadata)
    if (layout.target_identifier != reference.get('target_id') or
            layout.attempt_identifier != reference.get('attempt_id')):
        raise ValueError('Recorded execution layout identity differs from the queued reference')
    stage = layout.operational_stage or layout.workflow_node_identifier
    common = dict(stage=stage, origin='layout', qualification='recorded-layout-identity')
    return (LocationDescriptor('output', layout.output_directory, **common),
            LocationDescriptor('operational-logs', layout.logs_directory, **common),
            LocationDescriptor('inputs', layout.working_directory, **common),
            LocationDescriptor('records', layout.provenance_directory, **common),
            LocationDescriptor('records', metadata, kind='file', **common))


def discover(store, job_id, *, expected_attempt_id=None, descriptors=(), resolvers=None):
    """Resolve current declarations read-only; caller descriptors are not persisted.

    Supplying ``resolvers`` replaces the small default registry. A future engine
    therefore needs only a callable returning LocationDescriptor values.
    """
    job, state_identity = _read_job(store, job_id)
    if expected_attempt_id is not None and job['attempt_id'] != expected_attempt_id:
        raise JobsError('Attempt identity differs; select the job again')
    declared = [LocationDescriptor('working-directory', job['cwd'], origin='jobs-record',
                                  qualification='recorded-working-directory',
                                  detail='Working directory; no output association is inferred')]
    pending = job['status'] in {'Queue', 'Hold'} and job.get('started_at') is None
    for stream, path in job.get('logs', {}).items():
        if stream in {'stdout', 'stderr'}:
            declared.append(LocationDescriptor('operational-logs', path, kind='file',
                origin='jobs-record', qualification='recorded-attempt-log', detail=stream,
                expected_creation=True))
    diagnostics = []
    try:
        declared.extend(_layout_descriptors(job))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        diagnostics.append(f'Execution layout unavailable: {exc}')
        declared.append(LocationDescriptor('output', None, origin='layout',
            qualification='unresolved', detail=diagnostics[-1]))
    if resolvers is None:
        from .location_resolvers import builtin_resolvers
        resolvers = builtin_resolvers()
    for resolver in resolvers:
        try:
            declared.extend(resolver(job))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            diagnostics.append(f'Location resolver unavailable: {exc}')
            declared.append(LocationDescriptor('output', None, origin='resolver',
                qualification='unresolved', detail=diagnostics[-1]))
    declared.extend(descriptors)
    if not any(item.role == 'output' for item in declared):
        declared.append(LocationDescriptor('output', None, origin='jobs-record',
            qualification='unresolved', detail='Output location is not declared by a supported route'))
    locations = []
    keys = set()
    for item in declared:
        if not isinstance(item, LocationDescriptor):
            raise TypeError('Location resolvers must return LocationDescriptor values')
        declared_path = canonical = None
        status, target_identity, directory_identity, detail = 'LOCATION_UNKNOWN', None, None, item.detail
        if item.path is not None:
            try:
                path = _absolute_path(item.path, job['cwd'])
                declared_path = str(path)
                canonical, status, target_identity, directory_identity, issue = inspect_path(
                    path, item.kind, expected_creation=item.expected_creation and pending)
                detail = '; '.join(value for value in (detail, issue) if value)
            except (TypeError, ValueError) as exc:
                detail = '; '.join(value for value in (detail, str(exc)) if value)
        key = stable_hash({'state': str(store.root), 'state_identity': state_identity,
            'job': job['id'], 'attempt': job['attempt_id'], 'role': item.role,
            'declared_path': declared_path, 'path': canonical, 'kind': item.kind,
            'stage': item.stage, 'origin': item.origin, 'qualification': item.qualification})
        if key in keys:
            continue
        keys.add(key)
        locations.append(Location(key, str(store.root), state_identity, job['id'], job['attempt_id'],
            item.role, canonical, declared_path, item.kind, item.stage, item.origin, status,
            item.qualification, detail, target_identity, directory_identity))
    outputs = [item for item in locations if item.role == 'output' and item.path is not None]
    unknown = any(item.role == 'output' and item.path is None for item in locations)
    stages = {item.stage for item in locations if item.stage is not None}
    destinations = {(str(Path(item.path).parent) if item.kind == 'file' else item.path)
                    for item in outputs}
    preferred = None
    if len(stages) > 1 or len(destinations) > 1:
        status = 'AMBIGUOUS'
    elif unknown or not outputs:
        status = 'LOCATION_UNKNOWN'
    else:
        preferred = next((item for item in outputs if item.kind == 'directory'), outputs[0])
        status = preferred.accessibility
    return LocationResult(str(store.root), job['id'], job['display_id'], job['attempt_id'],
        status, tuple(replace(item, ambiguity=status == 'AMBIGUOUS') for item in locations),
        preferred.key if preferred else None, tuple(diagnostics))
