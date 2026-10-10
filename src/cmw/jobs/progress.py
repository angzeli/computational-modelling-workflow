"""Read-only Jobs association for the independent live-observation client."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import os

from cmw.core.provenance import stable_hash
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.registry import get_registration
from .locations import _read_job, discover
from .store import JobsError


def _explicit_sources(sources):
    result, roles, paths = [], set(), set()
    for source in sources:
        if not isinstance(source, SourceDescriptor):
            raise JobsError('Progress sources must be SourceDescriptor values')
        if (not source.role or not source.role.isprintable() or len(source.role) > 128
                or source.role in roles):
            raise JobsError('Progress source roles must be distinct nonempty printable names')
        path = str(source.path)
        if not Path(path).is_absolute() or '\0' in path or '://' in path:
            raise JobsError('Progress source paths must be absolute filesystem paths')
        canonical = str(Path(path).resolve(strict=False))
        if canonical in paths:
            raise JobsError('A source cannot be assigned to multiple progress roles')
        roles.add(source.role)
        paths.add(canonical)
        result.append(SourceDescriptor(source.role, os.path.abspath(path), source.qualification,
                                       source.optional, source.root_path, source.root_identity))
    return tuple(result)


def resolve_context(store, job_id, *, expected_attempt_id=None, descriptors=(), resolvers=None,
                    sources=(), engine=None):
    """Consume delivery-1 declarations; never infer output from cwd or activity.

    ``sources`` is an explicit session-local replacement for the default source
    plan. It is useful for opaque wrappers and a known stdout dialect. It never
    edits argv, source records or Jobs state.
    """
    job, state_identity = _read_job(store, job_id)
    expected = job['attempt_id'] if expected_attempt_id is None else expected_attempt_id
    if expected != job['attempt_id']:
        raise JobsError('Attempt identity differs; select the job again')
    found = discover(store, job['id'], expected_attempt_id=expected,
                     descriptors=descriptors, resolvers=resolvers)
    if any(location.state_identity != state_identity for location in found.locations):
        raise JobsError('Jobs state changed while resolving progress sources')
    selected_engine = str(engine or job['engine']).upper()
    registration = get_registration(selected_engine)
    association = 'explicit-caller-declaration' if sources else 'declared-job-location'
    limitations = ['Location declarations do not establish process ownership or scientific validity.']
    binding_status, stage = 'BOUND', None
    selected = []
    bindings = []
    if sources:
        selected = list(_explicit_sources(sources))
        stages = {item.stage for item in found.locations if item.stage is not None}
        if len(stages) == 1:
            stage = stages.pop()
        elif len(stages) > 1:
            binding_status = 'SOURCE_UNRESOLVED'
            limitations.append('Several workflow stages are declared; an explicit run context is required.')
        limitations.append('Session-local source override; historical execution binding is unverified.')
    elif registration is not None:
        preferred = next((item for item in found.locations if item.key == found.preferred_key), None)
        if preferred is None:
            binding_status = 'SOURCE_UNRESOLVED'
            limitations.append('No unique declared output/stage: ' + found.status)
            limitations.extend(found.diagnostics)
        else:
            stage = preferred.stage
            for plan in registration.sources:
                if plan.location_role == 'output':
                    candidates = [preferred]
                else:
                    candidates = [item for item in found.locations
                                  if item.role == plan.location_role and item.path is not None
                                  and item.stage in {None, stage}]
                destinations = {item.path: item for item in candidates}
                if len(destinations) != 1:
                    if not plan.optional:
                        binding_status = 'SOURCE_UNRESOLVED'
                    limitations.append(f'{plan.role}: no unique {plan.location_role} directory declaration')
                    continue
                location = next(iter(destinations.values()))
                if location.kind != 'directory':
                    if not plan.optional:
                        binding_status = 'SOURCE_UNRESOLVED'
                    limitations.append(f'{plan.role}: directory declaration required for a source plan')
                    continue
                # Canonical output names belong to the registered dialect, never a tree search.
                name = Path(plan.filename)
                if name.name != plan.filename or plan.filename in {'.', '..'}:
                    raise JobsError('Registered progress source names must be single literal filenames')
                selected.append(SourceDescriptor(plan.role, str(Path(location.path) / name),
                    location.qualification + ':' + location.origin, plan.optional,
                    location.path, location.directory_identity))
                bindings.append((location.key, location.target_identity, location.directory_identity))
                if plan.location_role == 'output':
                    association = location.qualification + ':' + location.origin
    signature = {'state': str(store.root), 'state_identity': state_identity, 'job': job['id'],
                 'attempt': expected, 'engine': selected_engine, 'stage': stage,
                 'sources': [asdict(source) for source in selected], 'locations': bindings,
                 'binding_status': binding_status}
    return RunContext(stable_hash(signature), selected_engine, tuple(selected), stage, expected,
                      str(store.root), job['id'], association, binding_status, tuple(limitations))
