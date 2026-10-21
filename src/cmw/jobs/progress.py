"""Read-only, automatic-first association for the bounded progress client."""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from cmw.core.provenance import stable_hash
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.registry import get_registration
from .locations import _read_job, discover
from .store import JobsError


QUALIFICATION = 'Location declarations do not establish process ownership or scientific validity.'


def source_role(source):
    return source.role.split(':', 1)[0]


def _explicit_sources(sources, engine=None):
    from .source_declarations import validate_sources
    sources = validate_sources(engine, sources) if engine else tuple(sources)
    result, paths = [], set()
    for source in sources:
        canonical = str(Path(source.path).resolve(strict=False))
        if canonical in paths:
            raise JobsError('A source cannot be assigned to multiple progress roles')
        paths.add(canonical)
        result.append(source)
    return tuple(result)


def _automatic(found, registration):
    """Translate every declared location through the existing registered plans."""
    candidates, issues = [], list(found.diagnostics)
    if registration is None:
        return candidates, issues
    for plan in registration.sources:
        locations = [item for item in found.locations if item.role == plan.location_role]
        for location in locations:
            if location.path is None:
                if location.detail:
                    issues.append(location.detail)
                continue
            expected_kind = 'directory' if plan.filename is not None else 'file'
            if location.kind != expected_kind:
                issues.append(f'{plan.role}: {expected_kind} declaration required for a source plan')
                continue
            if plan.filename is not None:
                name = Path(plan.filename)
                if name.name != plan.filename or plan.filename in {'.', '..'}:
                    raise JobsError('Registered progress source names must be single literal filenames')
                path, root = str(Path(location.path) / name), location.path
            else:
                path, root = location.path, str(Path(location.path).parent)
            candidates.append(SourceDescriptor(plan.role, path,
                location.qualification + ':' + location.origin, plan.optional,
                root, location.directory_identity, location.stage, (location.origin,)))
    return candidates, issues


def _staged(sources):
    """Infer only an unambiguous stage already attached to the same role/path."""
    result = []
    for source in sources:
        if source.stage is None:
            matching = {item.stage for item in sources if source_role(item) == source_role(source)
                        and item.stage is not None and item.path == source.path}
            if len(matching) == 1:
                source = replace(source, stage=next(iter(matching)))
        result.append(source)
    return result


def reconcile_sources(sources, plans):
    """No origin has precedence: equal role/stage/path evidence is combined."""
    sources = _staged(sources)
    selected, conflicts, warnings = [], [], []
    groups = {}
    alternative_roles = {role for plan in plans if not plan.optional for role in plan.alternatives}
    for source in sources:
        groups.setdefault((source_role(source), source.stage), []).append(source)
    for (role, stage), declarations in groups.items():
        plan = next((item for item in plans if item.role == role), None)
        paths = {item.path for item in declarations}
        if len(paths) > 1:
            message = f'{role}' + (f'@{stage}' if stage else '') + ': conflicting declarations: ' + '; '.join(
                f'{",".join(item.origins) or item.qualification} → {item.path}' for item in declarations)
            (warnings if plan and plan.optional and role not in alternative_roles else conflicts).append(message)
            continue
        first = declarations[0]
        origins = tuple(dict.fromkeys(origin for item in declarations for origin in item.origins))
        qualifications = tuple(dict.fromkeys(item.qualification for item in declarations))
        selected.append(replace(first, origins=origins, qualification=' + '.join(qualifications)))
    for plan in plans:
        role_sources = [item for item in selected if source_role(item) == plan.role]
        if len(role_sources) > 1:
            # Several exact stage outputs are supported only by a registered
            # stage-aware plan; the same file cannot stand for two stages.
            valid = (plan.stages and all(item.stage is not None for item in role_sources)
                     and len({item.path for item in role_sources}) == len(role_sources))
            if not valid:
                message = f'{plan.role}: incompatible or ambiguous stage declarations'
                (warnings if plan.optional and plan.role not in alternative_roles else conflicts).append(message)
                selected = [item for item in selected if item not in role_sources]
    for path in {item.path for item in selected}:
        aliases = [item for item in selected if item.path == path]
        if len({source_role(item) for item in aliases}) > 1:
            required = [item for item in aliases if not item.optional]
            message = 'One source file has incompatible roles: ' + ', '.join(item.role for item in aliases)
            (conflicts if len(required) > 1 else warnings).append(message)
            selected = [item for item in selected if item not in aliases or item in required and len(required) == 1]
    counts = {role: sum(source_role(item) == role for item in selected)
              for role in {source_role(item) for item in selected}}
    selected = [replace(item, role=f'{source_role(item)}:{item.stage}'
                        if counts[source_role(item)] > 1 else source_role(item)) for item in selected]
    return tuple(selected), tuple(dict.fromkeys(conflicts)), tuple(dict.fromkeys(warnings))


def _source_identity(source):
    """Resolve literal aliases and pin the containing directory, not file content."""
    path = Path(source.path)
    try:
        canonical = str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        canonical = str(path)  # Availability does not erase an established declaration.
    root = source.root_path or str(Path(canonical).parent)
    identity = source.root_identity
    if identity is None:
        try:
            info = Path(root).stat()
            identity = (info.st_dev, info.st_ino)
        except OSError:
            pass  # A declared future/unavailable source remains a declaration.
    return replace(source, path=canonical, root_path=root, root_identity=identity)


def resolve_context(store, job_id, *, expected_attempt_id=None, descriptors=(), resolvers=None,
                    sources=(), engine=None, session_selection=None):
    """Resolve declarations automatically; explicit observation never rewrites them."""
    from .source_declarations import recorded_sources
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
    plans = registration.sources if registration else ()
    automatic, issues = _automatic(found, registration)
    persisted = recorded_sources(job)
    if 'vasp_input' in job.get('progress_sources', {}):
        from .input_sources import resolve_vasp_input
        staged, input_issues = resolve_vasp_input(job, selected_engine)
        # A recorded staged-input expectation never falls back to mutable prepared
        # inputs. Explicit alternatives remain subject to normal conflict review.
        automatic = [item for item in automatic if source_role(item) != 'poscar']
        if staged is not None:
            automatic.append(staged)
        else:
            persisted = tuple(item for item in persisted if source_role(item) != 'poscar')
        issues.extend(input_issues)
    if persisted and selected_engine != str(job['engine']).upper():
        raise JobsError('Persisted sources belong to the recorded engine')
    declarations = tuple(_source_identity(item) for item in (*automatic, *persisted))
    invalid_automatic = bool(found.diagnostics and automatic)
    for plan in plans:
        if plan.optional:
            continue
        locations = [item for item in found.locations if item.role == plan.location_role]
        if (any(item.path is None for item in locations)
                and plan.filename is None and any(source_role(item) == plan.role for item in automatic)):
            invalid_automatic = True

    role_plans = (*plans, *registration.source_roles) if registration else ()
    selected, conflicts, warnings = reconcile_sources(declarations, role_plans)
    limitations = list(issues) + list(warnings) + list(conflicts)
    missing = [plan.role for plan in plans if not plan.optional and not any(
        source_role(item) in (plan.role, *plan.alternatives) for item in selected)]
    binding_status = 'SOURCE_CONFLICT' if conflicts else 'SOURCE_UNRESOLVED' if invalid_automatic or missing or not selected else 'BOUND'
    if missing and not conflicts:
        limitations.insert(0, 'No unique declared output/stage: ' + found.status)
    association = 'declared-job-location'
    origins = tuple(dict.fromkeys(origin for item in selected for origin in item.origins))
    if origins:
        association = ' + '.join(origins)
    # Retain the original pure automatic association label for existing callers.
    if origins and 'user-declared-at-add' not in origins:
        association = selected[0].qualification
    if sources and len(sources) <= 8:
        explicit = list(_explicit_sources(sources, selected_engine))
        stage_errors = []
        for index, source in enumerate(explicit):
            matching = {item.stage for item in declarations
                        if item.path == str(Path(source.path).resolve(strict=False)) and item.stage is not None}
            if source.stage is not None and matching and source.stage not in matching:
                stage_errors.append(f'{source.role}: explicit stage conflicts with its declared file.')
            elif source.stage is None and len(matching) == 1:
                explicit[index] = replace(source, stage=next(iter(matching)))
            elif source.stage is None and len(matching) > 1:
                stage_errors.append(f'{source.role}: file belongs to several declared stages; provide an explicit source stage.')
        selected = tuple(_source_identity(replace(item, origins=('invocation-local',))) for item in explicit)
        _, differing, _ = reconcile_sources((*declarations, *selected), role_plans)
        conflicts = tuple(dict.fromkeys((*conflicts, *differing)))
        binding_status = 'SOURCE_UNRESOLVED' if stage_errors else 'BOUND'
        limitations = list(stage_errors) + list(conflicts) + ['Invocation-local source override; historical execution binding is unverified.']
        association = 'explicit-caller-declaration'
    if len(selected) > 8 or len(sources) > 8:
        binding_status = 'SOURCE_UNRESOLVED'
        limitations.append('Observation is bounded to at most eight declared sources.')
    if binding_status != 'BOUND':
        selected = ()  # Never let unresolved/conflicted declarations reach readers.
    limitations.append(QUALIFICATION)
    stages = {source.stage for source in selected if source.stage is not None}
    stage = next(iter(stages)) if len(stages) == 1 else None
    signature = {'state': str(store.root), 'state_identity': state_identity, 'job': job['id'],
                 'attempt': expected, 'engine': selected_engine, 'stage': stage,
                 'sources': [asdict(source) for source in selected],
                 'declarations': [asdict(source) for source in declarations], 'binding_status': binding_status,
                 'locations': [(item.key, item.target_identity, item.directory_identity)
                               for item in found.locations
                               if any(plan.location_role == item.role for plan in plans)]}
    context = RunContext(stable_hash(signature), selected_engine, tuple(selected), stage, expected,
        str(store.root), job['id'], association, binding_status, tuple(dict.fromkeys(limitations)),
        declarations=declarations, source_conflicts=conflicts)
    if session_selection is not None:
        from .source_selection import apply_selection
        context = apply_selection(store, job, state_identity, context, session_selection)
    return context
