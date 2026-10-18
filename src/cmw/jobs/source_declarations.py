"""Literal, attempt-scoped scientific source declarations; no filesystem reads."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import re
import unicodedata

from cmw.progress.models import SourceDescriptor
from cmw.progress.registry import get_registration
from .store import JobsError

SOURCE_SCHEMA = 1
SOURCE_ORIGIN = 'user-declared-at-add'
SOURCE_QUALIFICATION = 'caller-declared; scientific lineage unverified'
MAX_SOURCES = 8


def _syntax(sources):
    result, roles, paths = [], set(), set()
    for source in sources:
        if not isinstance(source, SourceDescriptor):
            raise JobsError('Progress sources must be SourceDescriptor values')
        role, stage = source.role, source.stage
        if not isinstance(role, str):
            raise JobsError('Progress source role must be a registered name')
        if stage is not None:
            if (not isinstance(stage, str) or not stage.strip() or not stage.isprintable()
                    or len(stage) > 256 or '@' in stage):
                raise JobsError('Progress source stage must be nonempty printable text without @ (up to 256 characters)')
            if ':' in role:
                role, suffix = role.split(':', 1)
                if suffix != stage:
                    raise JobsError('Progress source role and stage disagree')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,127}', role):
            raise JobsError('Progress source role must be a registered name')
        key = (role, stage)
        if key in roles:
            raise JobsError('Progress source roles must be distinct within each stage')
        path = source.path
        if (not isinstance(path, str) or not Path(path).is_absolute() or '://' in path
                or any(unicodedata.category(char) in {'Cc', 'Cs'} for char in path)):
            raise JobsError('Progress source paths must be literal absolute filesystem paths without control characters')
        # No resolve/stat: a future output need not exist, and no inode is pinned here.
        lexical = os.path.normpath(path)
        if lexical in paths:
            raise JobsError('A source cannot be assigned to multiple progress roles or stages')
        if type(source.optional) is not bool:
            raise JobsError('Progress source optional flag must be boolean')
        roles.add(key)
        paths.add(lexical)
        result.append(source)
        if len(result) > MAX_SOURCES:
            raise JobsError(f'Observation is bounded to at most {MAX_SOURCES} declared sources')
    return tuple(result)


def validate_sources(engine, sources, require_complete=False):
    """Check registered roles and literal syntax, never existence or file identity."""
    result = _syntax(sources)
    if not result:
        if require_complete:
            raise JobsError('Declare the required scientific sources')
        return result
    if not isinstance(engine, str) or not engine.strip():
        raise JobsError('Progress source engine is required')
    registration = get_registration(engine)
    if registration is None:
        if require_complete:
            raise JobsError(f'No progress adapter is registered for engine {engine}')
        return result
    plans = {plan.role: plan for plan in registration.sources}
    extra_plans = {plan.role: plan for plan in getattr(registration, 'source_roles', ())}
    role_plans = {**plans, **extra_plans}
    allowed = set(role_plans)
    # Factory-only registrations predate source plans and accept caller-owned roles.
    normalized = []
    for source in result:
        role = source.role.split(':', 1)[0]
        plan = role_plans.get(role)
        staged = source.stage is not None and (plan is None or plan.stages)
        normalized.append(replace(source, role=f'{role}:{source.stage}' if staged else role,
                                  optional=plan.optional if plan is not None else source.optional))
    result = tuple(normalized)
    for source in result:
        role = source.role.split(':', 1)[0]
        if allowed and role not in allowed:
            raise JobsError(f'Unsupported progress source role {role!r} for engine {engine}')
    stages = {source.stage for source in result}
    if None in stages and len(stages) > 1:
        raise JobsError('Progress source declarations must consistently identify their stages')
    for role, plan in role_plans.items():
        role_stages = {source.stage for source in result if source.role.split(':', 1)[0] == role}
        if not plan.stages and role_stages and len(stages) > 1:
            raise JobsError(f'Progress source role {role!r} does not support multiple stages')
    if require_complete:
        required = {plan.role for plan in registration.sources if not plan.optional}
        for stage in stages:
            present = {source.role.split(':', 1)[0] for source in result if source.stage == stage}
            missing = {role for role in required if role not in present
                       and not (set(getattr(plans[role], 'alternatives', ())) & present)}
            if missing:
                raise JobsError('Missing required progress source roles: ' + ', '.join(sorted(missing)))
    return result


def parse_sources(declarations, engine):
    """Parse ROLE[@STAGE]=ABSOLUTE_PATH without shell or environment expansion."""
    sources = []
    for declaration in declarations:
        if not isinstance(declaration, str) or '=' not in declaration:
            raise JobsError('Progress source requires ROLE[@STAGE]=ABSOLUTE_PATH')
        role, path = declaration.split('=', 1)
        stage = None
        if '@' in role:
            role, stage = role.split('@', 1)
        sources.append(SourceDescriptor(role, path, qualification='explicit-client-source', stage=stage))
    return validate_sources(engine, sources)


def declaration_record(engine, attempt_id, sources):
    """Serialize an already checked declaration against a newly created attempt."""
    return {'schema': SOURCE_SCHEMA, 'attempt_id': attempt_id, 'engine': engine.upper(),
            'origin': SOURCE_ORIGIN, 'qualification': SOURCE_QUALIFICATION,
            'sources': [{'role': source.role, 'path': source.path, 'stage': source.stage,
                         'optional': source.optional} for source in sources]}


def recorded_sources(job):
    """Read an immutable declaration; absent legacy metadata remains absent.

    Adapter availability is an observation concern. A stored custom declaration
    stays readable even in a process where its trusted factory is not registered.
    """
    if 'progress_sources' not in job:
        return ()
    record = job['progress_sources']
    if (not isinstance(record, dict) or type(record.get('schema')) is not int
            or record['schema'] != SOURCE_SCHEMA
            or record.get('attempt_id') != job.get('attempt_id')
            or not isinstance(record.get('engine'), str)
            or record['engine'] != str(job.get('engine', '')).upper()
            or record.get('origin') != SOURCE_ORIGIN
            or record.get('qualification') != SOURCE_QUALIFICATION
            or not isinstance(record.get('sources'), list) or not record['sources']):
        raise JobsError('Invalid attempt-scoped progress source declaration')
    sources = []
    for value in record['sources']:
        if not isinstance(value, dict) or set(value) != {'role', 'path', 'stage', 'optional'}:
            raise JobsError('Invalid recorded progress source descriptor')
        sources.append(SourceDescriptor(value['role'], value['path'],
            qualification=SOURCE_ORIGIN + ': ' + SOURCE_QUALIFICATION,
            stage=value['stage'], optional=value['optional'], origins=(SOURCE_ORIGIN,)))
    return _syntax(sources)
