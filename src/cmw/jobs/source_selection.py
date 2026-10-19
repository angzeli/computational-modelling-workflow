"""Read-only validation and immutable, attempt-scoped session source selections."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from cmw.core.provenance import stable_hash
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.registry import SourcePlan, get_registration
from .locations import _read_job, inspect_path
from .source_declarations import validate_sources
from .store import JobsError


@dataclass(frozen=True)
class SourceInspection:
    identity: tuple
    context: RunContext
    plans: tuple[SourcePlan, ...]
    declarations: tuple[SourceDescriptor, ...]


@dataclass(frozen=True)
class SourceFingerprint:
    literal: str
    canonical: str
    file_identity: tuple | None
    directory_identity: tuple | None
    health: str
    issue: str


@dataclass(frozen=True)
class SourceValidation:
    identity: tuple
    context: RunContext
    sources: tuple[SourceDescriptor, ...]
    fingerprint: tuple[SourceFingerprint, ...]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    conflict: bool = False
    conflicts: tuple[str, ...] = ()

    @property
    def valid(self):
        return not self.errors


@dataclass(frozen=True)
class SessionSelection:
    identity: tuple
    sources: tuple[SourceDescriptor, ...]
    fingerprint: tuple[SourceFingerprint, ...]
    generation: int
    base_key: str
    conflicts: tuple[str, ...] = ()


def _identity(store, job, state_identity, engine):
    return (str(store.root.resolve()), *state_identity, job['id'], job['attempt_id'], engine)


def _base_key(context):
    # A future directory becoming available is not a declaration edit.
    return stable_hash({'engine': context.engine, 'binding': context.binding_status,
        'declarations': [(source.role, source.path, source.stage, source.optional, source.origins)
                         for source in context.declarations], 'conflicts': context.source_conflicts})


def inspect_selection(store, job_id, *, expected_attempt_id=None, session_selection=None,
                      descriptors=(), resolvers=None, engine=None):
    from .progress import resolve_context
    job, state_identity = _read_job(store, job_id)
    expected = expected_attempt_id or job['attempt_id']
    context = resolve_context(store, job_id, expected_attempt_id=expected,
        session_selection=session_selection, descriptors=descriptors, resolvers=resolvers, engine=engine)
    after, after_identity = _read_job(store, job_id)
    identity = _identity(store, job, state_identity, context.engine)
    if identity != _identity(store, after, after_identity, context.engine):
        raise JobsError('Jobs attempt changed while inspecting sources')
    registration = get_registration(context.engine)
    plans = () if registration is None else tuple(
        {plan.role: plan for plan in (*registration.sources, *registration.source_roles)}.values())
    return SourceInspection(identity, context, plans, context.declarations)


def _fingerprints(sources, *, expected_creation=False):
    result = []
    for source in sources:
        canonical, health, file_id, directory_id, issue = inspect_path(
            Path(source.path), 'file', expected_creation=expected_creation)
        if directory_id is None:
            try:
                parent = Path(canonical).parent.stat()
                directory_id = (parent.st_dev, parent.st_ino)
            except OSError:
                pass
        result.append(SourceFingerprint(source.path, canonical, file_id, directory_id, health, issue))
    return tuple(result)


def validate_selection(store, job_id, sources, *, expected_attempt_id=None,
                       descriptors=(), resolvers=None, engine=None):
    from .progress import resolve_context
    inspection = inspect_selection(store, job_id, expected_attempt_id=expected_attempt_id,
        descriptors=descriptors, resolvers=resolvers, engine=engine)
    context = inspection.context
    try:
        sources = validate_sources(context.engine, sources, require_complete=True)
    except JobsError as exc:
        return SourceValidation(inspection.identity, context, (), (), (str(exc),))
    job, state_identity = _read_job(store, job_id)
    if inspection.identity != _identity(store, job, state_identity, context.engine):
        raise JobsError('Jobs attempt changed while validating sources')
    pending = job['status'] in {'Queue', 'Hold'} and not job.get('started_at')
    fingerprint = _fingerprints(sources, expected_creation=pending)
    errors, warnings = [], []
    if len({item.canonical for item in fingerprint}) != len(fingerprint):
        errors.append('One canonical file cannot represent multiple source roles or stages')
    for source, item in zip(sources, fingerprint):
        if item.health == 'AVAILABLE':
            continue
        message = f'{source.role}: {item.issue or item.health}'
        future = pending and item.health in {'NOT_CREATED_YET', 'VOLUME_UNAVAILABLE'}
        (warnings if item.health != 'LOCATION_UNKNOWN' and (source.optional or future) else errors).append(message)
    candidate = resolve_context(store, job_id, expected_attempt_id=inspection.identity[4],
        sources=sources, descriptors=descriptors, resolvers=resolvers, engine=context.engine)
    if candidate.binding_status != 'BOUND':
        errors.extend(candidate.limitations)
    # Preserve literal aliases for later identity checks; use the resolved stage/role.
    normalized = []
    for source in sources:
        match = next((item for item in candidate.sources
                      if item.path == str(Path(source.path).resolve(strict=False))), source)
        normalized.append(replace(source, role=match.role, stage=match.stage,
            origins=('session-local',), qualification='user-declared-session-local; scientific lineage unverified'))
    conflicts = tuple(dict.fromkeys((*context.source_conflicts, *candidate.source_conflicts)))
    warnings.extend(conflicts)
    return SourceValidation(inspection.identity, context, tuple(normalized), fingerprint,
        tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings)), bool(conflicts), conflicts)


def activate_selection(store, validation, generation, *, confirmed=False, descriptors=(), resolvers=None):
    if not validation.valid:
        raise JobsError('Validate complete source declarations before activation')
    if type(generation) is not int or generation < 1:
        raise JobsError('Source binding generation must be a positive integer')
    fresh = validate_selection(store, validation.identity[3], validation.sources,
        expected_attempt_id=validation.identity[4], engine=validation.identity[5],
        descriptors=descriptors, resolvers=resolvers)
    if (not fresh.valid or fresh.identity != validation.identity
            or _base_key(fresh.context) != _base_key(validation.context)
            or fresh.fingerprint != validation.fingerprint or fresh.conflicts != validation.conflicts):
        raise JobsError('Source identity or declarations changed; validate again before activation')
    if fresh.conflict and not confirmed:
        raise JobsError('Confirm the reviewed source conflict before using this session override')
    return SessionSelection(fresh.identity, fresh.sources, fresh.fingerprint, generation,
                            _base_key(fresh.context), fresh.conflicts)


def apply_selection(store, job, state_identity, context, selection):
    from .progress import QUALIFICATION, _source_identity
    if (selection.identity != _identity(store, job, state_identity, context.engine)
            or selection.base_key != _base_key(context)):
        raise JobsError('Session source selection is stale; open Sources and validate this attempt again')
    current = _fingerprints(selection.sources)
    for before, after in zip(selection.fingerprint, current):
        if (before.canonical != after.canonical
                or before.file_identity and after.file_identity and before.file_identity != after.file_identity
                or before.directory_identity and after.directory_identity
                and before.directory_identity != after.directory_identity
                or after.health == 'LOCATION_UNKNOWN'):
            raise JobsError('Selected source identity changed; open Sources and validate again')
    sources = tuple(_source_identity(source) for source in selection.sources)
    stages = {source.stage for source in sources if source.stage is not None}
    signature = {'default': context.key, 'generation': selection.generation,
                 'identity': selection.identity, 'sources': [asdict(source) for source in sources]}
    limitations = ('Session-local source selection; caller-declared; scientific lineage unverified.',
                  *selection.conflicts, QUALIFICATION)
    return replace(context, binding_id=stable_hash(signature), sources=sources,
        stage=next(iter(stages)) if len(stages) == 1 else None,
        association='user-declared:session-local', binding_status='BOUND',
        limitations=limitations, source_conflicts=selection.conflicts,
        binding_generation=selection.generation)
