"""Explicitly registered, read-only declarations for supported runner dialects.

These adapters interpret argv/metadata as declarations. They neither authenticate
runner source nor establish execution ownership, result validity or finalization.
Unknown wrappers are deliberately left to caller-supplied LocationDescriptors.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .locations import LocationDescriptor, read_location_metadata


_VASP_PAIRS = frozenset({
    '--input', '--output', '--binary', '--ranks', '--ncore', '--kpar',
    '--mpi-mode', '--restart', '--restart-from', '--timeout', '--stop-before',
    '--extra-input',
})
_ORCA_PAIRS = frozenset({
    '--input', '--target', '--metadata', '--output', '--stderr', '--layout',
    '--geometry-contract', '--runtime-contract', '--minimum-free-disk-gb',
    '--disk-check-path', '--external-scratch-root', '--external-scratch-mount',
    '--external-scratch-minimum-free-gib', '--external-scratch-required-filesystem',
    '--execution-input', '--artifact',
})
_WORKFLOW_PAIRS = frozenset({
    '--mode', '--structure', '--config', '--output', '--orca-exe', '--nprocs',
    '--maxcore', '--imaginary-tolerance',
})
_MODES = {'opt': ('OPT',), 'opt+sp': ('OPT', 'SP'),
          'opt+freq': ('OPT', 'FREQ'), 'opt+freq+sp': ('OPT', 'FREQ', 'SP')}


def _argv(job):
    value = job.get('argv')
    if not isinstance(value, (list, tuple)) or not value:
        return ()
    if any(not isinstance(item, str) or not item or '\0' in item for item in value):
        return ()
    return tuple(value)


def _script_argv(job):
    argv = _argv(job)
    if not argv or Path(argv[0]).name not in {'bash', 'sh', 'zsh'}:
        return argv
    index = 1
    while index < len(argv):
        if argv[index] in {'-e', '-u', '-eu', '-ue'}:
            index += 1
        elif argv[index:index + 2] == ('-o', 'pipefail'):
            index += 2
        elif argv[index] == '--':
            index += 1
            break
        else:
            break
    # In particular, shell -c strings are opaque and are never parsed/evaluated.
    if index >= len(argv) or argv[index].startswith('-'):
        return ()
    return argv[index:]


def _options(argv, pairs, flags=(), repeated=(), *, equals=False):
    options = {}
    index = 1
    while index < len(argv):
        name, value = argv[index], None
        if equals and '=' in name:
            name, value = name.split('=', 1)
        if name in options and name not in repeated:
            raise ValueError(f'Repeated runner argument: {name}')
        if name in flags and value is None:
            options[name] = True
        elif name in pairs:
            if value is None:
                index += 1
                if index >= len(argv):
                    raise ValueError(f'Missing value for {name}')
                value = argv[index]
            if not value or value.startswith('--') or '\0' in value:
                raise ValueError(f'Missing or ambiguous value for {name}')
            if name in repeated:
                options.setdefault(name, []).append(value)
            else:
                options[name] = value
        else:
            raise ValueError(f'Unsupported runner argument: {name}')
        index += 1
    return options


def _path(value, job):
    if (not isinstance(value, str) or not value or '://' in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError('A filesystem path without URI syntax or control characters is required')
    # Literal shell-like characters remain literal filenames, never expansions.
    path = Path(value)
    if not path.is_absolute():
        cwd = Path(job['cwd'])
        if not cwd.is_absolute():
            raise ValueError('Relative declarations require an absolute Jobs cwd')
        path = cwd / path
    return path


def _metadata_path(value, job):
    path = _path(value, job)
    if not Path(value).is_absolute():
        raise ValueError('Workflow metadata requires explicit absolute paths')
    return path


def _decl(role, path, *, stage=None, kind='file', origin='recognized-runner-argv',
          detail='', expected_creation=False):
    return LocationDescriptor(role, path, kind=kind, stage=stage, origin=origin,
                              qualification='declared', detail=detail,
                              expected_creation=expected_creation)


def _unknown(detail, *, stage=None, origin='recognized-runner-argv'):
    return LocationDescriptor('output', None, stage=stage, origin=origin,
                              qualification='unresolved', detail=detail)


def vasp_runner_locations(job):
    """Interpret the known run-vasp.sh argv dialect without terminal receipts."""
    argv = _script_argv(job)
    if not argv or Path(argv[0]).name != 'run-vasp.sh':
        return ()
    try:
        options = _options(argv, _VASP_PAIRS,
                           {'--managed-foreground', '--dry-run'}, {'--extra-input'}, equals=True)
        source = _path(options['--input'], job)
        output = _path(options['--output'], job)
    except (KeyError, TypeError, ValueError) as exc:
        return (_unknown(f'Unresolved run-vasp.sh declaration: {exc}'),)
    return (_decl('inputs', source, kind='directory'),
            _decl('output', output, kind='directory', expected_creation='--dry-run' not in options,
                  detail='Declared VASP output; no process or scientific-result association verified.'))


def _orca_stage(options, job):
    try:
        target = read_location_metadata(_path(options['--target'], job))
        stage = target.get('target', {}).get('stage_type')
        if (target.get('schema_version') == 1 and isinstance(stage, str)
                and stage.strip() and len(stage) <= 256 and stage.isprintable()):
            return stage
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _orca_stage_locations(options, job, *, stage=None, origin='recognized-runner-argv'):
    locations = []
    for key, role in (('--input', 'inputs'), ('--output', 'output'),
                      ('--stderr', 'operational-logs'), ('--target', 'records'),
                      ('--metadata', 'records'), ('--layout', 'records'),
                      ('--geometry-contract', 'records'), ('--runtime-contract', 'records')):
        if key in options:
            path = (_metadata_path(options[key], job) if origin == 'workflow-metadata'
                    else _path(options[key], job))
            locations.append(_decl(role, path, stage=stage, origin=origin,
                                   expected_creation=key in {'--output', '--stderr', '--metadata'},
                                   detail=f'ORCA {key[2:]} declaration; scientific validation not assessed.'))
    for key, role in (('--artifact', 'output'), ('--execution-input', 'inputs')):
        for item in options.get(key, ()):
            label, separator, value = item.partition('=')
            if not label or not separator or not value:
                raise ValueError(f'Invalid {key} role=path declaration')
            locations.append(_decl(role, _path(value, job), stage=stage, origin=origin,
                                   detail=f'Declared ORCA {label} source.',
                                   expected_creation=key == '--artifact'))
    return locations


def orca_runner_locations(job):
    """Resolve explicit ORCA stage files; source files open through their parent."""
    argv = _script_argv(job)
    if not argv or Path(argv[0]).name != 'run_orca.sh':
        return ()
    try:
        options = _options(argv, _ORCA_PAIRS,
                           {'--require-runtime-contract', '--replace-stale-lock'},
                           {'--artifact', '--execution-input'})
        if not {'--input', '--target', '--metadata', '--output', '--stderr'} <= options.keys():
            raise ValueError('Required ORCA stage paths are missing')
        return tuple(_orca_stage_locations(options, job, stage=_orca_stage(options, job)))
    except (KeyError, TypeError, ValueError) as exc:
        return (_unknown(f'Unresolved run_orca.sh declaration: {exc}'),)


def orca_workflow_locations(job):
    """Expose the declared root without inventing hashed target/stage locations."""
    argv = _script_argv(job)
    if not argv or Path(argv[0]).name != 'run_opt_freq_sp.sh':
        return ()
    try:
        options = _options(argv, _WORKFLOW_PAIRS,
                           {'--require-minimum', '--no-require-minimum', '--new-attempt', '--plan', '--json'})
        stages = _MODES[options['--mode'].lower()]
        locations = [_decl('workflow-root', _path(options['--output'], job), kind='directory',
                           detail='Declared workflow root; individual stage paths require recorded metadata.')]
        for key in ('--structure', '--config'):
            locations.append(_decl('inputs' if key == '--structure' else 'records',
                                   _path(options[key], job)))
        for stage in stages:
            locations.append(_unknown('Stage output has no exact recorded path in this argv; '
                                      'the workflow root is available separately.', stage=stage))
        return tuple(locations)
    except (KeyError, TypeError, ValueError) as exc:
        return (_unknown(f'Unresolved ORCA workflow declaration: {exc}'),)


def _workflow_state_argv(job):
    argv = _argv(job)
    if not argv or not Path(argv[0]).name.startswith('python'):
        return ()
    index = 1
    while index < len(argv) and argv[index] in {'-B', '-I', '-S', '-s', '-E', '-u'}:
        index += 1
    if argv[index:index + 2] != ('-m', 'cmw.molecular.workflows.cli'):
        return ()
    remaining = argv[index + 2:]
    return remaining if remaining and remaining[0] in {'next', 'advance'} else ()


def orca_workflow_state_locations(job):
    """Read only an explicitly addressed v1 OPT/FREQ/SP workflow-state record."""
    argv = _workflow_state_argv(job)
    if not argv:
        return ()
    locations = []
    try:
        options = _options(argv, {'--state', '--stage'})
        state_path = _path(options['--state'], job)
        locations.append(_decl('records', state_path, detail='Explicit ORCA workflow-state record.'))
        state = read_location_metadata(state_path)
        if (state.get('schema_version') != 1 or state.get('workflow') != 'opt_freq_sp'
                or not isinstance(state.get('workflow_target_id'), str)
                or not state['workflow_target_id'] or not isinstance(state.get('stages'), Mapping)):
            raise ValueError('Unsupported workflow-state record')
        for key in ('output_root', 'result_path'):
            if state.get(key):
                locations.append(_decl('workflow-root' if key == 'output_root' else 'records',
                    _metadata_path(state[key], job), kind='directory' if key == 'output_root' else 'file',
                    origin='workflow-metadata'))
        stage_order = state.get('stage_order')
        if (not isinstance(stage_order, list) or not stage_order
                or len(stage_order) > 3 or len(set(stage_order)) != len(stage_order)
                or any(stage not in {'OPT', 'FREQ', 'SP'} for stage in stage_order)):
            raise ValueError('Unsupported workflow stage order')
        for stage in stage_order:
            record = state['stages'].get(stage)
            if not isinstance(record, Mapping) or record.get('selected') is not True:
                raise ValueError('Workflow stage selection is inconsistent')
            runner = record.get('runner')
            if isinstance(runner, Mapping):
                if (runner.get('stage') != stage
                        or runner.get('target_id') != record.get('target_id')
                        or runner.get('attempt_id') != record.get('attempt_id')
                        or not runner.get('target_id') or not runner.get('attempt_id')):
                    locations.append(_unknown('Workflow runner identity is inconsistent.',
                                              stage=stage, origin='workflow-metadata'))
                    continue
                mapped = {f'--{key.replace("_", "-")}': runner[key]
                          for key in ('input', 'output', 'stderr', 'target', 'metadata',
                                      'layout', 'geometry_contract') if runner.get(key)}
                locations.extend(_orca_stage_locations(mapped, job, stage=stage, origin='workflow-metadata'))
                if not runner.get('output'):
                    locations.append(_unknown('Stage output is not recorded.', stage=stage,
                                              origin='workflow-metadata'))
            else:
                artifacts = record.get('artifacts')
                output = artifacts.get('output') if isinstance(artifacts, Mapping) else None
                if isinstance(output, Mapping) and output.get('path'):
                    locations.append(_decl('output', _metadata_path(output['path'], job), stage=stage,
                                           origin='workflow-metadata', detail='Recorded source; validation not reassessed.'))
                else:
                    locations.append(_unknown('Stage output has not been recorded.', stage=stage,
                                              origin='workflow-metadata'))
                if record.get('metadata'):
                    locations.append(_decl('records', _metadata_path(record['metadata'], job), stage=stage,
                                           origin='workflow-metadata'))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        locations.append(_unknown(f'Unresolved ORCA workflow metadata: {exc}', origin='workflow-metadata'))
    return tuple(locations)


def builtin_resolvers():
    """Explicit registration; clients may add resolvers without engine branches."""
    return (vasp_runner_locations, orca_runner_locations, orca_workflow_locations,
            orca_workflow_state_locations)
